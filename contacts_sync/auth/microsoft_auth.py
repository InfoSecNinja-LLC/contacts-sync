"""Microsoft OAuth device-code flow and persistent token cache storage/retrieval.

This module has one responsibility: run the interactive Microsoft device-code
consent flow once (`run_device_code_auth`) and hand back a zero-argument
closure that yields a live access token on demand (`get_token_provider`). It
stores and retrieves an MSAL `SerializableTokenCache` blob via the `.env`
wrapper in `contacts_sync.auth.env_store` - it does not duplicate that
module's logic, and it has no knowledge of the Microsoft Graph API (contact
CRUD lives in the Microsoft adapter, not here).

Targets personal Microsoft accounts only (outlook.com/hotmail/live) - the
`/consumers` authority excludes work/school (Azure AD) accounts, which are
out of scope.
"""

import time

import msal

from contacts_sync.auth.env_store import env_read, env_set, EnvStoreError

AUTHORITY = "https://login.microsoftonline.com/consumers"
SCOPES = ["Contacts.ReadWrite"]


def _load_cache() -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    try:
        cache.deserialize(env_read("MICROSOFT_TOKEN_CACHE"))
    except EnvStoreError:
        pass
    return cache


def _save_cache(cache: msal.SerializableTokenCache) -> None:
    if cache.has_state_changed:
        env_set("MICROSOFT_TOKEN_CACHE", cache.serialize())


def run_device_code_auth(client_id: str) -> None:
    """Run the interactive device-code consent flow and save the resulting token cache.

    Prints the device-code instructions for the user to complete in a
    browser, then stores the serialized MSAL token cache in `.env` for
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

    The returned closure caches the acquired access token in-process for its
    lifetime (minus a safety margin), so a long multi-request sync doesn't
    re-read the token cache from `.env` on every single Graph request. Only
    the first call (or one near token expiry) touches `.env` / MSAL; it
    raises `RuntimeError` if no cached account/token is available (i.e.
    `run_device_code_auth` hasn't been run).
    """
    state = {"token": None, "expires_at": 0.0}

    def get_token() -> str:
        now = time.time()
        if state["token"] is not None and now < state["expires_at"] - 120:
            return state["token"]
        cache = _load_cache()
        app = msal.PublicClientApplication(client_id, authority=AUTHORITY, token_cache=cache)
        accounts = app.get_accounts()
        result = app.acquire_token_silent(SCOPES, account=accounts[0] if accounts else None)
        if not result:
            raise RuntimeError("No cached Microsoft token. Run `contacts-sync auth microsoft` first.")
        _save_cache(cache)
        state["token"] = result["access_token"]
        state["expires_at"] = now + result.get("expires_in", 3600)
        return state["token"]

    return get_token
