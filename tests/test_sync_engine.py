import pytest
from contacts_sync.db import Database
from contacts_sync.sync_engine import SyncEngine
from contacts_sync.adapters.base import ChangeSet, ChangedContact, SyncTokenExpiredError
from contacts_sync.models import CanonicalContact, Email

@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "contacts.db"))
    database.migrate()
    return database


class FakeAdapter:
    def __init__(
        self,
        name,
        changes=None,
        next_token="tok-1",
        raise_expired_once=False,
        create_etag="etag-created",
        update_etag="etag-updated",
    ):
        self.name = name
        self._changes = changes or []
        self._next_token = next_token
        self._raise_expired_once = raise_expired_once
        self._create_etag = create_etag
        self._update_etag = update_etag
        self.created = []
        self.updated = []
        self.deleted = []

    def list_changes(self, since_token):
        if self._raise_expired_once and since_token is not None:
            self._raise_expired_once = False
            raise SyncTokenExpiredError("expired")
        return ChangeSet(changes=self._changes, next_sync_token=self._next_token)

    def create(self, contact):
        provider_id = f"{self.name}-new-{len(self.created)}"
        self.created.append(contact)
        return provider_id, self._create_etag

    def update(self, provider_id, contact):
        self.updated.append((provider_id, contact))
        return self._update_etag

    def delete(self, provider_id):
        self.deleted.append(provider_id)


def test_new_contact_from_one_provider_is_created_and_pushed_to_others(db):
    incoming = ChangedContact(
        provider_id="g-1", contact=CanonicalContact(display_name="Jane", emails=[Email(value="jane@e.com")]),
        updated_at="2026-01-01T00:00:00Z",
    )
    google = FakeAdapter("google", changes=[incoming])
    microsoft = FakeAdapter("microsoft")
    engine = SyncEngine(db, {"google": google, "microsoft": microsoft})

    result = engine.run()

    assert result.created == 1
    assert len(microsoft.created) == 1
    assert microsoft.created[0].display_name == "Jane"


def test_matched_contact_merges_emails_across_providers(db):
    existing_id = db.create_contact(CanonicalContact(display_name="Jane", emails=[Email(value="jane@e.com")]))
    db.link_provider(existing_id, "google", "g-1")

    incoming = ChangedContact(
        provider_id="ms-1",
        contact=CanonicalContact(
            display_name="Jane",
            emails=[Email(value="jane@e.com"), Email(value="jane.doe@work.com")],
        ),
        updated_at="2026-01-02T00:00:00Z",
    )
    google = FakeAdapter("google")
    microsoft = FakeAdapter("microsoft", changes=[incoming])
    engine = SyncEngine(db, {"google": google, "microsoft": microsoft})

    result = engine.run()

    updated_contact = db.get_contact(existing_id)
    emails = {e.value for e in updated_contact.emails}
    assert emails == {"jane@e.com", "jane.doe@work.com"}
    assert result.updated == 1


def test_matched_contact_merges_structured_name_from_incoming(db):
    existing_id = db.create_contact(
        CanonicalContact(
            display_name="Jane", given_name="Jane", family_name=None, emails=[Email(value="jane@e.com")]
        )
    )
    db.link_provider(existing_id, "google", "g-1")

    incoming = ChangedContact(
        provider_id="ms-1",
        contact=CanonicalContact(
            display_name="Jane Smith",
            given_name="Jane",
            family_name="Smith",
            emails=[Email(value="jane@e.com")],
        ),
        updated_at="2026-01-02T00:00:00Z",
    )
    google = FakeAdapter("google")
    microsoft = FakeAdapter("microsoft", changes=[incoming])
    engine = SyncEngine(db, {"google": google, "microsoft": microsoft})

    result = engine.run()

    updated_contact = db.get_contact(existing_id)
    assert updated_contact.given_name == "Jane"
    assert updated_contact.family_name == "Smith"
    assert result.updated == 1


def test_merge_into_propagates_extra_from_incoming_change(db):
    existing_id = db.create_contact(
        CanonicalContact(display_name="Jane", emails=[Email(value="jane@e.com")], extra={"google_etag": "stale"})
    )
    db.link_provider(existing_id, "google", "g-1")

    incoming = ChangedContact(
        provider_id="g-1",
        contact=CanonicalContact(
            display_name="Jane",
            emails=[Email(value="jane@e.com")],
            extra={"google_etag": "fresh"},
        ),
        updated_at="2026-01-02T00:00:00Z",
    )
    google = FakeAdapter("google", changes=[incoming])
    engine = SyncEngine(db, {"google": google})

    engine.run()

    updated_contact = db.get_contact(existing_id)
    assert updated_contact.extra["google_etag"] == "fresh"


