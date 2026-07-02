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
    def __init__(self, name, changes=None, next_token="tok-1", raise_expired_once=False):
        self.name = name
        self._changes = changes or []
        self._next_token = next_token
        self._raise_expired_once = raise_expired_once
        self.created = []
        self.updated = []

    def list_changes(self, since_token):
        if self._raise_expired_once and since_token is not None:
            self._raise_expired_once = False
            raise SyncTokenExpiredError("expired")
        return ChangeSet(changes=self._changes, next_sync_token=self._next_token)

    def create(self, contact):
        provider_id = f"{self.name}-new-{len(self.created)}"
        self.created.append(contact)
        return provider_id

    def update(self, provider_id, contact):
        self.updated.append((provider_id, contact))

    def delete(self, provider_id):
        pass


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


def test_deleted_contact_is_removed_locally(db):
    existing_id = db.create_contact(CanonicalContact(display_name="Jane"))
    db.link_provider(existing_id, "google", "g-1")
    deletion = ChangedContact(provider_id="g-1", contact=None, updated_at="", deleted=True)
    google = FakeAdapter("google", changes=[deletion])
    engine = SyncEngine(db, {"google": google})

    result = engine.run()

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
