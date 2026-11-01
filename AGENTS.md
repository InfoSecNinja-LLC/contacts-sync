# Repository Guidelines

`contacts-sync` is a packaged Python CLI that keeps Google, iCloud, and Microsoft/Outlook personal contacts in sync via each provider's native API. A local SQLite database (`contacts.db`) holds the canonical merged copy. Built for personal use on Windows.

## Project Structure & Module Organization

- `contacts_sync/` — the package. `cli.py` (Typer entry point), `db.py`, `models.py`, `matcher.py`, `merger.py`, `sync_engine.py`, `names.py`, `paths.py`, `http_retry.py`, plus `adapters/` (per-provider Google/iCloud/Microsoft) and `auth/` (per-provider auth flows).
- `tests/` — `pytest` suite mirroring the modules (`test_sync_engine.py`, `test_matcher.py`, `test_*_adapter.py`, `test_*_auth.py`, etc.).
- `docs/` — design spec and implementation plan under `docs/superpowers/`.
- `contacts-sync.spec` + `build.ps1` + `pyinstaller_entry.py` — PyInstaller standalone build.
- Runtime/local (gitignored): `.env` (credentials), `contacts.db`, `sync.log`, `.venv/`, `dist/`, `build/`.

## Build, Test, and Development Commands

Install for development (editable, with dev + build extras):

```powershell
pip install -e ".[dev]"
pip install -e ".[dev,build]"   # also pulls PyInstaller for standalone builds
```

Run the CLI:

```powershell
contacts-sync doctor                     # check all three providers reachable
contacts-sync sync --dry-run             # preview changes, write nothing
contacts-sync sync                       # real sync
contacts-sync status                     # contact counts + sync token state
```

Run the tests:

```powershell
pytest -v
```

Build the standalone Windows exe (into `dist\contacts-sync\`):

```powershell
./build.ps1
```

`build.ps1` invokes `.venv\Scripts\pyinstaller.exe contacts-sync.spec --noconfirm`, so set up `.venv` with `pip install -e ".[dev,build]"` first.

## Coding Style & Naming Conventions

- Python 3.12+ (`requires-python = ">=3.12"`), 4-space indentation, standard PEP 8.
- `snake_case` for modules, functions, and variables; `PascalCase` for classes.
- CLI built with Typer + Rich; keep new subcommands consistent with `cli.py`.
- Tests are named `test_<module>.py` under `tests/` and mirror the package layout.

## Testing Guidelines

- Framework: `pytest` with `pytest-mock` and `requests-mock` (`[dev]` extra). `pyproject.toml` sets `pythonpath = ["."]`.
- Provider adapters and auth are tested against mocked HTTP — no live credentials or network needed.
- Run `pytest -v` before committing; add tests alongside the module you change.

## Commit & Pull Request Guidelines

- Keep commits focused and messages descriptive. Bump `version` in `pyproject.toml` for user-facing changes (currently `0.2.2`).
- Ensure `pytest -v` passes and never commit `.env`, `contacts.db`, `sync.log`, or `dist/`.

## Security & Configuration Tips

- Provider credentials live in a local `.env` in the project root — **gitignored, never commit it**. It is created automatically the first time you run an `auth` command.
- Set up all three providers with `contacts-sync auth google|microsoft|icloud` before `sync`. iCloud uses an app-specific password; Microsoft uses a public-client `--microsoft-client-id` (also read from `MICROSOFT_CLIENT_ID`).
- `contacts.db` and `sync.log` contain real personal contact data — keep them out of version control.
- Always `sync --dry-run` before a first real sync, and back up contacts on each provider beforehand.
