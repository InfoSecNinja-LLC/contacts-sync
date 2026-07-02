"""Thin wrapper around the 1Password CLI ('op') for reading and writing secrets.

This module has one responsibility: shell out to `op` to read a secret by
reference and to upsert a field on an item. It has no knowledge of which
provider (Google/Microsoft/iCloud) a credential belongs to - that logic lives
in each provider's own auth module, which calls into this one.

Secret values are never logged or included in exception messages - only the
`op://` reference string and the CLI's own stderr output are surfaced.
"""

import subprocess


class OnePasswordError(RuntimeError):
    """Raised when the 1Password CLI is unavailable or a command fails."""

    pass


def op_read(reference: str) -> str:
    """Read a single secret from 1Password by its `op://vault/item/field` reference."""
    try:
        result = subprocess.run(["op", "read", reference], capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise OnePasswordError(
            "1Password CLI ('op') not found on PATH. Install it from "
            "https://developer.1password.com/docs/cli/"
        ) from exc
    if result.returncode != 0:
        raise OnePasswordError(
            f"1Password CLI failed to read '{reference}': {result.stderr.strip()}. "
            "Is the 1Password desktop app unlocked?"
        )
    return result.stdout.strip()


def op_set_field(vault: str, title: str, field_name: str, value: str) -> None:
    """Upsert a field's value on a 1Password item, creating the item if needed.

    Tries `op item edit` first (the common case, where the item already
    exists from a previous auth run) and only falls back to `op item create`
    if the edit fails - e.g. because the item doesn't exist yet.
    """
    edit = subprocess.run(
        ["op", "item", "edit", title, f"--vault={vault}", f"{field_name}={value}"],
        capture_output=True,
        text=True,
    )
    if edit.returncode == 0:
        return
    create = subprocess.run(
        [
            "op",
            "item",
            "create",
            "--category=password",
            f"--vault={vault}",
            f"--title={title}",
            f"{field_name}={value}",
        ],
        capture_output=True,
        text=True,
    )
    if create.returncode != 0:
        raise OnePasswordError(f"Failed to save 1Password item '{title}': {create.stderr.strip()}")
