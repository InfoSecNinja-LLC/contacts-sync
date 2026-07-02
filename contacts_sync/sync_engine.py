import json
import logging
from dataclasses import dataclass
from contacts_sync.db import Database
from contacts_sync.matcher import match_contact, normalize_email, normalize_phone
from contacts_sync.merger import merge_single_value, merge_multi_value
from contacts_sync.models import Email, Phone
from contacts_sync.adapters.base import SyncTokenExpiredError

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
                        if contact_id and not dry_run:
                            self._db.delete_contact(contact_id)
                        if contact_id:
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
                            continue

                    existing_contact = self._db.get_contact(contact_id)
                    self._merge_into(existing_contact, change, name, dry_run)
                    updated += 1

                if not dry_run:
                    self._db.set_sync_token(name, change_set.next_sync_token)
            except Exception as exc:
                errors[name] = str(exc)

        if not dry_run:
            self._push_to_providers(errors)

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
        logger.info(
            f"UPDATE contact_id={existing_contact.id} provider={provider_name} "
            f"fields=display_name,notes,emails,phones"
        )
        if not dry_run:
            self._db.update_contact(existing_contact)

    def _push_to_providers(self, errors):
        for contact in self._db.list_contacts():
            links = self._db.get_links_for_contact(contact.id)
            for name, adapter in self._adapters.items():
                if name in errors:
                    continue
                try:
                    if name not in links:
                        provider_id = adapter.create(contact)
                        self._db.link_provider(contact.id, name, provider_id)
                    else:
                        adapter.update(links[name], contact)
                except Exception as exc:
                    errors[name] = str(exc)
