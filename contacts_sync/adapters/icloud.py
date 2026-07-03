"""iCloud CardDAV contacts adapter.

Apple has no REST API for iCloud Contacts - the only supported access path is
CardDAV (RFC 6352) at https://contacts.icloud.com/, authenticated via HTTP
Basic Auth using an app-specific password. This module has one
responsibility: CardDAV protocol handling and vCard <-> CanonicalContact
mapping. No auth-flow logic lives here - the constructor just takes the
already-collected `apple_id`/`app_password`/`addressbook_path` (see
`contacts_sync.auth.icloud_auth.get_credentials`, which the CLI layer calls
to obtain the first two).

XML parsing uses `defusedxml.ElementTree`, not the stdlib
`xml.etree.ElementTree`, because this XML comes from a network response and
stdlib XML parsers are vulnerable to XXE and billion-laughs attacks by
default. Do not change this back to stdlib XML.
"""

from urllib.parse import urljoin

import defusedxml.ElementTree as ET
import requests
import vobject

from contacts_sync.adapters.base import ChangeSet, ChangedContact, SyncTokenExpiredError
from contacts_sync.http_retry import request_with_retry
from contacts_sync.models import CanonicalContact, Email, Phone

BASE_URL = "https://contacts.icloud.com"
NS = {"D": "DAV:", "C": "urn:ietf:params:xml:ns:carddav"}

SYNC_COLLECTION_BODY = """<?xml version="1.0" encoding="utf-8" ?>
<D:sync-collection xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:carddav">
  <D:sync-token>{sync_token}</D:sync-token>
  <D:sync-level>1</D:sync-level>
  <D:prop>
    <D:getetag/>
    <C:address-data/>
  </D:prop>
</D:sync-collection>"""

PRINCIPAL_PROPFIND_BODY = """<?xml version="1.0" encoding="utf-8" ?>
<D:propfind xmlns:D="DAV:">
  <D:prop>
    <D:current-user-principal/>
  </D:prop>
</D:propfind>"""

ADDRESSBOOK_HOME_PROPFIND_BODY = """<?xml version="1.0" encoding="utf-8" ?>
<D:propfind xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:carddav">
  <D:prop>
    <C:addressbook-home-set/>
  </D:prop>
</D:propfind>"""

ADDRESSBOOK_COLLECTION_PROPFIND_BODY = """<?xml version="1.0" encoding="utf-8" ?>
<D:propfind xmlns:D="DAV:">
  <D:prop>
    <D:resourcetype/>
    <D:displayname/>
  </D:prop>
</D:propfind>"""

CARDDAV_ADDRESSBOOK_TAG = "{urn:ietf:params:xml:ns:carddav}addressbook"


class ICloudAdapter:
    name = "icloud"

    def __init__(self, apple_id: str, app_password: str, addressbook_path: str):
        """`addressbook_path` may be either a bare path (e.g.
        "/carddavhome/addressbooks/card/") relative to BASE_URL, or a full
        absolute URL (e.g. "https://p119-contacts.icloud.com:443/.../carddavhome/").
        Apple's CardDAV discovery (see `discover_addressbook_path`) returns a
        full absolute URL when the account's addressbook lives on a
        per-account sharded server rather than contacts.icloud.com itself, so
        both forms must be accepted here.
        """
        self._auth = (apple_id, app_password)
        if addressbook_path.startswith("http://") or addressbook_path.startswith("https://"):
            self._addressbook_url = addressbook_path
        else:
            self._addressbook_url = f"{BASE_URL}{addressbook_path}"

    def list_changes(self, since_token):
        body = SYNC_COLLECTION_BODY.format(sync_token=since_token or "")
        response = request_with_retry(
            "REPORT",
            self._addressbook_url,
            data=body,
            auth=self._auth,
            headers={"Content-Type": "application/xml; charset=utf-8", "Depth": "1"},
        )
        if response.status_code == 507 or "valid-sync-token" in response.text:
            raise SyncTokenExpiredError("iCloud sync token invalid, full resync required")
        response.raise_for_status()

        changes = []
        for href, status, _etag, address_data in _parse_multistatus(response.text):
            if status.startswith("404"):
                changes.append(ChangedContact(provider_id=href, contact=None, updated_at="", deleted=True))
                continue
            if not address_data:
                # Not a vCard resource — e.g. the addressbook collection's own
                # entry in the multistatus (RFC 6578 includes the collection
                # itself alongside member resources; its propstat/prop has no
                # C:address-data since a collection isn't a vCard). Skip it
                # rather than crashing the whole sync run on vobject.readOne("").
                continue
            vcard = vobject.readOne(address_data)
            changes.append(ChangedContact(provider_id=href, contact=_to_canonical(vcard), updated_at=""))

        return ChangeSet(changes=changes, next_sync_token=_extract_sync_token(response.text))

    def create(self, contact: CanonicalContact) -> str:
        vcard = _to_vcard(contact)
        href = f"{self._addressbook_url}{contact.id}.vcf"
        response = request_with_retry(
            "PUT",
            href,
            data=vcard.serialize(),
            auth=self._auth,
            headers={
                "Content-Type": "text/vcard; charset=utf-8",
                # Only create if no resource currently exists at this URL. This is
                # the standard WebDAV/CardDAV mechanism for guarding against
                # silently overwriting a stray/unlinked resource left over from a
                # prior partial sync or a manually created contact with a
                # colliding filename. If the server already has a resource here,
                # it responds 412 Precondition Failed, which raise_for_status()
                # below turns into a clear HTTPError.
                "If-None-Match": '"*"',
            },
        )
        response.raise_for_status()
        return href

    def update(self, provider_id: str, contact: CanonicalContact) -> None:
        vcard = _to_vcard(contact)
        response = request_with_retry(
            "PUT",
            provider_id,
            data=vcard.serialize(),
            auth=self._auth,
            headers={"Content-Type": "text/vcard; charset=utf-8"},
        )
        response.raise_for_status()

    def delete(self, provider_id: str) -> None:
        response = request_with_retry("DELETE", provider_id, auth=self._auth)
        if response.status_code not in (204, 404):
            response.raise_for_status()


