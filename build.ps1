# Builds a standalone contacts-sync.exe (no venv/Python needed to run it)
# into dist\contacts-sync\. Run this from the dev machine after
# `pip install -e ".[dev,build]"` in .venv.

& "$PSScriptRoot\.venv\Scripts\pyinstaller.exe" "$PSScriptRoot\contacts-sync.spec" --noconfirm
