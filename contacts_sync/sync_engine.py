import json
import logging
from dataclasses import dataclass
from contacts_sync.db import Database
from contacts_sync.matcher import match_contact, normalize_email, normalize_phone
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
    def __init__(self, db: Database, adapters: dict):
        self._db = db
        self._adapters = adapters

    def run(self, dry_run: bool = False) -> SyncResult:
        errors = {}
        created = updated = deleted = pending_review = 0
        # Contact ids that were created or modified during THIS run's pull
        # phase. Only these need their changes pushed back out to already-linked
        # providers; untouched contacts are skipped to avoid redundant writes.
        dirty_ids: set[int] = set()

        for name, adapter in self._adapters.items():
            try:
                token = self._db.get_sync_token(name)
                try:
                    change_set = adapter.list_changes(token)
                except SyncTokenExpiredError:
                    change_set = adapter.list_changes(None)

                for change in change_set.changes:
                    contact_id = self._db.get_link(name, change.provider_id)

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
                    stored_etag = self._db.get_link_etag(name, change.provider_id)
                    if change.etag is not None and stored_etag is not None and change.etag == stored_etag:
                        logger.debug(
                            f"SKIP-ECHO provider={name} provider_id={change.provider_id} etag={change.etag}"
                        )
                        continue

                    existing_contact = self._db.get_contact(contact_id)
                    self._merge_into(existing_contact, change, name, dry_run)
                    dirty_ids.add(existing_contact.id)
                    updated += 1
                    if not dry_run:
                        self._db.set_link_etag(name, change.provider_id, change.etag)

                if not dry_run:
                    self._db.set_sync_token(name, change_set.next_sync_token)
            except Exception as exc:
                errors[name] = str(exc)

        if not dry_run:
            self._push_to_providers(errors, dirty_ids)

        return SyncResult(errors, created, updated, deleted, pending_review)

    def _merge_into(self, existing_contact, change, provider_name, dry_run):
        incoming = change.contact
        meta = existing_contact.field_meta

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

        existing_contact.emails = [
            Email(value=v)
            for v in merge_multi_value(
                [e.value for e in existing_contact.emails], [e.value for e in incoming.emails], normalize=normalize_email
            )
        ]
        existing_contact.phones = [
            Phone(value=v)
            for v in merge_multi_value(
                [p.value for p in existing_contact.phones], [p.value for p in incoming.phones], normalize=normalize_phone
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

        logger.info(
            f"UPDATE contact_id={existing_contact.id} provider={provider_name} "
            f"fields=display_name,given_name,family_name,notes,emails,phones"
        )
        if not dry_run:
            self._db.update_contact(existing_contact)

    def _push_to_providers(self, errors, dirty_ids):
        for contact in self._db.list_contacts():
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
