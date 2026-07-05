import defusedxml.ElementTree as ET
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


def test_list_changes_sends_sync_collection_in_dav_namespace(requests_mock):
    """Regression test: per RFC 6578, sync-collection is a WebDAV REPORT type
    defined in the DAV: namespace, not CardDAV. The root element of the
    REPORT request body must be <D:sync-collection>, not <C:sync-collection>.
    Sending <C:sync-collection> against a real iCloud account produces a 400
    Bad Request ("Didn't understand the report") because Apple's CardDAV
    server does not recognize sync-collection under the CardDAV namespace.
    """
    requests_mock.register_uri("REPORT", f"{BASE}{ADDRESSBOOK}", text=SYNC_RESPONSE, status_code=207)
    adapter = ICloudAdapter("me@icloud.com", "app-pass", ADDRESSBOOK)

    adapter.list_changes(None)

    sent_body = requests_mock.request_history[0].text
    root = ET.fromstring(sent_body)
    assert root.tag == "{DAV:}sync-collection", f"root element should be in DAV: namespace, got {root.tag}"


def test_list_changes_raises_on_invalid_sync_token(requests_mock):
    requests_mock.register_uri(
        "REPORT", f"{BASE}{ADDRESSBOOK}", status_code=507, text="valid-sync-token invalid"
    )
    adapter = ICloudAdapter("me@icloud.com", "app-pass", ADDRESSBOOK)

    with pytest.raises(SyncTokenExpiredError):
        adapter.list_changes("stale-token")


def test_create_puts_vcard_and_returns_href_and_etag(requests_mock):
    requests_mock.put(f"{BASE}{ADDRESSBOOK}1.vcf", status_code=201, headers={"ETag": '"etag-created"'})
    adapter = ICloudAdapter("me@icloud.com", "app-pass", ADDRESSBOOK)

    result = adapter.create(CanonicalContact(id=1, display_name="New", emails=[Email(value="n@e.com")]))

    assert result == (f"{BASE}{ADDRESSBOOK}1.vcf", '"etag-created"')


def test_create_returns_none_etag_when_header_absent(requests_mock):
    requests_mock.put(f"{BASE}{ADDRESSBOOK}1.vcf", status_code=201)
    adapter = ICloudAdapter("me@icloud.com", "app-pass", ADDRESSBOOK)

    result = adapter.create(CanonicalContact(id=1, display_name="New", emails=[Email(value="n@e.com")]))

    assert result == (f"{BASE}{ADDRESSBOOK}1.vcf", None)


def test_list_changes_populates_changed_contact_etag(requests_mock):
    requests_mock.register_uri("REPORT", f"{BASE}{ADDRESSBOOK}", text=SYNC_RESPONSE, status_code=207)
    adapter = ICloudAdapter("me@icloud.com", "app-pass", ADDRESSBOOK)

    change_set = adapter.list_changes(None)

    assert change_set.changes[0].etag == '"etag-1"'


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


def test_to_vcard_includes_uid_derived_from_contact_id():
    """Apple's CardDAV server rejects any vCard PUT without a UID property
    ("null vcard or UID missing from vcard"), confirmed live. The UID must be
    derived from the contact's local canonical id so it's stable across
    repeated pushes of the same contact.
    """
    contact = CanonicalContact(id=42, display_name="Jane Doe")

    vcard = _to_vcard(contact)

    assert hasattr(vcard, "uid")
    assert "42" in vcard.uid.value


def test_to_vcard_uid_is_stable_across_calls():
    contact = CanonicalContact(id=42, display_name="Jane Doe")

    vcard1 = _to_vcard(contact)
    vcard2 = _to_vcard(contact)

    assert vcard1.uid.value == vcard2.uid.value


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
    requests_mock.put(provider_id, status_code=204, headers={"ETag": '"etag-updated"'})
    adapter = ICloudAdapter("me@icloud.com", "app-pass", ADDRESSBOOK)

    contact = CanonicalContact(
        display_name="Jane Updated",
        given_name="Jane",
        family_name="Updated",
        emails=[Email(value="jane.updated@example.com")],
    )
    result = adapter.update(provider_id, contact)

    assert result == '"etag-updated"'
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


