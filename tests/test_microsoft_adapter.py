import pytest
from contacts_sync.adapters.microsoft import MicrosoftAdapter, _to_graph
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
    requests_mock.get(f"{BASE}/me/contacts/AAMk123/photo/$value", status_code=404)
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

def test_create_posts_contact_and_returns_id_and_etag(requests_mock):
    requests_mock.post(f"{BASE}/me/contacts", json={"id": "AAMk-new", "@odata.etag": 'W/"etag-created"'})
    adapter = MicrosoftAdapter(token_provider=lambda: "fake-token")

    result = adapter.create(CanonicalContact(display_name="New", emails=[Email(value="n@e.com")]))

    assert result == ("AAMk-new", 'W/"etag-created"')


def test_list_changes_populates_changed_contact_etag(requests_mock):
    requests_mock.get(
        f"{BASE}/me/contactFolders/contacts/contacts/delta",
        json={
            "value": [
                {
                    "id": "AAMk123",
                    "@odata.etag": 'W/"etag-abc"',
                    "displayName": "Jane Doe",
                    "emailAddresses": [{"address": "jane@example.com"}],
                }
            ],
            "@odata.deltaLink": f"{BASE}/x?$deltatoken=y",
        },
    )
    requests_mock.get(f"{BASE}/me/contacts/AAMk123/photo/$value", status_code=404)
    adapter = MicrosoftAdapter(token_provider=lambda: "fake-token")

    change_set = adapter.list_changes(None)

    assert change_set.changes[0].etag == 'W/"etag-abc"'


def test_update_requests_representation_and_returns_etag(requests_mock):
    requests_mock.patch(
        f"{BASE}/me/contacts/AAMk1", json={"id": "AAMk1", "@odata.etag": 'W/"etag-updated"'}
    )
    adapter = MicrosoftAdapter(token_provider=lambda: "fake-token")

    result = adapter.update("AAMk1", CanonicalContact(display_name="Jane", emails=[Email(value="j@e.com")]))

    assert result == 'W/"etag-updated"'
    assert requests_mock.last_request.headers["Prefer"] == "return=representation"


def test_update_returns_none_on_204_no_content(requests_mock):
    requests_mock.patch(f"{BASE}/me/contacts/AAMk1", status_code=204)
    adapter = MicrosoftAdapter(token_provider=lambda: "fake-token")

    result = adapter.update("AAMk1", CanonicalContact(display_name="Jane", emails=[Email(value="j@e.com")]))

    assert result is None

def test_delete_treats_404_as_success(requests_mock):
    requests_mock.delete(f"{BASE}/me/contacts/AAMk1", status_code=404)
    adapter = MicrosoftAdapter(token_provider=lambda: "fake-token")
    adapter.delete("AAMk1")  # should not raise


def test_to_graph_truncates_business_phones_to_two_entries():
    """Microsoft Graph enforces a hard maximum of 2 entries for businessPhones,
    confirmed live: "The multi value property businessPhones has 4 entries,
    that exceeds the max allowed value of 2." A contact with more non-mobile
    phones than that must be truncated, not sent as-is.
    """
    contact = CanonicalContact(
        display_name="Many Phones",
        phones=[
            Phone(value="1111111111"),
            Phone(value="2222222222"),
            Phone(value="3333333333"),
            Phone(value="4444444444"),
        ],
    )

    body = _to_graph(contact)

    assert len(body["businessPhones"]) == 2
    assert body["businessPhones"] == ["1111111111", "2222222222"]


def test_to_graph_truncates_email_addresses_to_three_entries():
    """Graph/Outlook contacts support at most 3 email addresses; a contact with
    more must be truncated, not sent as-is (same failure class as businessPhones).
    """
    contact = CanonicalContact(
        display_name="Many Emails",
        emails=[Email(value=f"e{i}@example.com") for i in range(6)],
    )

    body = _to_graph(contact)

    assert len(body["emailAddresses"]) == 3
    assert [e["address"] for e in body["emailAddresses"]] == [
        "e0@example.com",
        "e1@example.com",
        "e2@example.com",
    ]


def test_create_sends_truncated_business_phones(requests_mock):
    requests_mock.post(f"{BASE}/me/contacts", json={"id": "AAMk-new"})
    adapter = MicrosoftAdapter(token_provider=lambda: "fake-token")

    contact = CanonicalContact(
        display_name="Many Phones",
        phones=[Phone(value=str(n) * 10) for n in range(1, 5)],
    )
    adapter.create(contact)

    sent_body = requests_mock.last_request.json()
    assert len(sent_body["businessPhones"]) == 2