def _parse_multistatus(xml_text: str):
    root = ET.fromstring(xml_text)
    results = []
    for response in root.findall("D:response", NS):
        href = response.findtext("D:href", default="", namespaces=NS)
        propstat = response.find("D:propstat", NS)
        # No propstat means this D:response is a deleted-resource entry per RFC
        # 6578's typical shape (status is a direct child of D:response, not
        # nested under propstat). We deliberately treat "no propstat" as
        # "deleted" via this hardcoded fallback rather than parsing a status
        # value in that branch.
        status = propstat.findtext("D:status", default="200", namespaces=NS) if propstat is not None else "404"
        status_code = status.split()[1] if len(status.split()) > 1 else status
        etag = propstat.findtext("D:prop/D:getetag", default="", namespaces=NS) if propstat is not None else ""
        address_data = (
            propstat.findtext("D:prop/C:address-data", default="", namespaces=NS)
            if propstat is not None
            else ""
        )
        results.append((href, status_code, etag, address_data))
    return results


def _extract_sync_token(xml_text: str) -> str:
    root = ET.fromstring(xml_text)
    return root.findtext("D:sync-token", default="", namespaces=NS)


def _extract_propstat_href(xml_text: str, property_path: str) -> str:
    """Navigate a PROPFIND multistatus response to find a D:href nested under
    the given property (e.g. "D:current-user-principal" or
    "C:addressbook-home-set"). The href is a child element of the property,
    not the property's own text content, per RFC 4918/6764.
    """
    root = ET.fromstring(xml_text)
    for response in root.findall("D:response", NS):
        propstat = response.find("D:propstat", NS)
        if propstat is None:
            continue
        prop_element = propstat.find(f"D:prop/{property_path}", NS)
        if prop_element is None:
            continue
        href = prop_element.findtext("D:href", default="", namespaces=NS)
        if href:
            return href
    return ""