def test_list_changes_skips_collection_self_entry_with_no_address_data(requests_mock):
    """Real-world bug: a real iCloud sync-collection REPORT response includes
    an extra <D:response> entry representing the addressbook COLLECTION
    resource itself (per RFC 6578), alongside entries for each member vCard.
    This entry's propstat/prop only has <D:getetag>, no <C:address-data>, so
    _parse_multistatus's address_data comes back as "" for it. list_changes
    must skip this entry rather than calling vobject.readOne(""), which
    raises StopIteration (whose str() is confusingly empty, producing the
    "ERROR [icloud]: " symptom seen live with nothing after the colon).
    """
    response_with_collection_entry = """<?xml version="1.0" encoding="utf-8" ?>
<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:carddav">
  <D:response>
    <D:href>/carddavhome/addressbooks/card</D:href>
    <D:propstat>
      <D:prop>
        <D:getetag>"collection-etag"</D:getetag>
      </D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
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
  <D:sync-token>https://contacts.icloud.com/sync/2</D:sync-token>
</D:multistatus>"""
    requests_mock.register_uri("REPORT", f"{BASE}{ADDRESSBOOK}", text=response_with_collection_entry, status_code=207)
    adapter = ICloudAdapter("me@icloud.com", "app-pass", ADDRESSBOOK)

    change_set = adapter.list_changes(None)

    assert len(change_set.changes) == 1
    assert change_set.changes[0].contact.display_name == "Jane Doe"


def _is_depth(depth):
    def matcher(request):
        return request.headers.get("Depth") == depth

    return matcher


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
    requests_mock.register_uri(
        "PROPFIND", f"{BASE}/1234567890/carddavhome/",
        additional_matcher=_is_depth("1"),
        text="""<?xml version="1.0" encoding="utf-8" ?>
<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:carddav">
  <D:response>
    <D:href>/1234567890/carddavhome/</D:href>
    <D:propstat>
      <D:prop><D:resourcetype><D:collection/></D:resourcetype></D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
  <D:response>
    <D:href>/1234567890/carddavhome/card/</D:href>
    <D:propstat>
      <D:prop><D:resourcetype><D:collection/><C:addressbook/></D:resourcetype></D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
</D:multistatus>""",
        status_code=207,
    )

    path = discover_addressbook_path("me@icloud.com", "app-pass")

    # discover_addressbook_path always returns a fully-resolved absolute URL,
    # even when the underlying hrefs were relative paths, so ICloudAdapter
    # never has to guess which form it received. It also now performs a third
    # discovery step (Depth:1 enumeration of the home-set's children) since
    # the home-set itself is a container, not a queryable addressbook.
    assert path == f"{BASE}/1234567890/carddavhome/card/"


def _register_principal_and_home_set(requests_mock, home_set_href="/877060579/carddavhome/"):
    """Shared helper for tests focused on the third discovery step: registers
    the first two PROPFIND hops (principal, then home-set) with fixed,
    uninteresting responses so each test can focus on mocking the Depth:1
    home-set enumeration response.
    """
    requests_mock.register_uri(
        "PROPFIND", f"{BASE}/",
        text="""<?xml version="1.0" encoding="utf-8" ?>
<D:multistatus xmlns:D="DAV:">
  <D:response>
    <D:href>/</D:href>
    <D:propstat>
      <D:prop>
        <D:current-user-principal>
          <D:href>/877060579/principal/</D:href>
        </D:current-user-principal>
      </D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
</D:multistatus>""",
        status_code=207,
    )
    requests_mock.register_uri(
        "PROPFIND", f"{BASE}/877060579/principal/",
        text=f"""<?xml version="1.0" encoding="utf-8" ?>
<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:carddav">
  <D:response>
    <D:href>/877060579/principal/</D:href>
    <D:propstat>
      <D:prop>
        <C:addressbook-home-set>
          <D:href>{home_set_href}</D:href>
        </C:addressbook-home-set>
      </D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
</D:multistatus>""",
        status_code=207,
    )


