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

import defusedxml.ElementTree as ET
import vobject

from contacts_sync.adapters.base import ChangeSet, ChangedContact, SyncTokenExpiredError
from contacts_sync.http_retry import request_with_retry
from contacts_sync.models import CanonicalContact, Email, Phone

BASE_URL = "https://contacts.icloud.com"
NS = {"D": "DAV:", "C": "urn:ietf:params:xml:ns:carddav"}

SYNC_COLLECTION_BODY = """<?xml version="1.0" encoding="utf-8" ?>
<C:sync-collection xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:carddav">
  <D:sync-token>{sync_token}</D:sync-token>
  <D:sync-level>1</D:sync-level>
  <D:prop>
    <D:getetag/>
    <C:address-data/>
  </D:prop>
</C:sync-collection>"""


class ICloudAdapter:
    name = "icloud"

    def __init__(self, apple_id: str, app_password: str, addressbook_path: str):
        self._auth = (apple_id, app_password)
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
