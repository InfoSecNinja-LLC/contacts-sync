import logging

import typer
from dotenv import load_dotenv

from contacts_sync.db import Database
from contacts_sync.sync_engine import SyncEngine
from contacts_sync.auth import google_auth, microsoft_auth, icloud_auth
from contacts_sync.adapters.google import GoogleAdapter
from contacts_sync.adapters.microsoft import MicrosoftAdapter
from contacts_sync.adapters.icloud import ICloudAdapter, discover_addressbook_path


def _configure_logging():
    logger = logging.getLogger("contacts_sync.sync")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.FileHandler("sync.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger.addHandler(handler)


load_dotenv()

app = typer.Typer()
auth_app = typer.Typer()
app.add_typer(auth_app, name="auth")

DB_PATH = "contacts.db"


@app.callback()
def main():
    """contacts-sync: sync contacts between Google, iCloud, and Microsoft."""


@app.command()
def version():
    typer.echo("contacts-sync 0.1.0")


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
    microsoft_client_id: str = typer.Option(..., envvar="MICROSOFT_CLIENT_ID"),
):
    _configure_logging()
    db = Database(DB_PATH)
    db.migrate()
    try:
        adapters = _build_adapters(microsoft_client_id)
    except RuntimeError as exc:
        typer.echo(f"ERROR: {exc}")
        raise typer.Exit(code=1)

    engine = SyncEngine(db, adapters)
    result = engine.run(dry_run=dry_run)

    typer.echo(
        f"Created: {result.created}, Updated: {result.updated}, "
        f"Deleted: {result.deleted}, Pending review: {result.pending_review}"
    )
    if result.provider_errors:
        for provider, error in result.provider_errors.items():
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
