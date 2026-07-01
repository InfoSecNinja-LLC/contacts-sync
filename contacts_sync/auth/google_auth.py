"""Google OAuth ("installed app") flow and credential storage/retrieval.

This module has one responsibility: run the interactive Google OAuth consent
flow once (`run_installed_app_auth`) and hand back a live, refreshed
`google.oauth2.credentials.Credentials` object on demand (`get_credentials`).
It stores and retrieves the refresh token, client id, and client secret via
the 1Password wrapper in `contacts_sync.auth.onepassword` - it does not
duplicate that module's logic, and it has no knowledge of the Google People
API (contact CRUD lives in the Google adapter, not here).
"""

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

from contacts_sync.auth.onepassword import op_read, op_set_field, OnePasswordError

SCOPES = ["https://www.googleapis.com/auth/contacts"]
VAULT = "contacts-sync"


def run_installed_app_auth(client_secrets_path: str) -> None:
    """Run the interactive OAuth consent flow and save the resulting credentials.

    Opens a local browser for the user to approve access, then stores the
    refresh token, client id, and client secret in 1Password for later use
    by `get_credentials`.
    """
    flow = InstalledAppFlow.from_client_secrets_file(client_secrets_path, SCOPES)
    credentials = flow.run_local_server(port=0)
    op_set_field(VAULT, "google", "refresh_token", credentials.refresh_token)
    op_set_field(VAULT, "google", "client_id", credentials.client_id)
    op_set_field(VAULT, "google", "client_secret", credentials.client_secret)


def get_credentials() -> Credentials:
    """Build a live, refreshed `Credentials` object from stored 1Password secrets.

    Raises `RuntimeError` if no credentials have been saved yet (i.e.
    `run_installed_app_auth` hasn't been run).
    """
    try:
        refresh_token = op_read(f"op://{VAULT}/google/refresh_token")
        client_id = op_read(f"op://{VAULT}/google/client_id")
        client_secret = op_read(f"op://{VAULT}/google/client_secret")
    except OnePasswordError as exc:
        raise RuntimeError(
            "No Google credentials found. Run `contacts-sync auth google` first."
        ) from exc

    credentials = Credentials(
        token=None,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )
    credentials.refresh(Request())
    return credentials