def test_discover_addressbook_path_finds_addressbook_collection_within_home_set(requests_mock):
    """Real-world bug: the addressbook-home-set collection is a CONTAINER,
    not itself a queryable addressbook. Using it directly for sync-collection
    REPORT requests produces a 400 Bad Request from Apple's server. A third
    discovery step (Depth:1 PROPFIND on the home-set, enumerating children and
    checking each child's DAV:resourcetype for a carddav:addressbook marker)
    is required to find the actual addressbook collection URL.
    """
    _register_principal_and_home_set(requests_mock)
    requests_mock.register_uri(
        "PROPFIND", f"{BASE}/877060579/carddavhome/",
        additional_matcher=_is_depth("1"),
        text="""<?xml version="1.0" encoding="utf-8" ?>
<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:carddav">
  <D:response>
    <D:href>/877060579/carddavhome/</D:href>
    <D:propstat>
      <D:prop><D:resourcetype><D:collection/></D:resourcetype></D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
  <D:response>
    <D:href>/877060579/carddavhome/card/</D:href>
    <D:propstat>
      <D:prop><D:resourcetype><D:collection/><C:addressbook/></D:resourcetype></D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
</D:multistatus>""",
        status_code=207,
    )

    path = discover_addressbook_path("me@icloud.com", "app-pass")

    assert path == f"{BASE}/877060579/carddavhome/card/"


def test_discover_addressbook_path_sends_depth_1_for_home_set_enumeration(requests_mock):
    _register_principal_and_home_set(requests_mock)
    requests_mock.register_uri(
        "PROPFIND", f"{BASE}/877060579/carddavhome/",
        additional_matcher=_is_depth("1"),
        text="""<?xml version="1.0" encoding="utf-8" ?>
<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:carddav">
  <D:response>
    <D:href>/877060579/carddavhome/card/</D:href>
    <D:propstat>
      <D:prop><D:resourcetype><D:collection/><C:addressbook/></D:resourcetype></D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
</D:multistatus>""",
        status_code=207,
    )

    discover_addressbook_path("me@icloud.com", "app-pass")

    depth_1_requests = [
        r for r in requests_mock.request_history
        if r.url == f"{BASE}/877060579/carddavhome/" and r.headers.get("Depth") == "1"
    ]
    assert len(depth_1_requests) == 1


def test_discover_addressbook_path_raises_when_no_addressbook_collection_found(requests_mock):
    """If Depth:1 enumeration returns children but none of them has a
    resourcetype containing carddav:addressbook, discovery must fail loudly
    rather than silently returning the (non-queryable) home-set URL again.
    """
    _register_principal_and_home_set(requests_mock)
    requests_mock.register_uri(
        "PROPFIND", f"{BASE}/877060579/carddavhome/",
        additional_matcher=_is_depth("1"),
        text="""<?xml version="1.0" encoding="utf-8" ?>
<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:carddav">
  <D:response>
    <D:href>/877060579/carddavhome/</D:href>
    <D:propstat>
      <D:prop><D:resourcetype><D:collection/></D:resourcetype></D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
</D:multistatus>""",
        status_code=207,
    )

    with pytest.raises(RuntimeError, match="addressbook"):
        discover_addressbook_path("me@icloud.com", "app-pass")


def test_discover_addressbook_path_prefers_card_suffixed_collection_when_multiple_match(requests_mock):
    """Some accounts have multiple addressbook-typed collections within the
    home-set (e.g. a "Shared" addressbook alongside the default one). When
    more than one child's resourcetype matches carddav:addressbook, prefer
    whichever href ends in "/card/", matching Apple's known default-collection
    convention, rather than arbitrarily picking whichever appears first.
    """
    _register_principal_and_home_set(requests_mock)
    requests_mock.register_uri(
        "PROPFIND", f"{BASE}/877060579/carddavhome/",
        additional_matcher=_is_depth("1"),
        text="""<?xml version="1.0" encoding="utf-8" ?>
<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:carddav">
  <D:response>
    <D:href>/877060579/carddavhome/shared/</D:href>
    <D:propstat>
      <D:prop><D:resourcetype><D:collection/><C:addressbook/></D:resourcetype></D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
  <D:response>
    <D:href>/877060579/carddavhome/card/</D:href>
    <D:propstat>
      <D:prop><D:resourcetype><D:collection/><C:addressbook/></D:resourcetype></D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
</D:multistatus>""",
        status_code=207,
    )

    path = discover_addressbook_path("me@icloud.com", "app-pass")

    assert path == f"{BASE}/877060579/carddavhome/card/"


