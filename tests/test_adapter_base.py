from contacts_sync.adapters.base import ChangedContact, ChangeSet, SyncTokenExpiredError
from contacts_sync.models import CanonicalContact


def test_changed_contact_and_change_set_construction():
    contact = CanonicalContact(display_name="Jane")
    change = ChangedContact(provider_id="123", contact=contact, updated_at="2026-01-01T00:00:00Z")
    change_set = ChangeSet(changes=[change], next_sync_token="token-1")
    assert change_set.changes[0].contact.display_name == "Jane"
    assert change_set.next_sync_token == "token-1"


def test_sync_token_expired_is_an_exception():
    assert issubclass(SyncTokenExpiredError, Exception)
