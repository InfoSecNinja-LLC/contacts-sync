"""Thin wrapper around a local `.env` file for reading and writing credentials.

This module has one responsibility: read a value by key from `.env` and
upsert a key/value pair into it. It has no knowledge of which provider
(Google/Microsoft/iCloud) a credential belongs to - that logic lives in each
provider's own auth module, which calls into this one.
"""

from dotenv import dotenv_values, set_key

from contacts_sync.paths import app_dir

ENV_PATH = app_dir() / ".env"


class EnvStoreError(RuntimeError):
    """Raised when a requested key is missing from the `.env` file."""

    pass


def env_read(key: str) -> str:
    """Read a single value from `.env` by key."""
    value = dotenv_values(ENV_PATH).get(key)
    if not value:
        raise EnvStoreError(f"'{key}' not set in {ENV_PATH}")
    return value


def env_set(key: str, value: str) -> None:
    """Upsert a key's value in `.env`, creating the file if needed."""
    ENV_PATH.touch(exist_ok=True)
    set_key(str(ENV_PATH), key, value, quote_mode="always")