def discover_addressbook_path(apple_id: str, app_password: str) -> str:
    """Discover this account's real, queryable CardDAV addressbook collection URL.

    Replaces guessing a hardcoded path (which only works for some accounts) with
    the standard three-step discovery: current-user-principal, then
    addressbook-home-set, then enumerating the home-set's children to find the
    actual addressbook collection within it.

    The addressbook-home-set is a CONTAINER resource, not itself a queryable
    addressbook — issuing sync-collection REPORT requests directly against it
    produces a 400 Bad Request from Apple's server. Per RFC 6352, a third
    PROPFIND (Depth: 1) on the home-set is required to enumerate its children
    and identify which one is actually addressbook-typed.

    Raises RuntimeError with a clear message if any step fails or the expected
    property is missing from the response.
    """
    auth = (apple_id, app_password)

    try:
        principal_response = request_with_retry(
            "PROPFIND",
            BASE_URL + "/",
            data=PRINCIPAL_PROPFIND_BODY,
            auth=auth,
            headers={"Content-Type": "application/xml; charset=utf-8", "Depth": "0"},
        )
        principal_response.raise_for_status()
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(
            "Failed to discover iCloud CardDAV principal — check that the app-specific "
            "password is valid and hasn't been revoked."
        ) from exc

    principal_href = _extract_propstat_href(principal_response.text, "D:current-user-principal")
    if not principal_href:
        raise RuntimeError(
            "Could not discover iCloud CardDAV principal URL — the account may not "
            "support CardDAV, or the app-specific password may be invalid."
        )
    # Per RFC 4918, a DAV:href value MAY be a full absolute URI or a
    # path-only relative reference. urljoin handles both: if principal_href
    # is already absolute it's returned unchanged; if relative, it's joined
    # against BASE_URL. Naively concatenating BASE_URL + principal_href would
    # mangle the URL in the absolute case (e.g.
    # "https://contacts.icloud.comhttps://p119-...").
    principal_url = urljoin(BASE_URL + "/", principal_href)

    try:
        home_response = request_with_retry(
            "PROPFIND",
            principal_url,
            data=ADDRESSBOOK_HOME_PROPFIND_BODY,
            auth=auth,
            headers={"Content-Type": "application/xml; charset=utf-8", "Depth": "0"},
        )
        home_response.raise_for_status()
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(
            "Failed to discover iCloud CardDAV addressbook home — check that the "
            "app-specific password is valid and hasn't been revoked."
        ) from exc

    home_href = _extract_propstat_href(home_response.text, "C:addressbook-home-set")
    if not home_href:
        raise RuntimeError(
            "Could not discover iCloud CardDAV addressbook home — the principal "
            "response didn't include an addressbook-home-set."
        )
    # Apple's CardDAV server frequently returns this href as a FULL absolute
    # URL pointing at a per-account sharded hostname (e.g.
    # p119-contacts.icloud.com), not a path relative to contacts.icloud.com.
    # Resolve via urljoin so the caller always receives a usable absolute URL
    # regardless of which form the server chose.
    home_set_url = urljoin(principal_url, home_href)

    return _discover_addressbook_collection(home_set_url, auth)


def _discover_addressbook_collection(home_set_url: str, auth) -> str:
    """Enumerate the addressbook-home-set's immediate children (Depth: 1) and
    return the URL of whichever child is actually addressbook-typed.

    The home-set is a container that may hold one or more addressbook
    collections (e.g. a default collection plus a "Shared" addressbook on
    some accounts) — it is not itself queryable via sync-collection REPORT.
    A child is identified as an addressbook by its DAV:resourcetype property
    containing a {urn:ietf:params:xml:ns:carddav}addressbook element. If more
    than one child matches, prefer one whose href ends in "/card/" (Apple's
    known default-collection convention); otherwise take the first match.
    """
    try:
        response = request_with_retry(
            "PROPFIND",
            home_set_url,
            data=ADDRESSBOOK_COLLECTION_PROPFIND_BODY,
            auth=auth,
            headers={"Content-Type": "application/xml; charset=utf-8", "Depth": "1"},
        )
        response.raise_for_status()
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(
            "Failed to enumerate iCloud CardDAV addressbook home's collections — "
            "check that the app-specific password is valid and hasn't been revoked."
        ) from exc

    root = ET.fromstring(response.text)
    matches = []
    for child_response in root.findall("D:response", NS):
        propstat = child_response.find("D:propstat", NS)
        if propstat is None:
            continue
        resourcetype = propstat.find("D:prop/D:resourcetype", NS)
        if resourcetype is None:
            continue
        if resourcetype.find(CARDDAV_ADDRESSBOOK_TAG) is None:
            continue
        href = child_response.findtext("D:href", default="", namespaces=NS)
        if href:
            matches.append(href)

    if not matches:
        raise RuntimeError(
            "Could not discover iCloud CardDAV addressbook collection — none of "
            "the addressbook-home-set's children have a resourcetype matching "
            "carddav:addressbook."
        )

    preferred = next((href for href in matches if href.endswith("/card/")), matches[0])
    return urljoin(home_set_url, preferred)


def _to_canonical(vcard) -> CanonicalContact:
    emails = [Email(value=e.value) for e in getattr(vcard, "email_list", [])]
    phones = [Phone(value=t.value) for t in getattr(vcard, "tel_list", [])]
    given_name = None
    family_name = None
    if hasattr(vcard, "n"):
        given_name = vcard.n.value.given or None
        family_name = vcard.n.value.family or None
    return CanonicalContact(
        display_name=vcard.fn.value if hasattr(vcard, "fn") else "",
        given_name=given_name,
        family_name=family_name,
        emails=emails,
        phones=phones,
        notes=vcard.note.value if hasattr(vcard, "note") else None,
    )


def _to_vcard(contact: CanonicalContact):
    vcard = vobject.vCard()
    vcard.add("fn").value = contact.display_name
    name = vcard.add("n")
    name.value = vobject.vcard.Name(family=contact.family_name or "", given=contact.given_name or "")
    for email in contact.emails:
        vcard.add("email").value = email.value
    for phone in contact.phones:
        vcard.add("tel").value = phone.value
    if contact.notes:
        vcard.add("note").value = contact.notes
    return vcard