def test_deleted_contact_is_removed_locally(db):
    existing_id = db.create_contact(CanonicalContact(display_name="Jane"))
    db.link_provider(existing_id, "google", "g-1")
    deletion = ChangedContact(provider_id="g-1", contact=None, updated_at="", deleted=True)
    google = FakeAdapter("google", changes=[deletion])
    engine = SyncEngine(db, {"google": google})

    result = engine.run()

    assert db.get_contact(existing_id) is None
    assert result.deleted == 1


def test_delete_propagates_to_other_linked_providers(db):
    existing_id = db.create_contact(CanonicalContact(display_name="Jane"))
    db.link_provider(existing_id, "google", "g-1")
    db.link_provider(existing_id, "microsoft", "ms-1")
    db.link_provider(existing_id, "icloud", "ic-1")

    deletion = ChangedContact(provider_id="g-1", contact=None, updated_at="", deleted=True)
    google = FakeAdapter("google", changes=[deletion])
    microsoft = FakeAdapter("microsoft")
    icloud = FakeAdapter("icloud")
    engine = SyncEngine(db, {"google": google, "microsoft": microsoft, "icloud": icloud})

    result = engine.run()

    assert microsoft.deleted == ["ms-1"]
    assert icloud.deleted == ["ic-1"]
    assert google.deleted == []
    assert db.get_contact(existing_id) is None
    assert result.deleted == 1


def test_dry_run_does_not_propagate_deletes(db):
    existing_id = db.create_contact(CanonicalContact(display_name="Jane"))
    db.link_provider(existing_id, "google", "g-1")
    db.link_provider(existing_id, "microsoft", "ms-1")

    deletion = ChangedContact(provider_id="g-1", contact=None, updated_at="", deleted=True)
    google = FakeAdapter("google", changes=[deletion])
    microsoft = FakeAdapter("microsoft")
    engine = SyncEngine(db, {"google": google, "microsoft": microsoft})

    result = engine.run(dry_run=True)

    assert microsoft.deleted == []
    assert db.get_contact(existing_id) is not None
    assert result.deleted == 1


def test_delete_propagation_failure_is_isolated_to_other_provider(db):
    class ExplodingDeleteAdapter(FakeAdapter):
        def delete(self, provider_id):
            raise RuntimeError("delete boom")

    existing_id = db.create_contact(CanonicalContact(display_name="Jane"))
    db.link_provider(existing_id, "google", "g-1")
    db.link_provider(existing_id, "microsoft", "ms-1")

    deletion = ChangedContact(provider_id="g-1", contact=None, updated_at="", deleted=True)
    google = FakeAdapter("google", changes=[deletion])
    microsoft = ExplodingDeleteAdapter("microsoft")
    engine = SyncEngine(db, {"google": google, "microsoft": microsoft})

    result = engine.run()

    assert "microsoft" in result.provider_errors
    assert "delete boom" in result.provider_errors["microsoft"]
    assert "google" not in result.provider_errors
    assert db.get_contact(existing_id) is None
    assert result.deleted == 1


def test_ambiguous_match_is_recorded_for_review(db):
    db.create_contact(CanonicalContact(display_name="Jane Smith"))
    db.create_contact(CanonicalContact(display_name="Jane Smith"))
    incoming = ChangedContact(
        provider_id="g-1", contact=CanonicalContact(display_name="Jane Smith"), updated_at="2026-01-01T00:00:00Z",
    )
    google = FakeAdapter("google", changes=[incoming])
    engine = SyncEngine(db, {"google": google})

    result = engine.run()

    assert result.pending_review == 1


def test_ambiguous_match_stores_contact_data_for_review(db):
    db.create_contact(CanonicalContact(display_name="Jane Smith", emails=[Email(value="jane@ambiguous.com")]))
    db.create_contact(CanonicalContact(display_name="Jane Smith Two", emails=[Email(value="jane@ambiguous.com")]))
    incoming = ChangedContact(
        provider_id="g-1",
        contact=CanonicalContact(display_name="Jane Smith", emails=[Email(value="jane@ambiguous.com")]),
        updated_at="2026-01-01T00:00:00Z",
    )
    google = FakeAdapter("google", changes=[incoming])
    engine = SyncEngine(db, {"google": google})

    engine.run()

    pending = db.list_pending_matches()
    assert len(pending) == 1
    assert pending[0]["contact_data"]["display_name"] == "Jane Smith"
    assert pending[0]["contact_data"]["emails"] == ["jane@ambiguous.com"]