def test_update_raises_resource_gone_on_404(requests_mock):
    from contacts_sync.adapters.base import ProviderResourceGoneError
    requests_mock.patch(f"{BASE}/me/contacts/AAMkGONE", status_code=404)
    adapter = MicrosoftAdapter(token_provider=lambda: "fake-token")
    with pytest.raises(ProviderResourceGoneError):
        adapter.update("AAMkGONE", CanonicalContact(display_name="Jane", emails=[Email(value="j@e.com")]))


def test_list_changes_fetches_photo_for_changed_contact(requests_mock):
    requests_mock.get(
        f"{BASE}/me/contactFolders/contacts/contacts/delta",
        json={
            "value": [{"id": "AAMk123", "displayName": "Jane Doe", "emailAddresses": [{"address": "jane@example.com"}]}],
            "@odata.deltaLink": f"{BASE}/x?$deltatoken=y",
        },
    )
    requests_mock.get(
        f"{BASE}/me/contacts/AAMk123/photo/$value",
        content=b"fake-photo-bytes",
        headers={"Content-Type": "image/jpeg"},
    )
    adapter = MicrosoftAdapter(token_provider=lambda: "fake-token")

    change_set = adapter.list_changes(None)

    assert change_set.changes[0].contact.photo_data == b"fake-photo-bytes"
    assert change_set.changes[0].contact.photo_content_type == "image/jpeg"


def test_list_changes_strips_parameters_from_content_type_header(requests_mock):
    requests_mock.get(
        f"{BASE}/me/contactFolders/contacts/contacts/delta",
        json={
            "value": [{"id": "AAMk123", "displayName": "Jane Doe"}],
            "@odata.deltaLink": f"{BASE}/x?$deltatoken=y",
        },
    )
    requests_mock.get(
        f"{BASE}/me/contacts/AAMk123/photo/$value",
        content=b"fake-photo-bytes",
        headers={"Content-Type": "image/jpeg; charset=binary"},
    )
    adapter = MicrosoftAdapter(token_provider=lambda: "fake-token")

    change_set = adapter.list_changes(None)

    assert change_set.changes[0].contact.photo_content_type == "image/jpeg"


def test_list_changes_treats_404_photo_as_no_photo(requests_mock):
    requests_mock.get(
        f"{BASE}/me/contactFolders/contacts/contacts/delta",
        json={
            "value": [{"id": "AAMk123", "displayName": "Jane Doe"}],
            "@odata.deltaLink": f"{BASE}/x?$deltatoken=y",
        },
    )
    requests_mock.get(f"{BASE}/me/contacts/AAMk123/photo/$value", status_code=404)
    adapter = MicrosoftAdapter(token_provider=lambda: "fake-token")

    change_set = adapter.list_changes(None)

    assert change_set.changes[0].contact.photo_data is None


def test_create_pushes_photo_bytes(requests_mock):
    requests_mock.post(f"{BASE}/me/contacts", json={"id": "AAMk-new"})
    photo_put = requests_mock.put(f"{BASE}/me/contacts/AAMk-new/photo/$value", status_code=204)
    adapter = MicrosoftAdapter(token_provider=lambda: "fake-token")

    contact = CanonicalContact(
        display_name="New", emails=[Email(value="n@e.com")],
        photo_data=b"fake-photo-bytes", photo_content_type="image/jpeg",
    )
    adapter.create(contact)

    assert photo_put.call_count == 1
    assert requests_mock.last_request.body == b"fake-photo-bytes"
    assert requests_mock.last_request.headers["Content-Type"] == "image/jpeg"


def test_update_does_not_push_photo_when_absent(requests_mock):
    requests_mock.patch(f"{BASE}/me/contacts/AAMk1", json={"id": "AAMk1"})
    photo_put = requests_mock.put(f"{BASE}/me/contacts/AAMk1/photo/$value", status_code=204)
    adapter = MicrosoftAdapter(token_provider=lambda: "fake-token")

    adapter.update("AAMk1", CanonicalContact(display_name="Jane", emails=[Email(value="j@e.com")]))

    assert photo_put.call_count == 0


def test_list_changes_skips_photo_on_fetch_failure(requests_mock):
    import requests
    requests_mock.get(
        f"{BASE}/me/contactFolders/contacts/contacts/delta",
        json={
            "value": [{"id": "AAMk123", "displayName": "Jane Doe"}],
            "@odata.deltaLink": f"{BASE}/x?$deltatoken=y",
        },
    )
    requests_mock.get(
        f"{BASE}/me/contacts/AAMk123/photo/$value",
        exc=requests.exceptions.ConnectionError("network blip"),
    )
    adapter = MicrosoftAdapter(token_provider=lambda: "fake-token")

    change_set = adapter.list_changes(None)

    # The photo fetch failed, but the contact and the rest of list_changes must
    # still succeed - a bad photo fetch must not abort the whole provider sync.
    assert change_set.changes[0].contact.photo_data is None
    assert change_set.next_sync_token.endswith("$deltatoken=y")
