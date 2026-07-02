"""Interactive credential collection and storage/retrieval for iCloud Contacts.

Apple has no OAuth flow for iCloud Contacts access - the only supported
mechanism is CardDAV (implemented separately in the iCloud adapter)
authenticated via HTTP Basic Auth using an app-specific password the user
generates by hand at appleid.apple.com (not their real Apple ID password,
since 2FA-protected accounts can't complete Basic Auth with it).

This module has one responsibility: prompt for the Apple ID and app-specific
password once (`run_icloud_auth`) and store/retrieve them via the 1Password
wrapper in `contacts_sync.auth.onepassword` - it does not duplicate that
module's logic, and it has no knowledge of CardDAV or HTTP (that lives in the
iCloud adapter, not here).
"""

import getpass

from contacts_sync.auth.onepassword import op_read, op_set_field, OnePasswordError

VAULT = "Private"


def run_icloud_auth() -> None:
    """Prompt for the Apple ID and app-specific password and save them to 1Password."""
    apple_id = input("Apple ID (email): ").strip()
    app_password = getpass.getpass("App-specific password (from appleid.apple.com): ").strip()
    op_set_field(VAULT, "icloud", "apple_id", apple_id)
    op_set_field(VAULT, "icloud", "app_password", app_password)


def get_credentials() -> tuple[str, str]:
    """Return the stored (apple_id, app_password) tuple for use as HTTP Basic Auth.

    Raises `RuntimeError` if no credentials have been saved yet, i.e.
    `run_icloud_auth` hasn't been run.
    """
    try:
        apple_id = op_read(f"op://{VAULT}/icloud/apple_id")
        app_password = op_read(f"op://{VAULT}/icloud/app_password")
    except OnePasswordError as exc:
        raise RuntimeError("No iCloud credentials found. Run `contacts-sync auth icloud` first.") from exc
    return apple_id, app_password