def test_expired_sync_token_triggers_full_resync(db):
    google = FakeAdapter("google", raise_expired_once=True)
    db.set_sync_token("google", "stale-token")
    engine = SyncEngine(db, {"google": google})

    result = engine.run()

    assert "google" not in result.provider_errors


def test_dry_run_does_not_write_anything(db):
    incoming = ChangedContact(
        provider_id="g-1", contact=CanonicalContact(display_name="Jane"), updated_at="2026-01-01T00:00:00Z",
    )
    google = FakeAdapter("google", changes=[incoming])
    engine = SyncEngine(db, {"google": google})

    result = engine.run(dry_run=True)

    assert result.created == 1
    assert db.list_contacts() == []


def test_unchanged_already_linked_contact_is_not_repushed(db):
    existing_id = db.create_contact(CanonicalContact(display_name="Jane", emails=[Email(value="jane@e.com")]))
    db.link_provider(existing_id, "google", "g-1")
    db.link_provider(existing_id, "microsoft", "ms-1")

    google = FakeAdapter("google")
    microsoft = FakeAdapter("microsoft")
    engine = SyncEngine(db, {"google": google, "microsoft": microsoft})

    result = engine.run()

    assert google.updated == []
    assert microsoft.updated == []
    assert result.updated == 0


def test_dirty_contact_is_pushed_to_all_providers(db):
    existing_id = db.create_contact(CanonicalContact(display_name="Jane", emails=[Email(value="jane@e.com")]))
    db.link_provider(existing_id, "google", "g-1")
    db.link_provider(existing_id, "microsoft", "ms-1")

    incoming = ChangedContact(
        provider_id="g-1",
        contact=CanonicalContact(
            display_name="Jane",
            emails=[Email(value="jane@e.com"), Email(value="jane.new@e.com")],
        ),
        updated_at="2026-01-02T00:00:00Z",
    )
    google = FakeAdapter("google", changes=[incoming])
    microsoft = FakeAdapter("microsoft")
    engine = SyncEngine(db, {"google": google, "microsoft": microsoft})

    engine.run()

    updated_provider_ids = {pid for pid, _ in google.updated} | {pid for pid, _ in microsoft.updated}
    assert updated_provider_ids == {"g-1", "ms-1"}


def test_unlinked_provider_gets_create_even_if_not_dirty(db):
    existing_id = db.create_contact(CanonicalContact(display_name="Jane", emails=[Email(value="jane@e.com")]))
    db.link_provider(existing_id, "google", "g-1")

    google = FakeAdapter("google")
    microsoft = FakeAdapter("microsoft")
    engine = SyncEngine(db, {"google": google, "microsoft": microsoft})

    engine.run()

    assert len(microsoft.created) == 1
    assert microsoft.created[0].display_name == "Jane"
    assert google.updated == []


def test_pulled_change_with_matching_etag_is_skipped_as_echo(db):
    existing_id = db.create_contact(CanonicalContact(display_name="Jane", emails=[Email(value="jane@e.com")]))
    db.link_provider(existing_id, "google", "g-1")
    db.set_link_etag("google", "g-1", "E1")

    # The pull returns our own contact back with the SAME etag we recorded.
    incoming = ChangedContact(
        provider_id="g-1",
        contact=CanonicalContact(
            display_name="DIFFERENT NAME SHOULD NOT BE MERGED",
            emails=[Email(value="should-not-merge@e.com")],
        ),
        updated_at="2026-01-02T00:00:00Z",
        etag="E1",
    )
    google = FakeAdapter("google", changes=[incoming])
    engine = SyncEngine(db, {"google": google})

    result = engine.run()

    # Not merged: the stored contact is untouched.
    stored = db.get_contact(existing_id)
    assert stored.display_name == "Jane"
    assert {e.value for e in stored.emails} == {"jane@e.com"}
    # Not counted as updated, not re-pushed.
    assert result.updated == 0
    assert google.updated == []


