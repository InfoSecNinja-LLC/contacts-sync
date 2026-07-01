import pytest
from contacts_sync.adapters.icloud import ICloudAdapter
from contacts_sync.adapters.base import SyncTokenExpiredError
from contacts_sync.models import CanonicalContact, Email

ADDRESSBOOK = "/carddavhome/addressbooks/card/"
BASE = "https://contacts.icloud.com"

SYNC_RESPONSE = """<?xml version="1.0" encoding="utf-8" ?>
<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:carddav">
  <D:response>
    <D:href>/carddavhome/addressbooks/card/jane.vcf</D:href>
    <D:propstat>
      <D:prop>
        <D:getetag>"etag-1"</D:getetag>
        <C:address-data>BEGIN:VCARD
VERSION:3.0
FN:Jane Doe
EMAIL:jane@example.com
TEL:+15551234567
END:VCARD
</C:address-data>
      </D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
  <D:sync-token>https://contacts.icloud.com/sync/2</D:sync-token>
</D:multistatus>"""


def test_list_changes_parses_vcard_from_multistatus(requests_mock):
    requests_mock.register_uri("REPORT", f"{BASE}{ADDRESSBOOK}", text=SYNC_RESPONSE, status_code=207)
    adapter = ICloudAdapter("me@icloud.com", "app-pass", ADDRESSBOOK)

    change_set = adapter.list_changes(None)

    assert len(change_set.changes) == 1
    assert change_set.changes[0].contact.display_name == "Jane Doe"
    assert change_set.changes[0].contact.emails[0].value == "jane@example.com"
    assert change_set.next_sync_token == "https://contacts.icloud.com/sync/2"


def test_list_changes_raises_on_invalid_sync_token(requests_mock):
    requests_mock.register_uri(
        "REPORT", f"{BASE}{ADDRESSBOOK}", status_code=507, text="valid-sync-token invalid"
    )
    adapter = ICloudAdapter("me@icloud.com", "app-pass", ADDRESSBOOK)

    with pytest.raises(SyncTokenExpiredError):
        adapter.list_changes("stale-token")


def test_create_puts_vcard(requests_mock):
    requests_mock.put(f"{BASE}{ADDRESSBOOK}1.vcf", status_code=201)
    adapter = ICloudAdapter("me@icloud.com", "app-pass", ADDRESSBOOK)

    href = adapter.create(CanonicalContact(id=1, display_name="New", emails=[Email(value="n@e.com")]))

    assert href == f"{BASE}{ADDRESSBOOK}1.vcf"


def test_delete_treats_404_as_success(requests_mock):
    requests_mock.delete(f"{BASE}{ADDRESSBOOK}jane.vcf", status_code=404)
    adapter = ICloudAdapter("me@icloud.com", "app-pass", ADDRESSBOOK)
    adapter.delete(f"{BASE}{ADDRESSBOOK}jane.vcf")  # should not raise
