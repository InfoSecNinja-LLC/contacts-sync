import logging

import typer
from dotenv import load_dotenv
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

from datetime import datetime, timezone

from contacts_sync.db import Database
from contacts_sync.names import find_name_fix_candidates
from contacts_sync.paths import app_dir
from contacts_sync.sync_engine import SyncEngine
from contacts_sync.auth import google_auth, microsoft_auth, icloud_auth
from contacts_sync.adapters.google import GoogleAdapter
from contacts_sync.adapters.microsoft import MicrosoftAdapter
from contacts_sync.adapters.icloud import ICloudAdapter, discover_addressbook_path


def _configure_logging():
    logger = logging.getLogger("contacts_sync.sync")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.FileHandler(app_dir() / "sync.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger.addHandler(handler)


load_dotenv(app_dir() / ".env")

app = typer.Typer()
auth_app = typer.Typer()
app.add_typer(auth_app, name="auth")

DB_PATH = str(app_dir() / "contacts.db")


def _progress_display() -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
    )


class _ProgressReporter:
    """Adapts SyncEngine progress events onto a rich Progress display.

    Pull: one task per provider - an indeterminate spinner while the
    provider's changes (and photos) download, becoming a bar once the change
    count is known and merging starts. Push: a single bar over all local
    contacts. Rendering degrades gracefully to plain output when stdout
    isn't a terminal (logs, schedulers).
    """

    def __init__(self, progress: Progress):
        self._progress = progress
        self._pull_tasks = {}
        self._push_task = None

    def __call__(self, event: str, **kwargs):
        if event == "pull_start":
            provider = kwargs["provider"]
            self._pull_tasks[provider] = self._progress.add_task(
                f"Pulling changes from {provider}", total=None
            )
        elif event == "pull_done":
            provider = kwargs["provider"]
            self._progress.update(
                self._pull_tasks[provider],
                description=f"Merging changes from {provider}",
                total=kwargs["total"],
                completed=0,
            )
        elif event == "change_done":
            self._progress.advance(self._pull_tasks[kwargs["provider"]])
        elif event == "provider_error":
            task_id = self._pull_tasks.get(kwargs["provider"])
            if task_id is not None:
                self._progress.update(task_id, description=f"{kwargs['provider']} failed")
        elif event == "push_start":
            self._push_task = self._progress.add_task(
                "Pushing to providers", total=kwargs["total"]
            )
        elif event == "push_advance":
            self._progress.advance(self._push_task)


@app.callback()
def main():
    """contacts-sync: sync contacts between Google, iCloud, and Microsoft."""


@app.command()
def version():
    typer.echo("contacts-sync 0.2.0")


@auth_app.command("google")
def auth_google(client_secrets: str = typer.Option(..., help="Path to Google OAuth client_secrets.json")):
    google_auth.run_installed_app_auth(client_secrets)
    typer.echo("Google credentials saved to .env.")


@auth_app.command("microsoft")
def auth_microsoft(client_id: str = typer.Option(..., help="Entra app registration client ID")):
    microsoft_auth.run_device_code_auth(client_id)
    typer.echo("Microsoft credentials saved to .env.")


@auth_app.command("icloud")
def auth_icloud():
    icloud_auth.run_icloud_auth()
    typer.echo("iCloud credentials saved to .env.")


def _build_adapters(microsoft_client_id: str) -> dict:
    google_creds = google_auth.get_credentials()
    apple_id, app_password = icloud_auth.get_credentials()
    ms_token_provider = microsoft_auth.get_token_provider(microsoft_client_id)
    addressbook_path = discover_addressbook_path(apple_id, app_password)
    return {
        "google": GoogleAdapter(google_creds),
        "microsoft": MicrosoftAdapter(ms_token_provider),
        "icloud": ICloudAdapter(apple_id, app_password, addressbook_path),
    }


@app.command()
def sync(
    dry_run: bool = typer.Option(False, "--dry-run"),
    full: bool = typer.Option(
        False,
        "--full",
        help="Clear sync tokens and link etags first, forcing a full re-pull and "
        "re-merge from every provider. Use to backfill fields the tool learned to "
        "sync after your last run (e.g. photos), or after fixing a sync bug.",
    ),
    microsoft_client_id: str = typer.Option(..., envvar="MICROSOFT_CLIENT_ID"),
):
    _configure_logging()
    db = Database(DB_PATH)
    db.migrate()
    if full:
        if dry_run:
            typer.echo("--full has no effect with --dry-run (sync state is left untouched).")
        else:
            db.reset_sync_state()
            typer.echo("Cleared sync state - performing a full re-pull from all providers.")
    try:
        adapters = _build_adapters(microsoft_client_id)
    except RuntimeError as exc:
        typer.echo(f"ERROR: {exc}")
        raise typer.Exit(code=1)

    with _progress_display() as progress:
        engine = SyncEngine(db, adapters, progress=_ProgressReporter(progress))
        result = engine.run(dry_run=dry_run)

    typer.echo(
        f"Created: {result.created}, Updated: {result.updated}, "
        f"Deleted: {result.deleted}, Pending review: {result.pending_review}"
    )
    if result.provider_errors:
        for provider, error in result.provider_errors.items():
            typer.echo(f"ERROR [{provider}]: {error}")
        raise typer.Exit(code=1)