def test_discover_addressbook_path_takes_first_addressbook_match_when_none_end_in_card(requests_mock):
    """When multiple addressbook-typed collections are found but none of them
    has an href ending in "/card/", fall back to just taking the first match
    rather than raising or guessing further.
    """
    _register_principal_and_home_set(requests_mock)
    requests_mock.register_uri(
        "PROPFIND", f"{BASE}/877060579/carddavhome/",
        additional_matcher=_is_depth("1"),
        text="""<?xml version="1.0" encoding="utf-8" ?>
<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:carddav">
  <D:response>
    <D:href>/877060579/carddavhome/default/</D:href>
    <D:propstat>
      <D:prop><D:resourcetype><D:collection/><C:addressbook/></D:resourcetype></D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
  <D:response>
    <D:href>/877060579/carddavhome/shared/</D:href>
    <D:propstat>
      <D:prop><D:resourcetype><D:collection/><C:addressbook/></D:resourcetype></D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
</D:multistatus>""",
        status_code=207,
    )

    path = discover_addressbook_path("me@icloud.com", "app-pass")

    assert path == f"{BASE}/877060579/carddavhome/default/"


def test_discover_addressbook_path_raises_on_enumeration_request_failure(requests_mock):
    """The third discovery step's network call must also be wrapped as a
    RuntimeError on failure, following the established pattern for the other
    two discovery steps.
    """
    _register_principal_and_home_set(requests_mock)
    requests_mock.register_uri(
        "PROPFIND", f"{BASE}/877060579/carddavhome/",
        additional_matcher=_is_depth("1"),
        status_code=401,
        text="Unauthorized",
    )

    with pytest.raises(RuntimeError):
        discover_addressbook_path("me@icloud.com", "app-pass")


def test_discover_addressbook_path_resolves_absolute_url_href(requests_mock):
    """Regression test: Apple's CardDAV server returns the addressbook-home-set
    href as a FULL absolute URL pointing at a per-account sharded hostname
    (e.g. p119-contacts.icloud.com), not a path relative to contacts.icloud.com.
    discover_addressbook_path must resolve this correctly via urljoin rather
    than naively concatenating it onto BASE_URL (which previously produced a
    mangled URL like "https://contacts.icloud.comhttps://p119-...").
    """
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
          <D:href>https://p119-contacts.icloud.com:443/877060579/carddavhome/</D:href>
        </C:addressbook-home-set>
      </D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
</D:multistatus>""",
        status_code=207,
    )
    requests_mock.register_uri(
        "PROPFIND", "https://p119-contacts.icloud.com:443/877060579/carddavhome/",
        additional_matcher=_is_depth("1"),
        text="""<?xml version="1.0" encoding="utf-8" ?>
<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:carddav">
  <D:response>
    <D:href>/877060579/carddavhome/card/</D:href>
    <D:propstat>
      <D:prop><D:resourcetype><D:collection/><C:addressbook/></D:resourcetype></D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
</D:multistatus>""",
        status_code=207,
    )

    path = discover_addressbook_path("me@icloud.com", "app-pass")

    assert path == "https://p119-contacts.icloud.com:443/877060579/carddavhome/card/"


def test_discover_addressbook_path_resolves_absolute_url_principal_href(requests_mock):
    """Defensive test for the same class of bug on the FIRST hop: if the
    current-user-principal href is itself returned as a full absolute URL
    (rather than a relative path), the second PROPFIND request must be sent
    to that resolved URL, not a mangled BASE_URL + href concatenation.
    """
    requests_mock.register_uri(
        "PROPFIND", f"{BASE}/",
        text="""<?xml version="1.0" encoding="utf-8" ?>
<D:multistatus xmlns:D="DAV:">
  <D:response>
    <D:href>/</D:href>
    <D:propstat>
      <D:prop>
        <D:current-user-principal>
          <D:href>https://p119-contacts.icloud.com:443/1234567890/principal/</D:href>
        </D:current-user-principal>
      </D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
