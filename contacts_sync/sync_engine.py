import json
import logging
from dataclasses import dataclass
from contacts_sync.db import Database
from contacts_sync.matcher import canonicalize_phone, match_contact, normalize_email, normalize_phone
from contacts_sync.merger import merge_single_value, merge_multi_value
from contacts_sync.models import Email, Phone
from contacts_sync.adapters.base import ProviderResourceGoneError, SyncTokenExpiredError

logger = logging.getLogger("contacts_sync.sync")


def _contact_to_review_json(contact) -> str:
    return json.dumps({
        "display_name": contact.display_name,
        "emails": [e.value for e in contact.emails],
        "phones": [p.value for p in contact.phones],
    })


@dataclass
class SyncResult:
    provider_errors: dict
    created: int = 0
    updated: int = 0
    deleted: int = 0
    pending_review: int = 0


class SyncEngine:
    def __init__(self, db: Database, adapters: dict, progress=None):
        self._db = db
        self._adapters = adapters
        # Optional progress hook: a callable `(event: str, **kwargs)` invoked
        # at pull/merge/push milestones so a UI (e.g. the CLI's rich bars) can
        # display progress. Defaults to a no-op so library callers and tests
        # don't have to care. Events:
        #   pull_start(provider)         - provider pull (network) beginning
        #   pull_done(provider, total)   - pull finished; `total` changes to merge
        #   change_done(provider)        - one pulled change processed
        #   provider_error(provider)     - provider aborted with an error
        #   push_start(total)            - push phase beginning over `total` contacts
        #   push_advance()               - one contact's push processed
        self._progress = progress if progress is not None else (lambda event, **kwargs: None)

    def run(self, dry_run: bool = False) -> SyncResult:
        errors = {}
        created = updated = deleted = pending_review = 0
        # Contact ids that were created or modified during THIS run's pull
        # phase. Only these need their changes pushed back out to already-linked
        # providers; untouched contacts are skipped to avoid redundant writes.
        dirty_ids: set[int] = set()

        for name, adapter in self._adapters.items():
            try:
                self._progress("pull_start", provider=name)
                token = self._db.get_sync_token(name)
                try:
                    change_set = adapter.list_changes(token)
                except SyncTokenExpiredError:
                    change_set = adapter.list_changes(None)
                self._progress("pull_done", provider=name, total=len(change_set.changes))

                for change in change_set.changes:
                    self._progress("change_done", provider=name)
                    contact_id = self._db.get_link(name, change.provider_id)
                    # Capture the previously-stored etag NOW, before any code
                    # below records this change's etag. Echo suppression must
                    # compare against what we knew BEFORE this change arrived;
                    # reading it after the first-time-match branch has already
                    # stored change.etag would make every first match look like
                    # an echo of itself, silently dropping the provider's data
                    # (this exact bug once discarded structured names and
                    # photos from the second provider on initial sync).
                    pre_existing_etag = (
                        self._db.get_link_etag(name, change.provider_id) if contact_id is not None else None
                    )

                    if change.deleted:
                        if contact_id:
                            if not dry_run:
                                links = self._db.get_links_for_contact(contact_id)
                                for other_name, other_provider_id in links.items():
                                    if other_name == name:
                                        continue
                                    other_adapter = self._adapters.get(other_name)
                                    if other_adapter is None:
                                        continue
                                    try:
                                        other_adapter.delete(other_provider_id)
                                    except Exception as exc:
                                        errors[other_name] = str(exc)
                                self._db.delete_contact(contact_id)
                            deleted += 1
                            logger.info(f"DELETE contact_id={contact_id} provider={name} provider_id={change.provider_id}")
                        continue

                    if contact_id is None:
                        existing = self._db.list_contacts()
                        match = match_contact(change.contact, existing)
                        if match.status == "matched":
                            contact_id = match.contact_id
                            if not dry_run:
                                self._db.link_provider(contact_id, name, change.provider_id)
                                self._db.set_link_etag(name, change.provider_id, change.etag)
                        elif match.status == "ambiguous":
                            pending_review += 1
                            if not dry_run:
                                self._db.save_pending_match(
                                    name,
                                    change.provider_id,
                                    match.candidate_ids,
                                    _contact_to_review_json(change.contact),
                                )
                            continue
                        else:
                            created += 1
                            logger.info(f'CREATE contact="{change.contact.display_name}" provider={name} provider_id={change.provider_id}')
                            if not dry_run:
                                contact_id = self._db.create_contact(change.contact)
                                self._db.link_provider(contact_id, name, change.provider_id)
                                self._db.set_link_etag(name, change.provider_id, change.etag)
                                dirty_ids.add(contact_id)
                            continue

                    # Echo suppression: if this change carries the same etag we
                    # last observed/wrote for the resource, it's either our own
                    # write coming back or an unchanged resource. Skip it
                    # entirely so it never becomes dirty and never gets re-pushed.
                    # Compares against the etag captured BEFORE this change was
                    # processed - a link established earlier in this very loop
                    # iteration (first-time match) has pre_existing_etag=None and
                    # must still be merged.
                    if change.etag is not None and pre_existing_etag is not None and change.etag == pre_existing_etag:
                        logger.debug(
                            f"SKIP-ECHO provider={name} provider_id={change.provider_id} etag={change.etag}"
                        )
                        continue

                    existing_contact = self._db.get_contact(contact_id)
                    changed = self._merge_into(existing_contact, change, name, dry_run)
                    if changed:
                        dirty_ids.add(existing_contact.id)
                        updated += 1
                    # Always record the resource's current etag - even for a
                    # no-op merge - so its echo is suppressed on the next pull.
                    if not dry_run:
                        self._db.set_link_etag(name, change.provider_id, change.etag)

                if not dry_run:
                    self._db.set_sync_token(name, change_set.next_sync_token)
            except Exception as exc:
                errors[name] = str(exc)
                self._progress("provider_error", provider=name)

        if not dry_run:
            self._push_to_providers(errors, dirty_ids)

        return SyncResult(errors, created, updated, deleted, pending_review)

    def _merge_into(self, existing_contact, change, provider_name, dry_run) -> bool:
        """Merge an incoming change into an existing canonical contact.

        Returns True only if the merge actually changed the canonical contact's
        data. A no-op merge (a pulled "change" whose data we already hold -
        common when a provider re-reports a resource, e.g. its own dedup bumped
        an etag) returns False so the caller doesn't mark the contact dirty and
        needlessly re-push it, which would just provoke another echo.
        """
        incoming = change.contact
        meta = existing_contact.field_meta

        def _snapshot(c):
            return (
                c.display_name,
                c.notes,
                c.given_name,
                c.family_name,
                sorted(e.value for e in c.emails),
                sorted(p.value for p in c.phones),
                c.photo_data,
            )

        before = _snapshot(existing_contact)

        new_name, new_name_meta = merge_single_value(
            existing_contact.display_name, meta.get("display_name"), incoming.display_name, change.updated_at,
        )
        existing_contact.display_name = new_name
        meta["display_name"] = new_name_meta

        new_notes, new_notes_meta = merge_single_value(
            existing_contact.notes, meta.get("notes"), incoming.notes, change.updated_at,
        )
        existing_contact.notes = new_notes
        meta["notes"] = new_notes_meta

        new_given_name, new_given_name_meta = merge_single_value(
            existing_contact.given_name, meta.get("given_name"), incoming.given_name, change.updated_at,
        )
        existing_contact.given_name = new_given_name
        meta["given_name"] = new_given_name_meta

        new_family_name, new_family_name_meta = merge_single_value(
            existing_contact.family_name, meta.get("family_name"), incoming.family_name, change.updated_at,
        )
        existing_contact.family_name = new_family_name
        meta["family_name"] = new_family_name_meta

        new_photo_data, new_photo_meta = merge_single_value(
            existing_contact.photo_data, meta.get("photo"), incoming.photo_data, change.updated_at,
        )
        if new_photo_data != existing_contact.photo_data:
            # photo_content_type always travels with whichever photo_data value
            # won the merge - it's metadata about that value, not an
            # independently-mergeable field.
            existing_contact.photo_content_type = incoming.photo_content_type
        existing_contact.photo_data = new_photo_data
        meta["photo"] = new_photo_meta

        existing_contact.emails = [
            Email(value=v)
            for v in merge_multi_value(
                [e.value for e in existing_contact.emails], [e.value for e in incoming.emails], normalize=normalize_email
            )
        ]
        # Canonicalize phones to a single stable representation before merging
        # so the same number in two source formats collapses to one value -
        # otherwise the push/pull round-trip never converges (the provider
        # dedupes on write, so we keep re-detecting a "change").
        existing_contact.phones = [
            Phone(value=v)
            for v in merge_multi_value(
                [canonicalize_phone(p.value) for p in existing_contact.phones],
                [canonicalize_phone(p.value) for p in incoming.phones],
                normalize=normalize_phone,
            )
        ]

        existing_contact.field_meta = meta

        # Propagate provider-specific passthrough data (e.g. a freshly-pulled
        # Google etag) from the incoming change into the existing canonical
        # record. Without this, a contact created once and never re-merged
        # would keep whatever (possibly now-stale) `extra` it got at creation
        # time, and a later push could send a stale etag to the provider.
        # Only merge truthy values so a partial/malformed response that omits a
        # key (e.g. a missing etag surfacing as None) can't clobber a good value.
        existing_contact.extra.update({k: v for k, v in incoming.extra.items() if v})

        changed = _snapshot(existing_contact) != before
        # Always persist (cheap, idempotent) so refreshed passthrough data like
        # the Google etag in `extra` is saved even when the visible fields
        # didn't change - but only log/return "changed" for a real data change,
        # so the caller re-pushes only when there's genuinely something new.
        if not dry_run:
            self._db.update_contact(existing_contact)
        if changed:
            logger.info(
                f"UPDATE contact_id={existing_contact.id} provider={provider_name} "
                f"fields=display_name,given_name,family_name,notes,emails,phones"
            )
        return changed

    def push_contacts(self, contact_ids) -> dict:
        """Push the given locally-modified contacts to every linked provider
        (and create any contact on providers it isn't linked to yet).

        `run` only pushes contacts dirtied by its own pull phase, so repair
        commands that edit the local store directly (e.g. fix-names) must
        call this to get their changes out - otherwise the edits would sit in
        the database forever, since the next pull would see no provider-side
        change and never mark the contacts dirty.
        """
        errors: dict = {}
        self._push_to_providers(errors, set(contact_ids))
        return errors

    def _push_to_providers(self, errors, dirty_ids):
        contacts = self._db.list_contacts()
        self._progress("push_start", total=len(contacts))
        for contact in contacts:
            self._progress("push_advance")
            links = self._db.get_links_for_contact(contact.id)
            for name, adapter in self._adapters.items():
                if name in errors:
                    continue
                try:
                    if name not in links:
                        # Not yet linked to this provider: create it there.
                        # This is catch-up and always runs, even for contacts
                        # that weren't touched this run, so a partially-failed
                        # previous sync can still be completed.
                        provider_id, etag = adapter.create(contact)
                        self._db.link_provider(contact.id, name, provider_id)
                        # Record the etag our write produced so the echo of this
                        # create on the next pull is recognized and suppressed.
                        self._db.set_link_etag(name, provider_id, etag)
                    elif contact.id in dirty_ids:
                        # Already linked AND changed this run: propagate.
                        etag = adapter.update(links[name], contact)
                        # Record the etag our write produced so the echo of this
                        # update on the next pull is recognized and suppressed.
                        self._db.set_link_etag(name, links[name], etag)
                    # else: already linked and unchanged this run -> skip; no
                    # network call needed.
                except ProviderResourceGoneError as exc:
                    # The resource we hold a link to no longer exists on the
                    # provider (deduped/deleted server-side). Drop just this
                    # stale link and keep going - do NOT mark the whole provider
                    # errored, so other contacts still sync this run. A future
                    # run's catch-up will re-create the contact there only if it
                    # has no remaining link to this provider.
                    self._db.unlink_provider(name, links[name])
                    logger.info(
                        f"STALE-LINK dropped contact_id={contact.id} provider={name} "
                        f"provider_id={links[name]} ({exc})"
                    )
                except Exception as exc:
                    errors[name] = str(exc)
