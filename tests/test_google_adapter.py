import pytest
import requests
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


def test_list_changes_populates_changed_contact_etag(mocker):
    service = _fake_service(
        mocker,
        {"connections": [FAKE_PERSON], "nextSyncToken": "sync-2"},
    )
    mocker.patch("contacts_sync.adapters.google.build", return_value=service)
    adapter = GoogleAdapter(credentials=mocker.Mock())

    change_set = adapter.list_changes(None)

    assert change_set.changes[0].etag == "%EgUBAj0CBy4="


def test_update_returns_new_etag_from_response(mocker):
    from contacts_sync.models import CanonicalContact, Email

    people = mocker.Mock()
    people.updateContact.return_value.execute.return_value = {"etag": "etag-after-update"}
    service = mocker.Mock()
    service.people.return_value = people
    mocker.patch("contacts_sync.adapters.google.build", return_value=service)
    adapter = GoogleAdapter(credentials=mocker.Mock())

    result = adapter.update("people/123", CanonicalContact(display_name="Jane", emails=[Email(value="j@e.com")]))

    assert result == "etag-after-update"


def test_update_returns_fresh_etag_from_retry_response(mocker):
    from contacts_sync.models import CanonicalContact, Email

    people = mocker.Mock()
    people.updateContact.return_value.execute.side_effect = [
        _fake_http_error(400, message=b"Request person.etag is different than the current person.etag."),
        {"etag": "etag-from-retry"},
    ]
    people.get.return_value.execute.return_value = {
        "resourceName": "people/123",
        "etag": "fresh-etag-xyz",
        "names": [{"displayName": "Jane"}],
    }
    service = mocker.Mock()
    service.people.return_value = people
    mocker.patch("contacts_sync.adapters.google.build", return_value=service)
    adapter = GoogleAdapter(credentials=mocker.Mock())

    contact = CanonicalContact(
        display_name="Jane", emails=[Email(value="j@e.com")], extra={"google_etag": "stale-etag"}
    )
    result = adapter.update("people/123", contact)

    assert result == "etag-from-retry"


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
    people.createContact.return_value.execute.return_value = {
        "resourceName": "people/789",
        "etag": "etag-created",
    }
    service = mocker.Mock()
    service.people.return_value = people
    mocker.patch("contacts_sync.adapters.google.build", return_value=service)
    adapter = GoogleAdapter(credentials=mocker.Mock())

    result = adapter.create(CanonicalContact(display_name="New Person", emails=[Email(value="n@e.com")]))

    assert result == ("people/789", "etag-created")
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


def test_list_changes_fetches_photo_bytes_for_non_default_photo(mocker):
    person_with_photo = {
        **FAKE_PERSON,
        "photos": [{"url": "https://photo.example/jane.jpg", "default": False}],
    }
    service = _fake_service(mocker, {"connections": [person_with_photo], "nextSyncToken": "sync-2"})
    mocker.patch("contacts_sync.adapters.google.build", return_value=service)
    fake_response = mocker.Mock(content=b"fake-photo-bytes", headers={"Content-Type": "image/jpeg"})
    fake_response.raise_for_status = mocker.Mock()
    get_mock = mocker.patch("contacts_sync.adapters.google.requests.get", return_value=fake_response)
    adapter = GoogleAdapter(credentials=mocker.Mock(token="fake-token"))

    change_set = adapter.list_changes(None)

    assert change_set.changes[0].contact.photo_data == b"fake-photo-bytes"
    assert change_set.changes[0].contact.photo_content_type == "image/jpeg"
    get_mock.assert_called_once_with(
        "https://photo.example/jane.jpg", headers={"Authorization": "Bearer fake-token"}
    )


def test_list_changes_skips_photo_fetch_when_only_default_photo(mocker):
    person_with_default_photo = {
        **FAKE_PERSON,
        "photos": [{"url": "https://photo.example/default.jpg", "default": True}],
    }
    service = _fake_service(mocker, {"connections": [person_with_default_photo], "nextSyncToken": "sync-2"})
    mocker.patch("contacts_sync.adapters.google.build", return_value=service)
    get_mock = mocker.patch("contacts_sync.adapters.google.requests.get")
    adapter = GoogleAdapter(credentials=mocker.Mock(token="fake-token"))

    change_set = adapter.list_changes(None)

    assert change_set.changes[0].contact.photo_data is None
    get_mock.assert_not_called()


def test_create_pushes_photo_via_update_contact_photo(mocker):
    import base64
    from contacts_sync.models import CanonicalContact, Email

    people = mocker.Mock()
    people.createContact.return_value.execute.return_value = {"resourceName": "people/789", "etag": "e1"}
    service = mocker.Mock()
    service.people.return_value = people
    mocker.patch("contacts_sync.adapters.google.build", return_value=service)
    adapter = GoogleAdapter(credentials=mocker.Mock(token="fake-token"))

    contact = CanonicalContact(
        display_name="New", emails=[Email(value="n@e.com")],
        photo_data=b"fake-photo-bytes", photo_content_type="image/jpeg",
    )
    adapter.create(contact)

    people.updateContactPhoto.assert_called_once_with(
        resourceName="people/789",
        body={"photoBytes": base64.b64encode(b"fake-photo-bytes").decode()},
    )


def test_update_does_not_push_photo_when_absent(mocker):
    from contacts_sync.models import CanonicalContact, Email

    people = mocker.Mock()
    people.updateContact.return_value.execute.return_value = {}
    service = mocker.Mock()
    service.people.return_value = people
    mocker.patch("contacts_sync.adapters.google.build", return_value=service)
    adapter = GoogleAdapter(credentials=mocker.Mock(token="fake-token"))

    adapter.update("people/123", CanonicalContact(display_name="Jane", emails=[Email(value="j@e.com")]))

    people.updateContactPhoto.assert_not_called()


def test_list_changes_strips_parameters_from_content_type_header(mocker):
    person_with_photo = {
        **FAKE_PERSON,
        "photos": [{"url": "https://photo.example/jane.jpg", "default": False}],
    }
    service = _fake_service(mocker, {"connections": [person_with_photo], "nextSyncToken": "sync-2"})
    mocker.patch("contacts_sync.adapters.google.build", return_value=service)
    fake_response = mocker.Mock(content=b"fake-photo-bytes", headers={"Content-Type": "image/jpeg; charset=binary"})
    fake_response.raise_for_status = mocker.Mock()
    mocker.patch("contacts_sync.adapters.google.requests.get", return_value=fake_response)
    adapter = GoogleAdapter(credentials=mocker.Mock(token="fake-token"))

    change_set = adapter.list_changes(None)

    assert change_set.changes[0].contact.photo_content_type == "image/jpeg"


def test_list_changes_skips_photo_on_fetch_failure(mocker):
    person_with_photo = {
        **FAKE_PERSON,
        "photos": [{"url": "https://photo.example/jane.jpg", "default": False}],
    }
    service = _fake_service(mocker, {"connections": [person_with_photo], "nextSyncToken": "sync-2"})
    mocker.patch("contacts_sync.adapters.google.build", return_value=service)
    mocker.patch(
        "contacts_sync.adapters.google.requests.get",
        side_effect=requests.exceptions.ConnectionError("network blip"),
    )
    adapter = GoogleAdapter(credentials=mocker.Mock(token="fake-token"))

    change_set = adapter.list_changes(None)

    # The photo fetch failed, but the contact itself and the rest of list_changes
    # must still succeed - a bad photo must not abort the whole provider sync.
    assert change_set.changes[0].contact.photo_data is None
    assert change_set.next_sync_token == "sync-2"
