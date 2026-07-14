import pytest
from contacts_sync.db import Database
from contacts_sync.models import CanonicalContact, Email, Phone


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "contacts.db"))
    database.migrate()
    return database


def test_create_and_get_contact(db):
    contact = CanonicalContact(
        display_name="Jane Doe",
        emails=[Email(value="jane@example.com", primary=True)],
        phones=[Phone(value="+15551234567")],
    )
    contact_id = db.create_contact(contact)
    fetched = db.get_contact(contact_id)
    assert fetched.display_name == "Jane Doe"
    assert fetched.emails[0].value == "jane@example.com"
    assert fetched.phones[0].value == "+15551234567"


def test_update_contact(db):
    contact_id = db.create_contact(CanonicalContact(display_name="Jane Doe"))
    contact = db.get_contact(contact_id)
    contact.display_name = "Jane Smith"
    contact.emails = [Email(value="jane.smith@example.com")]
    db.update_contact(contact)
    fetched = db.get_contact(contact_id)
    assert fetched.display_name == "Jane Smith"
    assert fetched.emails[0].value == "jane.smith@example.com"


def test_delete_contact(db):
    contact_id = db.create_contact(CanonicalContact(display_name="Jane Doe"))
    db.delete_contact(contact_id)
    assert db.get_contact(contact_id) is None


def test_list_contacts(db):
    db.create_contact(CanonicalContact(display_name="A"))
    db.create_contact(CanonicalContact(display_name="B"))
    assert len(db.list_contacts()) == 2


def test_provider_link_roundtrip(db):
    contact_id = db.create_contact(CanonicalContact(display_name="Jane Doe"))
    db.link_provider(contact_id, "google", "people/123")
    assert db.get_link("google", "people/123") == contact_id
    assert db.get_links_for_contact(contact_id) == {"google": "people/123"}


def test_unlink_provider_removes_only_that_link(db):
    contact_id = db.create_contact(CanonicalContact(display_name="Jane Doe"))
    db.link_provider(contact_id, "google", "people/123")
    db.link_provider(contact_id, "microsoft", "ms-1")

    db.unlink_provider("microsoft", "ms-1")

    assert db.get_link("microsoft", "ms-1") is None
    assert db.get_link("google", "people/123") == contact_id
    assert db.get_contact(contact_id) is not None


def test_link_etag_roundtrip(db):
    contact_id = db.create_contact(CanonicalContact(display_name="Jane Doe"))
    db.link_provider(contact_id, "google", "people/123")

    # A freshly-linked provider has no etag until one is set.
    assert db.get_link_etag("google", "people/123") is None

    db.set_link_etag("google", "people/123", "etag-1")
    assert db.get_link_etag("google", "people/123") == "etag-1"

    db.set_link_etag("google", "people/123", "etag-2")
    assert db.get_link_etag("google", "people/123") == "etag-2"


def test_get_link_etag_returns_none_for_unknown_link(db):
    assert db.get_link_etag("google", "people/does-not-exist") is None


def test_migrate_is_idempotent_and_etag_column_exists(tmp_path):
    database = Database(str(tmp_path / "idempotent.db"))
    database.migrate()
    # Calling migrate() again must not error even though the etag column and
    # all tables already exist.
    database.migrate()

    contact_id = database.create_contact(CanonicalContact(display_name="Jane"))
    database.link_provider(contact_id, "google", "people/1")
    database.set_link_etag("google", "people/1", "etag-x")
    assert database.get_link_etag("google", "people/1") == "etag-x"


def test_create_and_get_contact_with_photo(db):
    contact = CanonicalContact(
        display_name="Jane Doe", photo_data=b"fake-jpeg-bytes", photo_content_type="image/jpeg"
    )
    contact_id = db.create_contact(contact)
    fetched = db.get_contact(contact_id)
    assert fetched.photo_data == b"fake-jpeg-bytes"
    assert fetched.photo_content_type == "image/jpeg"


def test_update_contact_photo(db):
    contact_id = db.create_contact(CanonicalContact(display_name="Jane Doe"))
    contact = db.get_contact(contact_id)
    contact.photo_data = b"updated-bytes"
    contact.photo_content_type = "image/png"
    db.update_contact(contact)
    fetched = db.get_contact(contact_id)
    assert fetched.photo_data == b"updated-bytes"
    assert fetched.photo_content_type == "image/png"


def test_migrate_is_idempotent_with_photo_columns(tmp_path):
    database = Database(str(tmp_path / "photo-idempotent.db"))
    database.migrate()
    database.migrate()  # must not error even though photo columns already exist

    contact_id = database.create_contact(CanonicalContact(display_name="Jane", photo_data=b"x"))
    assert database.get_contact(contact_id).photo_data == b"x"


def test_sync_token_roundtrip(db):
    assert db.get_sync_token("google") is None
    db.set_sync_token("google", "token-abc")
    assert db.get_sync_token("google") == "token-abc"


def test_pending_match_roundtrip(db):
    db.save_pending_match("google", "people/999", candidate_contact_ids=[1, 2], contact_data_json="{}")
    pending = db.list_pending_matches()
    assert len(pending) == 1
    assert pending[0]["provider"] == "google"
    db.delete_pending_match(pending[0]["id"])
    assert db.list_pending_matches() == []


def test_reset_sync_state_clears_tokens_and_link_etags(tmp_path):
    from contacts_sync.models import CanonicalContact

    database = Database(str(tmp_path / "contacts.db"))
    database.migrate()
    contact_id = database.create_contact(CanonicalContact(display_name="Jane"))
    database.link_provider(contact_id, "google", "g-1")
    database.set_link_etag("google", "g-1", "etag-1")
    database.set_sync_token("google", "token-1")

    database.reset_sync_state()

    assert database.get_sync_token("google") is None
    assert database.get_link_etag("google", "g-1") is None
    # Links themselves survive - only the sync bookkeeping is cleared.
    assert database.get_link("google", "g-1") == contact_id
