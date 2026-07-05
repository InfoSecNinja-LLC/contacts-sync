# contacts-sync

Keep Google, iCloud, and Microsoft/Outlook personal contacts in sync using each provider's native API/protocol — no unofficial scraping.

**Status:** actively developed, built for personal use. Use at your own risk — back up your contacts on each provider before your first real sync.

## How it works

A local SQLite database (`contacts.db`) holds a canonical merged copy of every contact plus links to its ID on each service. Each sync run pulls incremental changes from all three providers, merges them (multi-value fields like emails/phones are unioned; single-value fields use newest-edit-wins), and pushes the merged result back out.

Every real (non-dry-run) `sync` appends one line per contact created/updated/deleted to `sync.log` (gitignored) — useful for auditing what a run actually changed or diagnosing an unexpected sync result.

See [`docs/superpowers/specs/2026-07-01-contacts-sync-design.md`](docs/superpowers/specs/2026-07-01-contacts-sync-design.md) for the full design and [`docs/superpowers/plans/2026-07-01-contacts-sync.md`](docs/superpowers/plans/2026-07-01-contacts-sync.md) for the task-by-task implementation plan.

## Setup

1. `pip install -e ".[dev]"`
2. Credentials for all three providers are stored in a local `.env` file (gitignored) in the project root, created automatically the first time you run an `auth` command.
3. **Google**: create OAuth credentials (Desktop app type) in Google Cloud Console for the People API, set the consent screen's publishing status to "In production" (you'll see an "unverified app" warning when you authorize — that's expected for a personal-use app), then run:
   `contacts-sync auth google --client-secrets path/to/client_secrets.json`
4. **Microsoft**: register a public-client app in Entra ID (Azure Portal), enable "Allow public client flows," support "personal Microsoft accounts," then run:
   `contacts-sync auth microsoft --client-id <your-client-id>`
5. **iCloud**: generate an app-specific password at [appleid.apple.com](https://appleid.apple.com), then run:
   `contacts-sync auth icloud`

You need to complete `auth` for **all three** providers before `sync` will do anything — see Known limitations below.

## Usage

```
contacts-sync doctor                                    # check all three providers are reachable
contacts-sync sync --dry-run --microsoft-client-id X     # preview changes without writing
contacts-sync sync --microsoft-client-id X               # run a real sync
contacts-sync review                                     # resolve ambiguous first-time matches
contacts-sync status                                     # see contact counts and sync token state
contacts-sync version                                    # print the installed version
```

Set `CONTACTS_SYNC_MS_CLIENT_ID` in your environment to avoid passing `--microsoft-client-id` every time (it's read as the option's envvar, so `--microsoft-client-id` can be omitted once it's set).

## Known limitations

- **Google and iCloud credentials are checked eagerly before a sync starts** — if either hasn't been set up yet (`contacts-sync auth <provider>` not run), `sync` fails immediately with an error like `No Google credentials found. Run 'contacts-sync auth google' first.`, even if you only care about syncing the other two right now. This is documented, deliberate v1 behavior, not a bug. **Microsoft credentials are checked lazily instead**: a missing/invalid Microsoft credential is not caught up front, so `sync` still runs and syncs Google/iCloud successfully, reporting the Microsoft failure separately in the summary — the same per-provider isolation used for other mid-sync failures (e.g. an expired token, a network error). Run `contacts-sync doctor` to check all three providers' credentials up front without running a real sync.
- Photo sync is not implemented.
- Postal addresses are modeled in the local database (`addresses` table) but no provider adapter reads or writes them yet — this is unimplemented, not just unmerged.
- The iCloud CardDAV addressbook path is hardcoded to Apple's common default (`/carddavhome/addressbooks/card/`) rather than discovered per-account, so it may not work for every Apple ID.
- No scheduler is built in — run `contacts-sync sync` manually or via your OS's task scheduler/cron.

## Development

```
pytest -v
```
