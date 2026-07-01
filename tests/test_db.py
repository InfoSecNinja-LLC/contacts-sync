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
