"""Resolves where app data (.env, contacts.db, sync.log) lives on disk.

When running from source (dev/test), data stays next to the current working
directory, exactly as before. When running as a PyInstaller-frozen exe, data
must live next to the exe itself (not whatever directory the user happened to
launch it from) - otherwise a shortcut or PATH invocation silently can't find
.env/contacts.db.
"""

import sys
from pathlib import Path


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path.cwd()
