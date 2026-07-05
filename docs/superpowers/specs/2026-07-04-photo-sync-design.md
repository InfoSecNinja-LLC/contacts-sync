# Photo sync design

## Goal

Sync contact photos across Google, Microsoft, and iCloud, bi-directionally, using the
same newest-edit-wins merge model already used for other single-value fields
(`display_name`, `notes`, etc.). This replaces the currently unused `photo_url` stub
field noted in the README's "Known limitations" section.

## Why a stub field already existed but isn't usable as-is

`CanonicalContact.photo_url` and the `contacts.photo_url` DB column were added but never
wired up, because none of the three providers actually expose a stable, directly-usable
photo URL:

- **Google**: person photos are exposed as URLs, but reading the actual bytes still
  requires an authenticated fetch.
- **Microsoft Graph**: contact photos are a separate binary sub-resource
  (`/me/contacts/{id}/photo/$value`), not part of the contact JSON payload at all.
- **iCloud**: vCard's `PHOTO` property embeds the image as base64 directly in the vCard
  — there's no URL at all.

So the canonical model needs to store photo bytes, not a URL.

## Data model

- `CanonicalContact`: remove `photo_url: Optional[str]`; add:
  - `photo_data: Optional[bytes] = None`
  - `photo_content_type: Optional[str] = None` (e.g. `"image/jpeg"`)
- DB (`db.py`): drop `photo_url TEXT`, add `photo_data BLOB` and `photo_content_type TEXT`
  columns to `contacts` via an `ALTER TABLE` migration in `Database.migrate()`, following
  the same guarded-`ALTER TABLE` pattern already used for `provider_links.etag`.
- `field_meta["photo"]` tracks the last-merge timestamp for the photo field, exactly like
  `field_meta["display_name"]`, so it flows through the existing `merge_single_value`
  newest-edit-wins logic unchanged.

## Change detection strategy

`list_changes` on each adapter is a delta/sync-token pull: it already returns only
contacts that changed since the last sync, not the full contact list every run. Photo
fetching piggybacks on this instead of introducing a separate photo-specific
change-tracking scheme. The extra cost (one additional network call for a contact whose
non-photo field changed, but whose photo didn't) is bounded to already-changed contacts,
consistent with this codebase's existing delta-sync design philosophy — and it's
negligible at personal-use contact volumes.

## Per-provider pull (`list_changes`)

- **iCloud** (`icloud.py`, `_to_canonical`): if the vCard has a `PHOTO` property,
  base64-decode `vcard.photo.value` into `photo_data`; derive `photo_content_type` from
  its `TYPE`/`ENCODING` params, defaulting to `image/jpeg` if unspecified. No network
  change — the vCard is already fully fetched.
- **Google** (`google.py`): add `photos` to `PERSON_FIELDS`. In `_to_canonical`, if
  `person["photos"]` contains a non-`default` entry, issue one authenticated
  `requests.get(url)` to fetch bytes (reusing the existing OAuth bearer token), and set
  `photo_content_type` from the response's `Content-Type` header.
- **Microsoft** (`microsoft.py`): in `list_changes`, for each non-deleted changed item,
  issue one `GET {GRAPH_BASE}/me/contacts/{id}/photo/$value`. A 404 means "no photo" and
  is not an error — just leave `photo_data` as `None`. Set `photo_content_type` from the
  response's `Content-Type` header.

## Per-provider push (`create`/`update`)

- **iCloud** (`_to_vcard`): if `contact.photo_data` is set, add a `PHOTO` property with
  `ENCODING=b` and `TYPE` derived from `photo_content_type`, base64-encoded value. This
  folds into the existing single vCard PUT — no extra call needed.
- **Google**: after `createContact`/`updateContact` succeeds, if `contact.photo_data` is
  set, call `people().updateContactPhoto(resourceName=..., body={"photoBytes": <base64>})`.
  One extra call, only when a photo is present.
- **Microsoft**: after the `POST`/`PATCH` succeeds, if `contact.photo_data` is set, one
  `PUT {GRAPH_BASE}/me/contacts/{id}/photo/$value` with the raw bytes and a matching
  `Content-Type` header.
- No changes to the `ProviderAdapter` protocol (`base.py`) — photo push/pull stays
  internal to each adapter's existing `create`/`update`/`list_changes` implementations.

## Merge (`sync_engine.py`)

- `_merge_into`: add `photo_data`/`photo_content_type` through `merge_single_value` using
  `field_meta["photo"]`, the same pattern as `notes`/`display_name`.
- `_snapshot()` (used to detect whether a merge actually changed anything) includes
  `photo_data`, so a photo-only change correctly marks the contact dirty and triggers
  re-push to providers that don't have it yet.
- No changes to `_push_to_providers` — it already re-runs `create`/`update` for
  dirty/unlinked contacts, which will now also carry the photo through the same call.

## Testing

- Adapter tests mock the extra HTTP call (photo GET/PUT) the same way existing tests
  mock `request_with_retry` / `requests_mock`.
- `merger.py`/`sync_engine.py` tests extend existing fixtures with photo cases: bytes
  present/absent, changed/unchanged, verifying dirty-tracking and newest-wins merge
  behavior.
- No new test infrastructure — follows the existing per-adapter/per-module test file
  conventions.

## Out of scope (v1)

- Image resizing/downscaling before push. Provider size limits (Google ~10MB, Microsoft
  ~4MB) are not enforced; an oversized photo surfaces as a normal per-provider error
  during push, consistent with how other create/update failures are already handled.
- A dedicated `fetch_photo`/`push_photo` method on `ProviderAdapter` — unnecessary
  given photo push/pull is folded into each adapter's existing methods.
