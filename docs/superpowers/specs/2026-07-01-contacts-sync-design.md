# contacts-sync: Design Spec

**Date:** 2026-07-01
**Status:** Approved for implementation

## Goal

A CLI tool, `contacts-sync`, that keeps a personal Google account, an iCloud account, and a personal Microsoft/Outlook.com account in sync: same contact set, fields merged across sources, deletions propagated. Prefers native provider APIs/protocols over screen scraping or unofficial endpoints.

## Provider research summary

- **Google**: People API v1 (`https://people.googleapis.com/v1/`) is the only supported API; the legacy Contacts/GData API is fully retired. OAuth 2.0 "installed app" flow, scope `contacts` for read/write. Keep the OAuth consent screen in **"In production"** publishing status (not "Testing") — Testing-mode refresh tokens and sync tokens both expire after 7 days, forcing constant re-auth. Supports incremental sync via `syncToken` on `people.connections.list` (`requestSyncToken=true`); token itself also expires after 7 days, requiring a full-resync fallback.
- **iCloud**: No REST API exists for third-party access to iCloud Contacts. The only supported path is **CardDAV** (RFC 6352) at `https://contacts.icloud.com/`, authenticated via HTTP Basic Auth using an **app-specific password** (generated at appleid.apple.com), not the Apple ID password. Apple layers custom vCard extensions (`X-ABLabel`, `X-ABRELATEDNAMES`, `X-ABADR`, phonetic-name fields, etc.) on top of standard vCard 3.0/4.0 for labels, related names, and groups — these must round-trip correctly or data is silently lost. Incremental sync via `sync-collection` REPORT (RFC 6578) or cheaper ctag polling. Rate limits are undocumented; conservative backoff is required.
- **Microsoft**: Graph API `/me/contacts` (and `/me/contactFolders/{id}/contacts`), v1.0, GA. Personal Microsoft accounts (outlook.com/hotmail/live) work via **device code flow** against a public-client Entra app registration with "personal Microsoft accounts" enabled — no browser redirect needed, no admin consent required for `Contacts.ReadWrite`. Supports delta queries (`/contacts/delta`) per contact folder for incremental sync; delta tokens can be evicted from Microsoft's cache, returning `syncStateNotFound` and requiring a full resync. Outlook service rate limit: 10,000 requests / 10 min, 4 concurrent requests per app+mailbox.

All three support some form of incremental/delta sync, and none support a real push/webhook model that a CLI tool run on-demand could easily use — so the tool is fundamentally polling-based, run either manually or via an OS scheduler.

## Architecture: hub-and-spoke

A local SQLite database at `contacts-sync/contacts.db` (unencrypted, browsable with any SQLite tool, gitignored) is the hub. It holds:

- A **canonical merged contact record** per person — superset of fields across all three providers.
- A **link table** mapping each canonical contact to its ID on each service (Google `resourceName`, iCloud CardDAV `href` + vCard `UID`, Microsoft Graph `contact.id`).
- A **last-synced snapshot**, per field per source, used to detect what changed since the last run and enable correct 3-way merge (distinguishing "provider A added a field" from "providers A and B disagree").

Each provider gets an **adapter** (`adapters/google.py`, `adapters/icloud.py`, `adapters/microsoft.py`) implementing a shared interface:

```python
class ProviderAdapter(Protocol):
    def list_changes(self, since_token: str | None) -> ChangeSet: ...
    def create(self, contact: CanonicalContact) -> ProviderId: ...
    def update(self, provider_id: ProviderId, contact: CanonicalContact) -> None: ...
    def delete(self, provider_id: ProviderId) -> None: ...
```

