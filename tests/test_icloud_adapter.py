import pytest
import vobject
from contacts_sync.adapters.icloud import (
    ICloudAdapter,
    _parse_multistatus,
    _to_canonical,
    _to_vcard,
    discover_addressbook_path,
)
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


def test_create_sends_if_none_match_header_to_prevent_overwrite(requests_mock):
    requests_mock.put(f"{BASE}{ADDRESSBOOK}1.vcf", status_code=201)
    adapter = ICloudAdapter("me@icloud.com", "app-pass", ADDRESSBOOK)

    adapter.create(CanonicalContact(id=1, display_name="New", emails=[Email(value="n@e.com")]))

    assert requests_mock.last_request.headers["If-None-Match"] == '"*"'


def test_create_propagates_412_when_resource_already_exists(requests_mock):
    requests_mock.put(f"{BASE}{ADDRESSBOOK}1.vcf", status_code=412)
    adapter = ICloudAdapter("me@icloud.com", "app-pass", ADDRESSBOOK)

    with pytest.raises(Exception):
        adapter.create(CanonicalContact(id=1, display_name="New", emails=[Email(value="n@e.com")]))


def test_to_canonical_reads_back_structured_name_round_trip():
    contact = CanonicalContact(given_name="John", family_name="Smith", display_name="John Smith")

    vcard = _to_vcard(contact)
    parsed = vobject.readOne(vcard.serialize())
    canonical = _to_canonical(parsed)

    assert canonical.given_name == "John"
    assert canonical.family_name == "Smith"


def test_to_canonical_maps_empty_name_components_to_none():
    vcard = vobject.readOne(
        "BEGIN:VCARD\nVERSION:3.0\nFN:No Name\nN:;;;;\nEND:VCARD\n"
    )

    canonical = _to_canonical(vcard)

    assert canonical.given_name is None
    assert canonical.family_name is None


def test_parse_multistatus_handles_double_space_status_line():
    xml_text = """<?xml version="1.0" encoding="utf-8" ?>
<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:carddav">
  <D:response>
    <D:href>/carddavhome/addressbooks/card/jane.vcf</D:href>
    <D:propstat>
      <D:prop>
        <D:getetag>"etag-1"</D:getetag>
        <C:address-data>BEGIN:VCARD
VERSION:3.0
FN:Jane Doe
END:VCARD
</C:address-data>
      </D:prop>
      <D:status>HTTP/1.1  200 OK</D:status>
    </D:propstat>
  </D:response>
</D:multistatus>"""

    results = _parse_multistatus(xml_text)

    assert len(results) == 1
    _href, status_code, _etag, _address_data = results[0]
    assert status_code == "200"


def test_update_puts_vcard_to_provider_id_with_contact_data(requests_mock):
    provider_id = f"{BASE}{ADDRESSBOOK}jane.vcf"
    requests_mock.put(provider_id, status_code=204)
    adapter = ICloudAdapter("me@icloud.com", "app-pass", ADDRESSBOOK)

    contact = CanonicalContact(
        display_name="Jane Updated",
        given_name="Jane",
        family_name="Updated",
        emails=[Email(value="jane.updated@example.com")],
    )
    adapter.update(provider_id, contact)

    assert requests_mock.last_request.url == provider_id
    assert requests_mock.last_request.method == "PUT"
    body = requests_mock.last_request.text
    assert "FN:Jane Updated" in body
    assert "jane.updated@example.com" in body