</D:multistatus>""",
        status_code=207,
    )
    requests_mock.register_uri(
        "PROPFIND", "https://p119-contacts.icloud.com:443/1234567890/principal/",
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
    requests_mock.register_uri(
        "PROPFIND", "https://p119-contacts.icloud.com:443/1234567890/carddavhome/",
        additional_matcher=_is_depth("1"),
        text="""<?xml version="1.0" encoding="utf-8" ?>
<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:carddav">
  <D:response>
    <D:href>/1234567890/carddavhome/card/</D:href>
    <D:propstat>
      <D:prop><D:resourcetype><D:collection/><C:addressbook/></D:resourcetype></D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
</D:multistatus>""",
        status_code=207,
    )

    path = discover_addressbook_path("me@icloud.com", "app-pass")

    # The second-hop href was relative, but it must resolve against the
    # sharded principal URL from the first hop, not against BASE_URL. The
    # third-hop (Depth:1 enumeration) href is also relative and must resolve
    # against the home-set URL from the second hop.
    assert path == "https://p119-contacts.icloud.com:443/1234567890/carddavhome/card/"


def test_icloud_adapter_uses_full_absolute_url_addressbook_path_as_is(requests_mock):
    """Regression test: when ICloudAdapter is constructed with a FULL absolute
    URL as addressbook_path (as discover_addressbook_path now returns for
    per-account sharded servers), it must use that URL as-is rather than
    re-prepending BASE_URL (which would produce a mangled URL like
    "https://contacts.icloud.comhttps://p119-...").
    """
    sharded_addressbook = "https://p119-contacts.icloud.com:443/877060579/carddavhome/"
    requests_mock.register_uri("REPORT", sharded_addressbook, text=SYNC_RESPONSE, status_code=207)
    adapter = ICloudAdapter("me@icloud.com", "app-pass", sharded_addressbook)

    change_set = adapter.list_changes(None)

    assert requests_mock.last_request.url == sharded_addressbook
    assert len(change_set.changes) == 1


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

    with pytest.raises(RuntimeError):
        discover_addressbook_path("me@icloud.com", "bad-pass")


def test_update_raises_resource_gone_on_404(requests_mock):
    from contacts_sync.adapters.base import ProviderResourceGoneError
    href = f"{BASE}{ADDRESSBOOK}gone.vcf"
    requests_mock.put(href, status_code=404)
    adapter = ICloudAdapter("me@icloud.com", "app-pass", ADDRESSBOOK)
    with pytest.raises(ProviderResourceGoneError):
        adapter.update(href, CanonicalContact(id=5, display_name="Jane", emails=[Email(value="j@e.com")]))


def test_to_canonical_decodes_embedded_photo():
    vcard = vobject.readOne(
        "BEGIN:VCARD\nVERSION:3.0\nFN:Jane Doe\n"
        "PHOTO;ENCODING=b;TYPE=JPEG:ZmFrZS1qcGVnLWJ5dGVz\nEND:VCARD\n"
    )

    canonical = _to_canonical(vcard)

    assert canonical.photo_data == b"fake-jpeg-bytes"
    assert canonical.photo_content_type == "image/jpeg"


def test_to_canonical_handles_vcard_without_photo():
    vcard = vobject.readOne("BEGIN:VCARD\nVERSION:3.0\nFN:Jane Doe\nEND:VCARD\n")

    canonical = _to_canonical(vcard)

    assert canonical.photo_data is None
    assert canonical.photo_content_type is None


def test_to_vcard_embeds_photo_when_present():
    contact = CanonicalContact(id=1, display_name="Jane Doe", photo_data=b"fake-png-bytes", photo_content_type="image/png")

    vcard = _to_vcard(contact)

    assert hasattr(vcard, "photo")
    assert vcard.photo.type_param == "PNG"
    # Round-trip through serialize/parse to confirm the base64 encoding is correct.
    parsed = vobject.readOne(vcard.serialize())
    assert parsed.photo.value == b"fake-png-bytes"


def test_to_vcard_omits_photo_when_absent():
    contact = CanonicalContact(id=1, display_name="Jane Doe")

    vcard = _to_vcard(contact)

    assert not hasattr(vcard, "photo")