def test_pulled_change_with_different_etag_is_processed(db):
    existing_id = db.create_contact(CanonicalContact(display_name="Jane", emails=[Email(value="jane@e.com")]))
    db.link_provider(existing_id, "google", "g-1")
    db.set_link_etag("google", "g-1", "E1")

    incoming = ChangedContact(
        provider_id="g-1",
        contact=CanonicalContact(
            display_name="Jane",
            emails=[Email(value="jane@e.com"), Email(value="jane.new@e.com")],
        ),
        updated_at="2026-01-02T00:00:00Z",
        etag="E2",
    )
    google = FakeAdapter("google", changes=[incoming], update_etag="E2-pushed")
    engine = SyncEngine(db, {"google": google})

    result = engine.run()

    stored = db.get_contact(existing_id)
    assert {e.value for e in stored.emails} == {"jane@e.com", "jane.new@e.com"}
    assert result.updated == 1
    # The merge dirtied the contact, so it was pushed back to google and the
    # link etag now reflects that write - not the stale "E1" it started with.
    assert db.get_link_etag("google", "g-1") == "E2-pushed"
    assert [pid for pid, _ in google.updated] == ["g-1"]


def test_push_update_records_returned_etag(db):
    existing_id = db.create_contact(CanonicalContact(display_name="Jane", emails=[Email(value="jane@e.com")]))
    db.link_provider(existing_id, "google", "g-1")
    db.link_provider(existing_id, "microsoft", "ms-1")

    # A change on google dirties the contact; the microsoft push then updates.
    incoming = ChangedContact(
        provider_id="g-1",
        contact=CanonicalContact(
            display_name="Jane",
            emails=[Email(value="jane@e.com"), Email(value="jane.new@e.com")],
        ),
        updated_at="2026-01-02T00:00:00Z",
        etag="E2",
    )
    google = FakeAdapter("google", changes=[incoming])
    microsoft = FakeAdapter("microsoft", update_etag="E-new")
    engine = SyncEngine(db, {"google": google, "microsoft": microsoft})

    engine.run()

    assert db.get_link_etag("microsoft", "ms-1") == "E-new"


def test_push_create_records_returned_etag(db):
    existing_id = db.create_contact(CanonicalContact(display_name="Jane", emails=[Email(value="jane@e.com")]))
    db.link_provider(existing_id, "google", "g-1")

    google = FakeAdapter("google")
    microsoft = FakeAdapter("microsoft", create_etag="E-created")
    engine = SyncEngine(db, {"google": google, "microsoft": microsoft})

    engine.run()

    # A new link to microsoft was created for the contact.
    links = db.get_links_for_contact(existing_id)
    assert "microsoft" in links
    assert db.get_link_etag("microsoft", links["microsoft"]) == "E-created"


def test_echo_after_own_write_is_suppressed_end_to_end(db):
    # Run 1: a new google contact is created and pushed to microsoft.
    incoming = ChangedContact(
        provider_id="g-1",
        contact=CanonicalContact(display_name="Jane", emails=[Email(value="jane@e.com")]),
        updated_at="2026-01-01T00:00:00Z",
        etag="G1",
    )
    google = FakeAdapter("google", changes=[incoming])
    microsoft = FakeAdapter("microsoft", create_etag="M1")
    engine = SyncEngine(db, {"google": google, "microsoft": microsoft})

    result1 = engine.run()
    assert result1.created == 1
    assert len(microsoft.created) == 1

    contact_id = db.get_link("google", "g-1")
    ms_links = db.get_links_for_contact(contact_id)
    ms_provider_id = ms_links["microsoft"]
    # The etag microsoft's create returned was recorded so its echo is suppressed.
    assert db.get_link_etag("microsoft", ms_provider_id) == "M1"

    # Run 2: microsoft's next pull echoes back the contact we just wrote there,
    # carrying the same etag "M1" it assigned. It must be suppressed.
    echo = ChangedContact(
        provider_id=ms_provider_id,
        contact=CanonicalContact(display_name="Jane", emails=[Email(value="jane@e.com")]),
        updated_at="2026-01-02T00:00:00Z",
        etag="M1",
    )
    google2 = FakeAdapter("google")
    microsoft2 = FakeAdapter("microsoft", changes=[echo])
    engine2 = SyncEngine(db, {"google": google2, "microsoft": microsoft2})

    result2 = engine2.run()

    assert result2.updated == 0
    assert microsoft2.updated == []
    assert google2.updated == []


def test_provider_error_is_isolated_and_reported(db):
    class ExplodingAdapter(FakeAdapter):
        def list_changes(self, since_token):
            raise RuntimeError("boom")

    google = ExplodingAdapter("google")
    microsoft = FakeAdapter("microsoft")
    engine = SyncEngine(db, {"google": google, "microsoft": microsoft})

    result = engine.run()

    assert "google" in result.provider_errors
    assert "boom" in result.provider_errors["google"]