@app.command("fix-names")
def fix_names(
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Write the splits to the local store and push them to all providers. "
        "Without this flag only a preview is printed.",
    ),
    microsoft_client_id: str = typer.Option(None, envvar="MICROSOFT_CLIENT_ID"),
):
    """Repair contacts whose FULL name is stuck in the first-name field.

    Finds contacts with no last name whose first name contains multiple words
    (e.g. first="Pallavi Sharma", last=empty) and splits at the last space:
    first="Pallavi", last="Sharma" - the same heuristic phones and Google use.
    Contacts that already have a last name are never touched, so the command
    is safe to re-run. Preview by default; --apply writes and pushes.
    """
    _configure_logging()
    db = Database(DB_PATH)
    db.migrate()
    candidates = find_name_fix_candidates(db.list_contacts())
    if not candidates:
        typer.echo("No contacts need name fixes.")
        return
    for contact, new_given, new_family in candidates:
        typer.echo(f"[{contact.id}] \"{contact.given_name}\" -> first: \"{new_given}\", last: \"{new_family}\"")
    typer.echo(f"{len(candidates)} contact(s) to fix.")
    if not apply:
        typer.echo("Preview only - re-run with --apply to write these changes and push them to all providers.")
        return

    # Build adapters BEFORE touching the database: a credential problem then
    # aborts cleanly with nothing written, instead of leaving local repairs
    # that the providers never hear about (the sync engine only pushes
    # contacts dirtied by its own pull phase, so a stranded local edit would
    # otherwise never converge).
    try:
        adapters = _build_adapters(microsoft_client_id)
    except RuntimeError as exc:
        typer.echo(f"ERROR: {exc}")
        typer.echo("Nothing was changed. Fix the credential problem and re-run.")
        raise typer.Exit(code=1)

    # Stamp the repaired fields with the current time so a later pull of the
    # OLD unsplit name from a provider (whose updated_at is empty or older)
    # loses the newest-edit-wins merge and cannot silently undo this repair.
    logger = logging.getLogger("contacts_sync.sync")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    dirty_ids = set()
    for contact, new_given, new_family in candidates:
        contact.given_name = new_given
        contact.family_name = new_family
        contact.field_meta["given_name"] = now
        contact.field_meta["family_name"] = now
        db.update_contact(contact)
        dirty_ids.add(contact.id)
        logger.info(
            f'FIX-NAMES contact_id={contact.id} given="{new_given}" family="{new_family}"'
        )

    with _progress_display() as progress:
        engine = SyncEngine(db, adapters, progress=_ProgressReporter(progress))
        errors = engine.push_contacts(dirty_ids)
    typer.echo(f"Fixed and pushed {len(dirty_ids)} contact(s).")
    if errors:
        for provider, error in errors.items():
            typer.echo(f"ERROR [{provider}]: {error}")
        typer.echo(
            "The failed provider(s) still hold the old names. After fixing the "
            "error, run 'contacts-sync push --all' to bring them up to date."
        )
        raise typer.Exit(code=1)


@app.command()
def push(
    push_all: bool = typer.Option(
        False,
        "--all",
        help="Push every local contact to every provider (update if linked, create "
        "if not). Recovery tool: reconverges providers after a partial push "
        "failure or a restored local database.",
    ),
    microsoft_client_id: str = typer.Option(None, envvar="MICROSOFT_CLIENT_ID"),
):
    """Force-push local contacts out to the providers (no pull)."""
    if not push_all:
        typer.echo("Nothing to do. Pass --all to push every local contact to every provider.")
        return
    _configure_logging()
    db = Database(DB_PATH)
    db.migrate()
    try:
        adapters = _build_adapters(microsoft_client_id)
    except RuntimeError as exc:
        typer.echo(f"ERROR: {exc}")
        raise typer.Exit(code=1)
    contact_ids = [contact.id for contact in db.list_contacts()]
    with _progress_display() as progress:
        engine = SyncEngine(db, adapters, progress=_ProgressReporter(progress))
        errors = engine.push_contacts(contact_ids)
    typer.echo(f"Pushed {len(contact_ids)} contact(s) to all providers.")
    if errors:
        for provider, error in errors.items():
            typer.echo(f"ERROR [{provider}]: {error}")
        raise typer.Exit(code=1)


@app.command()
def status():
    db = Database(DB_PATH)
    db.migrate()
    contacts = db.list_contacts()
    typer.echo(f"{len(contacts)} contacts in local store.")
    for provider in ("google", "microsoft", "icloud"):
        token = db.get_sync_token(provider)
        typer.echo(f"{provider}: sync token {'set' if token else 'not set'}")


@app.command()
def review():
    db = Database(DB_PATH)
    db.migrate()
    pending = db.list_pending_matches()
    if not pending:
        typer.echo("No pending matches to review.")
        return
    for match in pending:
        typer.echo(f"\nProvider {match['provider']} contact {match['provider_id']} could be:")
        for candidate_id in match["candidate_contact_ids"]:
            candidate = db.get_contact(candidate_id)
            name = candidate.display_name if candidate else "(deleted)"
            typer.echo(f"  [{candidate_id}] {name}")
        choice = typer.prompt("Enter contact id to link, or 'skip'")
        if choice == "skip":
            continue
        try:
            contact_id = int(choice)
        except ValueError:
            typer.echo(f"'{choice}' is not a valid contact id or 'skip' — leaving this match pending.")
            continue
        if contact_id not in match["candidate_contact_ids"]:
            typer.echo(f"{contact_id} is not one of the listed candidates — leaving this match pending.")
            continue
        db.link_provider(contact_id, match["provider"], match["provider_id"])
        db.delete_pending_match(match["id"])


@app.command()
def doctor(microsoft_client_id: str = typer.Option(None, envvar="MICROSOFT_CLIENT_ID")):
    for provider, check in (
        ("google", lambda: google_auth.get_credentials()),
        ("icloud", lambda: icloud_auth.get_credentials()),
        ("microsoft", lambda: microsoft_auth.get_token_provider(microsoft_client_id)()),
    ):
        try:
            check()
            typer.echo(f"{provider}: OK")
        except Exception as exc:
            typer.echo(f"{provider}: FAILED ({exc})")


if __name__ == "__main__":
    app()
