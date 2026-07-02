"""Microsoft OAuth device-code flow and persistent token cache storage/retrieval.

This module has one responsibility: run the interactive Microsoft device-code
consent flow once (`run_device_code_auth`) and hand back a zero-argument
closure that yields a live access token on demand (`get_token_provider`). It
stores and retrieves an MSAL `SerializableTokenCache` blob via the 1Password
wrapper in `contacts_sync.auth.onepassword` - it does not duplicate that
module's logic, and it has no knowledge of the Microsoft Graph API (contact
CRUD lives in the Microsoft adapter, not here).

Targets personal Microsoft accounts only (outlook.com/hotmail/live) - the
`/consumers` authority excludes work/school (Azure AD) accounts, which are
out of scope.
"""

import msal

from contacts_sync.auth.onepassword import op_read, op_set_field, OnePasswordError

AUTHORITY = "https://login.microsoftonline.com/consumers"
SCOPES = ["Contacts.ReadWrite"]
VAULT = "Private"


def _load_cache() -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    try:
        cache.deserialize(op_read(f"op://{VAULT}/microsoft/token_cache"))
    except OnePasswordError:
        pass
    return cache


def _save_cache(cache: msal.SerializableTokenCache) -> None:
    if cache.has_state_changed:
        op_set_field(VAULT, "microsoft", "token_cache", cache.serialize())


def run_device_code_auth(client_id: str) -> None:
    """Run the interactive device-code consent flow and save the resulting token cache.

    Prints the device-code instructions for the user to complete in a
    browser, then stores the serialized MSAL token cache in 1Password for
    later use by `get_token_provider`.
    """
    cache = _load_cache()
    app = msal.PublicClientApplication(client_id, authority=AUTHORITY, token_cache=cache)
    flow = app.initiate_device_flow(scopes=SCOPES)
    print(flow["message"])
    app.acquire_token_by_device_flow(flow)
    _save_cache(cache)


def get_token_provider(client_id: str):
    """Return a zero-argument closure that yields a live Microsoft access token.

    Each call to the returned closure loads the stored token cache, attempts
    a silent (non-interactive) token acquisition - which transparently
    refreshes the token if needed - and raises `RuntimeError` if no cached
    account/token is available (i.e. `run_device_code_auth` hasn't been run).
    """

    def get_token() -> str:
        cache = _load_cache()
        app = msal.PublicClientApplication(client_id, authority=AUTHORITY, token_cache=cache)
        accounts = app.get_accounts()
        result = app.acquire_token_silent(SCOPES, account=accounts[0] if accounts else None)
        if not result:
            raise RuntimeError("No cached Microsoft token. Run `contacts-sync auth microsoft` first.")
        _save_cache(cache)
        return result["access_token"]

    return get_token
