import typer
from contacts_sync.db import Database
from contacts_sync.sync_engine import SyncEngine
from contacts_sync.auth import google_auth, microsoft_auth, icloud_auth
from contacts_sync.adapters.google import GoogleAdapter
from contacts_sync.adapters.microsoft import MicrosoftAdapter
from contacts_sync.adapters.icloud import ICloudAdapter

app = typer.Typer()
auth_app = typer.Typer()
app.add_typer(auth_app, name="auth")

DB_PATH = "contacts.db"
ICLOUD_ADDRESSBOOK_PATH = "/carddavhome/addressbooks/card/"


@app.callback()
def main():
    """contacts-sync: sync contacts between Google, iCloud, and Microsoft."""


@app.command()
def version():
    typer.echo("contacts-sync 0.1.0")


@auth_app.command("google")
def auth_google(client_secrets: str = typer.Option(..., help="Path to Google OAuth client_secrets.json")):
    google_auth.run_installed_app_auth(client_secrets)
    typer.echo("Google credentials saved to 1Password.")


@auth_app.command("microsoft")
def auth_microsoft(client_id: str = typer.Option(..., help="Entra app registration client ID")):
    microsoft_auth.run_device_code_auth(client_id)
    typer.echo("Microsoft credentials saved to 1Password.")


@auth_app.command("icloud")
def auth_icloud():
    icloud_auth.run_icloud_auth()
    typer.echo("iCloud credentials saved to 1Password.")


def _build_adapters(microsoft_client_id: str) -> dict:
    google_creds = google_auth.get_credentials()
    apple_id, app_password = icloud_auth.get_credentials()
    ms_token_provider = microsoft_auth.get_token_provider(microsoft_client_id)
    return {
        "google": GoogleAdapter(google_creds),
        "microsoft": MicrosoftAdapter(ms_token_provider),
        "icloud": ICloudAdapter(apple_id, app_password, ICLOUD_ADDRESSBOOK_PATH),
    }


@app.command()
def sync(
    dry_run: bool = typer.Option(False, "--dry-run"),
    microsoft_client_id: str = typer.Option(..., envvar="CONTACTS_SYNC_MS_CLIENT_ID"),
):
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


if __name__ == "__main__":
    app()
