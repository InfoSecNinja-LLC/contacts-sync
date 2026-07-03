"""Microsoft Graph contacts adapter.

Maps between Microsoft Graph's contact JSON shape and the canonical contact
model, and drives Graph's delta-query pagination for `list_changes`. No auth
logic lives here - the constructor takes a zero-argument `token_provider`
callable (see `contacts_sync.auth.microsoft_auth.get_token_provider`) and
calls it to get a fresh bearer token per request.
"""

from contacts_sync.adapters.base import ChangeSet, ChangedContact, SyncTokenExpiredError
from contacts_sync.http_retry import request_with_retry
from contacts_sync.models import CanonicalContact, Email, Phone

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
CONTACT_SELECT = (
    "id,displayName,givenName,surname,emailAddresses,businessPhones,mobilePhone,"
    "companyName,jobTitle,personalNotes,categories,lastModifiedDateTime"
)


class MicrosoftAdapter:
    name = "microsoft"

    def __init__(self, token_provider):
        self._token_provider = token_provider

    def _headers(self):
        return {"Authorization": f"Bearer {self._token_provider()}", "Content-Type": "application/json"}

    def list_changes(self, since_token):
        if since_token and since_token.startswith("http"):
            url = since_token
            params = None
        else:
            url = f"{GRAPH_BASE}/me/contactFolders/contacts/contacts/delta"
            params = {"$select": CONTACT_SELECT}

        changes = []
        next_token = since_token
        while url:
            response = request_with_retry("GET", url, headers=self._headers(), params=params)
            params = None
            if response.status_code == 410:
                raise SyncTokenExpiredError("Microsoft delta token expired (syncStateNotFound)")
            response.raise_for_status()
            body = response.json()
            for item in body.get("value", []):
                if "@removed" in item:
                    changes.append(ChangedContact(provider_id=item["id"], contact=None, updated_at="", deleted=True))
                    continue
                changes.append(
                    ChangedContact(
                        provider_id=item["id"],
                        contact=_to_canonical(item),
                        updated_at=item.get("lastModifiedDateTime", ""),
                    )
                )
            url = body.get("@odata.nextLink")
            if "@odata.deltaLink" in body:
                next_token = body["@odata.deltaLink"]

        return ChangeSet(changes=changes, next_sync_token=next_token)

    def create(self, contact: CanonicalContact) -> str:
        response = request_with_retry(
            "POST", f"{GRAPH_BASE}/me/contacts", headers=self._headers(), json=_to_graph(contact)
        )
        response.raise_for_status()
        return response.json()["id"]

    def update(self, provider_id: str, contact: CanonicalContact) -> None:
        response = request_with_retry(
            "PATCH", f"{GRAPH_BASE}/me/contacts/{provider_id}", headers=self._headers(), json=_to_graph(contact)
        )
        response.raise_for_status()

    def delete(self, provider_id: str) -> None:
        response = request_with_retry("DELETE", f"{GRAPH_BASE}/me/contacts/{provider_id}", headers=self._headers())
        if response.status_code not in (204, 404):
            response.raise_for_status()


def _to_canonical(item: dict) -> CanonicalContact:
    emails = [Email(value=e["address"]) for e in item.get("emailAddresses", [])]
    phones = [Phone(value=p, type="business") for p in item.get("businessPhones", [])]
    if item.get("mobilePhone"):
        phones.append(Phone(value=item["mobilePhone"], type="mobile"))
    return CanonicalContact(
        display_name=item.get("displayName") or "",
        given_name=item.get("givenName"),
        family_name=item.get("surname"),
        emails=emails,
        phones=phones,
        notes=item.get("personalNotes"),
        organization=item.get("companyName"),
        title=item.get("jobTitle"),
        groups=item.get("categories", []),
    )


def _to_graph(contact: CanonicalContact) -> dict:
    # Graph/Outlook contacts support at most 3 email addresses (Email1/Email2/
    # Email3); sending more is rejected the same way businessPhones overflow is.
    # Overflow emails are dropped for this provider (canonical store keeps them all).
    emails = [{"address": e.value, "name": contact.display_name} for e in contact.emails][:3]
    body = {
        "displayName": contact.display_name,
        "givenName": contact.given_name,
        "surname": contact.family_name,
        "emailAddresses": emails,
        "companyName": contact.organization,
        "jobTitle": contact.title,
        "categories": contact.groups,
    }
    if contact.notes:
        body["personalNotes"] = contact.notes
    # Graph enforces a hard maximum of 2 entries for businessPhones (documented
    # limit; confirmed live with "...exceeds the max allowed value of 2").
    # contacts-sync's canonical model doesn't distinguish home/business phones,
    # so overflow numbers are simply dropped for this provider rather than
    # invented a routing scheme Graph's schema doesn't support either way
    # (homePhones is also capped at 2).
    business_phones = [p.value for p in contact.phones if p.type != "mobile"][:2]
    mobile = next((p.value for p in contact.phones if p.type == "mobile"), None)
    if business_phones:
        body["businessPhones"] = business_phones
    if mobile:
        body["mobilePhone"] = mobile
    return body
