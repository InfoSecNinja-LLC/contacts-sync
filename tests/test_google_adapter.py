import pytest
from googleapiclient.errors import HttpError

from contacts_sync.adapters.google import PERSON_FIELDS, GoogleAdapter
from contacts_sync.adapters.base import SyncTokenExpiredError

FAKE_PERSON = {
    "resourceName": "people/123",
    "etag": "%EgUBAj0CBy4=",
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


def test_list_changes_captures_etag_into_extra(mocker):
    service = _fake_service(
        mocker,
        {"connections": [FAKE_PERSON], "nextSyncToken": "sync-2"},
    )
    mocker.patch("contacts_sync.adapters.google.build", return_value=service)
    adapter = GoogleAdapter(credentials=mocker.Mock())

    change_set = adapter.list_changes(None)

    assert change_set.changes[0].contact.extra["google_etag"] == "%EgUBAj0CBy4="


def test_list_changes_flags_deleted_contacts(mocker):
    deleted_person = {"resourceName": "people/456", "metadata": {"deleted": True}}
    service = _fake_service(mocker, {"connections": [deleted_person], "nextSyncToken": "sync-3"})
    mocker.patch("contacts_sync.adapters.google.build", return_value=service)
    adapter = GoogleAdapter(credentials=mocker.Mock())

    change_set = adapter.list_changes("sync-2")

    assert change_set.changes[0].deleted is True
    assert change_set.changes[0].provider_id == "people/456"


def _fake_http_error(status, message=b"EXPIRED_SYNC_TOKEN"):
    resp = type("Resp", (), {"status": status, "reason": "Gone"})()
    content = message if isinstance(message, bytes) else message.encode()
    return HttpError(resp, content, uri="https://people.googleapis.com/v1/people/me/connections")


def test_list_changes_raises_on_expired_sync_token(mocker):
    service = _fake_service(mocker, None, side_effect=_fake_http_error(410))
    mocker.patch("contacts_sync.adapters.google.build", return_value=service)
    adapter = GoogleAdapter(credentials=mocker.Mock())

    with pytest.raises(SyncTokenExpiredError):
        adapter.list_changes("stale-token")


def test_list_changes_does_not_misclassify_other_http_errors(mocker):
    service = _fake_service(mocker, None, side_effect=_fake_http_error(500, message=b"Internal error"))
    mocker.patch("contacts_sync.adapters.google.build", return_value=service)
    adapter = GoogleAdapter(credentials=mocker.Mock())

    with pytest.raises(HttpError):
        adapter.list_changes("stale-token")


def test_list_changes_paginates_across_multiple_pages(mocker):
    person_page_1 = {"resourceName": "people/111", **{k: v for k, v in FAKE_PERSON.items() if k != "resourceName"}}
    person_page_2 = {"resourceName": "people/222", **{k: v for k, v in FAKE_PERSON.items() if k != "resourceName"}}

    connections = mocker.Mock()
    connections.list.return_value.execute.side_effect = [
        {"connections": [person_page_1], "nextPageToken": "page-2"},
        {"connections": [person_page_2], "nextSyncToken": "sync-final"},
    ]
    people = mocker.Mock()
    people.connections.return_value = connections
    service = mocker.Mock()
    service.people.return_value = people
    mocker.patch("contacts_sync.adapters.google.build", return_value=service)
    adapter = GoogleAdapter(credentials=mocker.Mock())

    change_set = adapter.list_changes(None)

    provider_ids = {change.provider_id for change in change_set.changes}
    assert provider_ids == {"people/111", "people/222"}
    assert change_set.next_sync_token == "sync-final"
    assert connections.list.return_value.execute.call_count == 2


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


def test_update_does_not_clobber_addresses_or_organizations(mocker):
    from contacts_sync.models import CanonicalContact, Email

    people = mocker.Mock()
    people.updateContact.return_value.execute.return_value = {}
    service = mocker.Mock()
    service.people.return_value = people
    mocker.patch("contacts_sync.adapters.google.build", return_value=service)
    adapter = GoogleAdapter(credentials=mocker.Mock())

    adapter.update("people/123", CanonicalContact(display_name="Jane Doe", emails=[Email(value="jane@example.com")]))

    people.updateContact.assert_called_once()
    _, kwargs = people.updateContact.call_args
    assert kwargs["resourceName"] == "people/123"
    assert kwargs["updatePersonFields"] == PERSON_FIELDS
    assert "addresses" not in PERSON_FIELDS
    assert "organizations" not in PERSON_FIELDS
    body = kwargs["body"]
    assert "addresses" not in body
    assert "organizations" not in body


def test_update_includes_etag_in_body_when_contact_has_one(mocker):
    from contacts_sync.models import CanonicalContact, Email

    people = mocker.Mock()
    people.updateContact.return_value.execute.return_value = {}
    service = mocker.Mock()
    service.people.return_value = people
    mocker.patch("contacts_sync.adapters.google.build", return_value=service)
    adapter = GoogleAdapter(credentials=mocker.Mock())

    contact = CanonicalContact(
        display_name="Jane Doe",
        emails=[Email(value="jane@example.com")],
        extra={"google_etag": "%EgUBAj0CBy4="},
    )
    adapter.update("people/123", contact)

    people.updateContact.assert_called_once()
    _, kwargs = people.updateContact.call_args
    body = kwargs["body"]
    assert body["etag"] == "%EgUBAj0CBy4="


def test_update_omits_etag_when_contact_has_none(mocker):
    from contacts_sync.models import CanonicalContact, Email

    people = mocker.Mock()
    people.updateContact.return_value.execute.return_value = {}
    service = mocker.Mock()
    service.people.return_value = people
    mocker.patch("contacts_sync.adapters.google.build", return_value=service)
    adapter = GoogleAdapter(credentials=mocker.Mock())

    contact = CanonicalContact(display_name="Jane Doe", emails=[Email(value="jane@example.com")])
    adapter.update("people/123", contact)

    _, kwargs = people.updateContact.call_args
    body = kwargs["body"]
    assert "etag" not in body


def test_create_does_not_include_etag_in_body(mocker):
    from contacts_sync.models import CanonicalContact, Email

    people = mocker.Mock()
    people.createContact.return_value.execute.return_value = {"resourceName": "people/789"}
    service = mocker.Mock()
    service.people.return_value = people
    mocker.patch("contacts_sync.adapters.google.build", return_value=service)
    adapter = GoogleAdapter(credentials=mocker.Mock())

    # Even if a stray etag were present in extra on a "new" contact object,
    # create() must never send it - Google's createContact doesn't want one.
    contact = CanonicalContact(
        display_name="New Person", emails=[Email(value="n@e.com")], extra={"google_etag": "stale-etag"}
    )
    adapter.create(contact)

    _, kwargs = people.createContact.call_args
    assert "etag" not in kwargs["body"]


def test_update_refetches_etag_and_retries_on_etag_conflict(mocker):
    from contacts_sync.models import CanonicalContact, Email

    people = mocker.Mock()
    people.updateContact.return_value.execute.side_effect = [
        _fake_http_error(
            400, message=b"Request person.etag is different than the current person.etag."
        ),
        {},
    ]
    people.get.return_value.execute.return_value = {
        "resourceName": "people/123",
        "etag": "fresh-etag-xyz",
        "names": [{"displayName": "Jane Doe", "givenName": "Jane", "familyName": "Doe"}],
    }
    service = mocker.Mock()
    service.people.return_value = people
    mocker.patch("contacts_sync.adapters.google.build", return_value=service)
    adapter = GoogleAdapter(credentials=mocker.Mock())

    contact = CanonicalContact(
        display_name="Jane Doe",
        emails=[Email(value="jane@example.com")],
        extra={"google_etag": "stale-etag"},
    )
    adapter.update("people/123", contact)

    people.get.assert_called_once()
    _, get_kwargs = people.get.call_args
    assert get_kwargs["resourceName"] == "people/123"
    assert get_kwargs["personFields"] == PERSON_FIELDS

    assert people.updateContact.call_count == 2
    second_call_kwargs = people.updateContact.call_args_list[1].kwargs
    assert second_call_kwargs["body"]["etag"] == "fresh-etag-xyz"


def test_update_does_not_refetch_on_non_etag_400(mocker):
    from contacts_sync.models import CanonicalContact, Email

    people = mocker.Mock()
    people.updateContact.return_value.execute.side_effect = _fake_http_error(
        400, message=b"Some other bad request error"
    )
    service = mocker.Mock()
    service.people.return_value = people
    mocker.patch("contacts_sync.adapters.google.build", return_value=service)
    adapter = GoogleAdapter(credentials=mocker.Mock())

    contact = CanonicalContact(
        display_name="Jane Doe",
        emails=[Email(value="jane@example.com")],
        extra={"google_etag": "stale-etag"},
    )
    with pytest.raises(HttpError):
        adapter.update("people/123", contact)

    people.get.assert_not_called()
    assert people.updateContact.call_count == 1


def test_delete_calls_delete_contact_with_provider_id(mocker):
    people = mocker.Mock()
    people.deleteContact.return_value.execute.return_value = {}
    service = mocker.Mock()
    service.people.return_value = people
    mocker.patch("contacts_sync.adapters.google.build", return_value=service)
    adapter = GoogleAdapter(credentials=mocker.Mock())

    adapter.delete("people/123")

    people.deleteContact.assert_called_once_with(resourceName="people/123")
