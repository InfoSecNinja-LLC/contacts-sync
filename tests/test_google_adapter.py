import pytest
from contacts_sync.adapters.google import GoogleAdapter
from contacts_sync.adapters.base import SyncTokenExpiredError

FAKE_PERSON = {
    "resourceName": "people/123",
    "emailAddresses": [{"value": "jane@example.com"}],
    "phoneNumbers": [{"value": "+15551234567"}],
    "names": [{"displayName": "Jane Doe", "givenName": "Jane", "familyName": "Doe"}],
    "biographies": [{"value": "met at conf"}],
}


def _fake_service(mocker, response, side_effect=None):
    connections = mocker.Mock()
    if side_effect:
        connections.list.return_value.execute.side_effect = side_effect
    else:
        connections.list.return_value.execute.return_value = response
    people = mocker.Mock()
    people.connections.return_value = connections
    service = mocker.Mock()
    service.people.return_value = people
    return service


def test_list_changes_maps_person_to_canonical(mocker):
    service = _fake_service(
        mocker,
        {"connections": [FAKE_PERSON], "nextSyncToken": "sync-2"},
    )
    mocker.patch("contacts_sync.adapters.google.build", return_value=service)
    adapter = GoogleAdapter(credentials=mocker.Mock())

    change_set = adapter.list_changes(None)

    assert len(change_set.changes) == 1
    change = change_set.changes[0]
    assert change.provider_id == "people/123"
    assert change.contact.display_name == "Jane Doe"
    assert change.contact.emails[0].value == "jane@example.com"
    assert change_set.next_sync_token == "sync-2"


def test_list_changes_flags_deleted_contacts(mocker):
    deleted_person = {"resourceName": "people/456", "metadata": {"deleted": True}}
    service = _fake_service(mocker, {"connections": [deleted_person], "nextSyncToken": "sync-3"})
    mocker.patch("contacts_sync.adapters.google.build", return_value=service)
    adapter = GoogleAdapter(credentials=mocker.Mock())

    change_set = adapter.list_changes("sync-2")

    assert change_set.changes[0].deleted is True
    assert change_set.changes[0].provider_id == "people/456"


def test_list_changes_raises_on_expired_sync_token(mocker):
    class FakeHttpError(Exception):
        pass

    service = _fake_service(mocker, None, side_effect=FakeHttpError("EXPIRED_SYNC_TOKEN"))
    mocker.patch("contacts_sync.adapters.google.build", return_value=service)
    adapter = GoogleAdapter(credentials=mocker.Mock())

    with pytest.raises(SyncTokenExpiredError):
        adapter.list_changes("stale-token")


def test_create_sends_person_and_returns_resource_name(mocker):
    from contacts_sync.models import CanonicalContact, Email

    people = mocker.Mock()
    people.createContact.return_value.execute.return_value = {"resourceName": "people/789"}
    service = mocker.Mock()
    service.people.return_value = people
    mocker.patch("contacts_sync.adapters.google.build", return_value=service)
    adapter = GoogleAdapter(credentials=mocker.Mock())

    resource_name = adapter.create(CanonicalContact(display_name="New Person", emails=[Email(value="n@e.com")]))

    assert resource_name == "people/789"
    people.createContact.assert_called_once()