def test_list_changes_returns_all_contacts_from_multi_response_multistatus(requests_mock):
    multi_response = """<?xml version="1.0" encoding="utf-8" ?>
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
END:VCARD
</C:address-data>
      </D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
  <D:response>
    <D:href>/carddavhome/addressbooks/card/bob.vcf</D:href>
    <D:propstat>
      <D:prop>
        <D:getetag>"etag-2"</D:getetag>
        <C:address-data>BEGIN:VCARD
VERSION:3.0
FN:Bob Jones
EMAIL:bob@example.com
END:VCARD
</C:address-data>
      </D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
  <D:response>
    <D:href>/carddavhome/addressbooks/card/carol.vcf</D:href>
    <D:propstat>
      <D:prop>
        <D:getetag>"etag-3"</D:getetag>
        <C:address-data>BEGIN:VCARD
VERSION:3.0
FN:Carol White
EMAIL:carol@example.com
END:VCARD
</C:address-data>
      </D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
  <D:sync-token>https://contacts.icloud.com/sync/3</D:sync-token>
</D:multistatus>"""
    requests_mock.register_uri("REPORT", f"{BASE}{ADDRESSBOOK}", text=multi_response, status_code=207)
    adapter = ICloudAdapter("me@icloud.com", "app-pass", ADDRESSBOOK)

    change_set = adapter.list_changes(None)

    assert len(change_set.changes) == 3
    names = {c.contact.display_name for c in change_set.changes}
    assert names == {"Jane Doe", "Bob Jones", "Carol White"}
    assert change_set.next_sync_token == "https://contacts.icloud.com/sync/3"


def test_discover_addressbook_path_follows_principal_then_home_set(requests_mock):
    requests_mock.register_uri(
        "PROPFIND", f"{BASE}/",
        text="""<?xml version="1.0" encoding="utf-8" ?>
<D:multistatus xmlns:D="DAV:">
  <D:response>
    <D:href>/</D:href>
    <D:propstat>
      <D:prop>
        <D:current-user-principal>
          <D:href>/1234567890/principal/</D:href>
        </D:current-user-principal>
      </D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
</D:multistatus>""",
        status_code=207,
    )
    requests_mock.register_uri(
        "PROPFIND", f"{BASE}/1234567890/principal/",
        text="""<?xml version="1.0" encoding="utf-8" ?>
<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:carddav">
  <D:response>
    <D:href>/1234567890/principal/</D:href>
    <D:propstat>
      <D:prop>
        <C:addressbook-home-set>
          <D:href>/1234567890/carddavhome/</D:href>
        </C:addressbook-home-set>
      </D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
</D:multistatus>""",
        status_code=207,
    )

    path = discover_addressbook_path("me@icloud.com", "app-pass")

    assert path == "/1234567890/carddavhome/"


def test_discover_addressbook_path_raises_when_principal_missing(requests_mock):
    requests_mock.register_uri(
        "PROPFIND", f"{BASE}/",
        text="""<?xml version="1.0" encoding="utf-8" ?>
<D:multistatus xmlns:D="DAV:">
  <D:response>
    <D:href>/</D:href>
    <D:propstat>
      <D:prop/>
      <D:status>HTTP/1.1 404 Not Found</D:status>
    </D:propstat>
  </D:response>
</D:multistatus>""",
        status_code=207,
    )

    with pytest.raises(RuntimeError, match="principal"):
        discover_addressbook_path("me@icloud.com", "app-pass")


def test_discover_addressbook_path_raises_when_home_set_missing(requests_mock):
    requests_mock.register_uri(
        "PROPFIND", f"{BASE}/",
        text="""<?xml version="1.0" encoding="utf-8" ?>
<D:multistatus xmlns:D="DAV:">
  <D:response>
    <D:href>/</D:href>
    <D:propstat>
      <D:prop>
        <D:current-user-principal>
          <D:href>/1234567890/principal/</D:href>
        </D:current-user-principal>
      </D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
</D:multistatus>""",
        status_code=207,
    )
    requests_mock.register_uri(
        "PROPFIND", f"{BASE}/1234567890/principal/",
        text="""<?xml version="1.0" encoding="utf-8" ?>
<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:carddav">
  <D:response>
    <D:href>/1234567890/principal/</D:href>
    <D:propstat>
      <D:prop/>
      <D:status>HTTP/1.1 404 Not Found</D:status>
    </D:propstat>
  </D:response>
</D:multistatus>""",
        status_code=207,
    )

    with pytest.raises(RuntimeError, match="addressbook"):
        discover_addressbook_path("me@icloud.com", "app-pass")


def test_discover_addressbook_path_raises_on_auth_failure(requests_mock):
    requests_mock.register_uri(
        "PROPFIND", f"{BASE}/",
        status_code=401,
        text="Unauthorized",
    )

    with pytest.raises(Exception):
        discover_addressbook_path("me@icloud.com", "bad-pass")