All provider-specific quirks (Apple's `X-AB*` extensions, Google's singleton-field restrictions, Graph's folder structure) are translated to/from the canonical model inside the adapter. The sync engine never sees provider-specific shapes.

**Sync engine** (provider-agnostic), per run:

1. Pull changes from each provider using its sync token/delta query/ctag; fall back to a full fetch when a token has expired, logging that this happened.
2. Match newly-seen/unlinked provider contacts to existing canonical records, or create new canonical records.
3. Merge changed fields into canonical records.
4. Push merged canonical state to any provider that's missing it or stale.
5. Propagate deletions.
6. Record the new snapshot and sync tokens for the next run.

A failure pushing to one provider does not abort the run for the others; failures are collected and reported at the end, and `sync` exits non-zero if anything failed.

## Canonical data model

Normalized SQLite tables: `contacts`, `emails`, `phones`, `addresses`, `photos`, `groups`, plus a JSON overflow column for provider-specific fields with no cross-provider equivalent (e.g. Microsoft's `spouseName`/`manager`, Apple's phonetic-name fields) so they aren't lost even though other providers can't represent them.

| Field | Google | iCloud (vCard) | Microsoft Graph |
|---|---|---|---|
| Names | `names` | `N`/`FN` | `givenName`/`surname` |
| Emails (multi) | `emailAddresses` | `EMAIL` | `emailAddresses` |
| Phones (multi) | `phoneNumbers` | `TEL` | `businessPhones`/`mobilePhone` |
| Addresses | `addresses` | `ADR` | `homeAddress`/`businessAddress` |
| Photo | `photos` | `PHOTO` | `photo` |
| Notes | `biographies` | `NOTE` | `personalNotes` |
| Groups/labels | `memberships` | vCard groups | `categories` |
| Org/title | `organizations` | `ORG`/`TITLE` | `companyName`/`jobTitle` |

## Matching logic (first-run linking)

For a provider contact with no existing canonical link, attempt to match an existing canonical record in order:

1. Exact email match
2. Exact phone match (normalized to E.164)
3. Exact full-name match with no conflicting email/phone

Matches that are ambiguous (name-only match with multiple candidates, or multiple canonical records matching) are **not** auto-linked. Instead they're written to a human-readable review file (`contacts-sync/review.md`) listing candidates side by side. `contacts-sync review` walks through these interactively for confirmation.

## Merge logic

Field-level "newest edit wins," using the per-field last-synced snapshot to detect which provider(s) changed a field since the prior run:

- If only one provider changed a field, that value wins.
- If two providers changed the *same* single-value field since last sync, the value from the provider with the more recent `updatedTime`/`lastModifiedDateTime` wins.
- Multi-value fields (emails, phones) are **unioned** rather than overwritten — values are added, deduplicated by normalized form, never silently dropped because one provider's snapshot didn't include them.

## Auth & credential storage

| Provider | Flow | Scope/credential |
|---|---|---|
| Google | OAuth 2.0 installed-app (loopback redirect), consent screen in "In production" | scope `contacts` |
| Microsoft | Device code flow, public-client Entra app registration, personal accounts enabled | scope `Contacts.ReadWrite` |
| iCloud | HTTP Basic Auth over CardDAV | app-specific password from appleid.apple.com |

Secrets (Google refresh token, Microsoft cached MSAL token, iCloud app-specific password) are stored as items in a dedicated 1Password vault (e.g. `contacts-sync`), read at runtime via the `op` CLI (e.g. `op read op://contacts-sync/google/refresh_token`). Initial implementation uses 1Password's **desktop-app integration** (requires the 1Password app unlocked on this machine at run time) rather than a Service Account. This means unattended scheduled runs will fail with a clear error if the app is locked. The `op` CLI wrapper (`auth/onepassword.py`) is the only place that knows how secrets are fetched, so swapping to a Service Account later (when scheduling is added) requires no changes to sync logic.

`contacts-sync auth <provider>` drives the respective flow (opens a browser for Google, prints a device code for Microsoft, prompts for an app-specific password for iCloud) and writes the resulting secret into 1Password via `op item create`.

## CLI surface

```
contacts-sync auth <google|icloud|microsoft>   # one-time credential setup per provider
contacts-sync sync [--dry-run] [--provider=X]  # run a sync cycle
contacts-sync review                            # resolve ambiguous matches from review.md
contacts-sync status                            # last sync time, pending reviews, link counts
contacts-sync doctor                            # check credential validity & connectivity
```

`sync` is idempotent and safe to run repeatedly. `--dry-run` computes and prints planned changes without writing anything — the intended acceptance check before trusting real writes against real accounts.

## Scheduling

Out of scope for the code itself in this iteration. `contacts-sync sync` is a plain command suitable for Windows Task Scheduler (or cron/systemd elsewhere) once the user is ready; no daemon process is built now. Revisiting the 1Password Service Account is expected to happen at the same time scheduling is added.

## Error handling & rate limits

- Exponential backoff + retry on 429/5xx per adapter, respecting `Retry-After` where provided (Google, Microsoft); conservative backoff for iCloud, which publishes no limits.
- Google mutations within a run are sent sequentially (per Google's own guidance against parallel mutations for one user).
- Expired sync tokens/delta tokens/ctags are caught explicitly, trigger a one-time full resync for that provider, and are logged clearly.
- All writes are logged to `contacts-sync/sync.log` (contact name/id, field changed, provider, timestamp) for manual audit or reversal.

## Tech stack

Python 3.12+. Libraries: `google-api-python-client` (Google), `msgraph-sdk` + `azure-identity` (Microsoft, device code flow via `DeviceCodeCredential`), `caldav`/`vobject` (iCloud CardDAV + vCard parsing), `typer` (CLI), stdlib `sqlite3` (state store).

## Repo structure

```
contacts-sync/
  contacts_sync/
    adapters/
      base.py          # shared adapter Protocol
      google.py
      icloud.py
      microsoft.py
    models.py           # canonical contact dataclasses
    db.py               # SQLite schema + access layer
    matcher.py           # cross-provider matching logic
    merger.py            # field-level merge logic
    sync_engine.py        # orchestrates pull -> match -> merge -> push
    auth/
      google_auth.py
      microsoft_auth.py
      icloud_auth.py
      onepassword.py      # `op` CLI wrapper
    cli.py                # Typer app: auth/sync/review/status/doctor
  tests/
    fixtures/             # recorded API responses per provider for offline tests
    test_matcher.py
    test_merger.py
    test_adapters_*.py
  contacts.db              # gitignored, unencrypted SQLite
  pyproject.toml
  README.md
  .gitignore
```

## Testing strategy

- Unit tests for `matcher.py` and `merger.py` (pure logic, no network) get the heaviest coverage since correctness there matters most.
- Adapter tests run against recorded/fixture responses — no live API calls in CI, avoiding the need for real credentials in GitHub Actions and avoiding burning real API quota.
- A manual `--dry-run` pass against real accounts is the acceptance test before trusting real writes.
- No live-account integration tests in CI; none of the three providers offer a sandbox/test-account model.
