# contacts-sync: Clean Re-Sync (ETag-Based Echo Suppression) Design

**Date:** 2026-07-03
**Status:** Approved for implementation
**Context:** Live full sync succeeded at initial population (all contacts on all 3 providers) but re-running the sync is not idempotent. Two problems, both found via real runs:

1. **Echo:** After pushing N contacts to a provider, that provider's next incremental pull (`delta`/`sync-collection`/`connections.list` with a sync token) returns our own writes as "changes." The engine re-processes them (match → merge → mark dirty → re-push), producing ~900 redundant writes every run.
2. **Stale-link 404s:** Some stored provider IDs no longer resolve (contacts the provider deduped/deleted server-side). `update()` returns 404 "not found," which currently errors the whole provider for the rest of the run.

## Goal

Make `contacts-sync sync` idempotent and cheap on re-run: a run with no genuine external changes should do near-zero writes and exit 0.

## Mechanism 1: ETag-based echo suppression

Every provider gives each contact resource a version identifier that changes **only** when the resource actually changes:
- **Google People API:** `Person.etag`
- **Microsoft Graph:** `@odata.etag` (or `lastModifiedDateTime` as fallback)
- **iCloud CardDAV:** the `getetag` (DAV:getetag) per resource, and the `ETag` header returned on `PUT`.

The engine records, per provider resource, the ETag it last observed/wrote. On a subsequent pull:
- If a pulled change's ETag **equals** the stored ETag for that `(provider, provider_id)` → it's an echo of our own write, or simply unchanged → **skip** (do not merge, do not mark dirty, do not re-push).
- If the ETag **differs** (or there is no stored ETag) → genuine change → process normally, then store the new ETag.

On push:
- `create()` and `update()` return the ETag the provider assigns to our write. The engine stores that ETag for the `(provider, provider_id)`, so the inevitable echo on the next pull is recognized and suppressed.

This survives lossy round-trips because it compares the provider's own opaque version token, never our reconstructed content.

## Interface changes

`contacts_sync/adapters/base.py`:
- `ChangedContact` gains `etag: Optional[str] = None` (the resource's current ETag as seen in the pull).
- `ProviderAdapter.create(contact) -> tuple[str, Optional[str]]` — returns `(provider_id, etag)`.
- `ProviderAdapter.update(provider_id, contact) -> Optional[str]` — returns the new `etag`.

Each adapter:
- Populates `ChangedContact.etag` from the provider's per-resource version in `list_changes`.
- Returns the ETag from `create`/`update`. Google: from the `createContact`/`updateContact` response's `etag`. Microsoft: from the response `@odata.etag`. iCloud: from the `ETag` response header on `PUT` (CardDAV servers return it; if absent, return `None` and the next pull will backfill it — the engine tolerates `None`).

## Storage

`provider_links` gains an `etag TEXT` column (nullable). New `Database` methods:
- `set_link_etag(provider, provider_id, etag)`
- `get_link_etag(provider, provider_id) -> Optional[str]`

(Existing `link_provider` continues to work; etag defaults to `NULL`. A migration adds the column with `ALTER TABLE ... ADD COLUMN etag TEXT` guarded so it's a no-op if already present.)

## Engine changes (`sync_engine.py`)

Pull loop, for a change that maps to an existing linked contact:
- Compare `change.etag` to `db.get_link_etag(name, change.provider_id)`. If equal and non-`None` → skip entirely (no merge, not added to `dirty_ids`). Else → merge as today, then `db.set_link_etag(name, change.provider_id, change.etag)`.
- For a newly created/linked contact from a pull, store `change.etag` for its link.

Push (`_push_to_providers`):
- On `create()`, capture returned `(provider_id, etag)`, link, and `set_link_etag`.
- On `update()`, capture returned `etag` and `set_link_etag` so the next pull's echo is suppressed.

## Mechanism 2: Stale-link 404 handling

When `update()` (or `delete()`) raises a 404 "not found":
- The adapter surfaces this as a distinct, catchable condition (a `ProviderResourceGoneError` raised by the adapter on 404, subclass of a shared adapter error).
- The engine catches it during push, calls `db.unlink_provider(provider_id)` for that `(provider, provider_id)`, logs a clear `STALE-LINK` line, and continues with other contacts/providers (does **not** mark the whole provider errored).
- No automatic recreate and no automatic delete-propagation from a push-404 (too ambiguous to infer intent from a push failure). A future run's catch-up (`unlinked → create`) will re-create the contact on that provider only if it has no remaining link there — which correctly avoids duplicating the merged-Google-duplicate case (those retain their other link).

New `Database` method: `unlink_provider(provider, provider_id)`.

## Explicitly out of scope (documented follow-ups)

- Auto-propagating a push-404 as a deletion to other providers.
- A `--full` reconcile flag that re-pushes everything regardless of dirty/etag state (useful as an occasional repair tool; not needed for normal operation once echo suppression works).
- Photo/address/org/title/groups field sync (unchanged from prior scope).

## Testing strategy

- Adapter unit tests: `create`/`update` return the etag; `list_changes` populates `ChangedContact.etag`; `update`/`delete` raise `ProviderResourceGoneError` on 404.
- DB tests: etag get/set roundtrip; `unlink_provider`; migration idempotency.
- Engine tests: a pulled change whose etag matches the stored etag is skipped (not merged, not dirtied, not re-pushed); a differing etag is processed; push stores the returned etag; a 404 on push drops the link and doesn't error the provider.
- All via mocks; no live-account calls in the suite. Final validation is a real `sync` run: first run consumes the outstanding echo (with etags now recorded), the immediately-following run should report ~0 updated and exit 0.
