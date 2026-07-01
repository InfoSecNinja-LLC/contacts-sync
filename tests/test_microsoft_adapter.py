import pytest
from contacts_sync.adapters.microsoft import MicrosoftAdapter
from contacts_sync.adapters.base import SyncTokenExpiredError
from contacts_sync.models import CanonicalContact, Email, Phone

BASE = "https://graph.microsoft.com/v1.0"

def test_list_changes_maps_contact_and_captures_delta_link(requests_mock):
    requests_mock.get(
        f"{BASE}/me/contactFolders/contacts/contacts/delta",
        json={
            "value": [
                {
                    "id": "AAMk123",
                    "displayName": "Jane Doe",
                    "givenName": "Jane",
                    "surname": "Doe",
                    "emailAddresses": [{"address": "jane@example.com"}],
                    "businessPhones": ["5551234567"],
                    "lastModifiedDateTime": "2026-01-01T00:00:00Z",
                }
            ],
            "@odata.deltaLink": f"{BASE}/me/contactFolders/contacts/contacts/delta?$deltatoken=abc",
        },
    )
    adapter = MicrosoftAdapter(token_provider=lambda: "fake-token")

    change_set = adapter.list_changes(None)

    assert len(change_set.changes) == 1
    assert change_set.changes[0].contact.display_name == "Jane Doe"
    assert change_set.changes[0].contact.emails[0].value == "jane@example.com"
    assert change_set.next_sync_token.endswith("$deltatoken=abc")

def test_list_changes_flags_removed_contacts(requests_mock):
    requests_mock.get(
        f"{BASE}/me/contactFolders/contacts/contacts/delta",
        json={"value": [{"id": "AAMk999", "@removed": {"reason": "deleted"}}], "@odata.deltaLink": f"{BASE}/x?$deltatoken=y"},
    )
    adapter = MicrosoftAdapter(token_provider=lambda: "fake-token")

    change_set = adapter.list_changes(None)

    assert change_set.changes[0].deleted is True
    assert change_set.changes[0].provider_id == "AAMk999"

def test_list_changes_raises_on_410(requests_mock):
    requests_mock.get(
        f"{BASE}/me/contactFolders/contacts/contacts/delta",
        status_code=410,
        json={"error": {"code": "syncStateNotFound"}},
    )
    adapter = MicrosoftAdapter(token_provider=lambda: "fake-token")

    with pytest.raises(SyncTokenExpiredError):
        adapter.list_changes("stale-delta-link")

def test_create_posts_contact_and_returns_id(requests_mock):
    requests_mock.post(f"{BASE}/me/contacts", json={"id": "AAMk-new"})
    adapter = MicrosoftAdapter(token_provider=lambda: "fake-token")

    provider_id = adapter.create(CanonicalContact(display_name="New", emails=[Email(value="n@e.com")]))

    assert provider_id == "AAMk-new"

def test_delete_treats_404_as_success(requests_mock):
    requests_mock.delete(f"{BASE}/me/contacts/AAMk1", status_code=404)
    adapter = MicrosoftAdapter(token_provider=lambda: "fake-token")
    adapter.delete("AAMk1")  # should not raise
