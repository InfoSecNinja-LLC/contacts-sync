from googleapiclient.discovery import build

from contacts_sync.adapters.base import ChangedContact, ChangeSet, SyncTokenExpiredError
from contacts_sync.models import CanonicalContact, Email, Phone

PERSON_FIELDS = "names,emailAddresses,phoneNumbers,addresses,biographies,organizations"


class GoogleAdapter:
    name = "google"

    def __init__(self, credentials):
        self._service = build("people", "v1", credentials=credentials)

    def list_changes(self, since_token):
        changes = []
        page_token = None
        next_sync_token = since_token
        request_args = {"resourceName": "people/me", "personFields": PERSON_FIELDS, "pageSize": 200}
        if since_token:
            request_args["syncToken"] = since_token
        else:
            request_args["requestSyncToken"] = True

        try:
            while True:
                if page_token:
                    request_args["pageToken"] = page_token
                response = self._service.people().connections().list(**request_args).execute()
                for person in response.get("connections", []):
                    if person.get("metadata", {}).get("deleted"):
                        changes.append(
                            ChangedContact(provider_id=person["resourceName"], contact=None, updated_at="", deleted=True)
                        )
                        continue
                    changes.append(
                        ChangedContact(
                            provider_id=person["resourceName"],
                            contact=_to_canonical(person),
                            updated_at="",
                        )
                    )
                page_token = response.get("nextPageToken")
                if "nextSyncToken" in response:
                    next_sync_token = response["nextSyncToken"]
                if not page_token:
                    break
        except Exception as exc:
            if "EXPIRED_SYNC_TOKEN" in str(exc) or "410" in str(exc):
                raise SyncTokenExpiredError(str(exc)) from exc
            raise

        return ChangeSet(changes=changes, next_sync_token=next_sync_token)

    def create(self, contact: CanonicalContact) -> str:
        body = _to_person(contact)
        response = self._service.people().createContact(body=body).execute()
        return response["resourceName"]

    def update(self, provider_id: str, contact: CanonicalContact) -> None:
        body = _to_person(contact)
        self._service.people().updateContact(
            resourceName=provider_id, updatePersonFields=PERSON_FIELDS, body=body
        ).execute()

    def delete(self, provider_id: str) -> None:
        self._service.people().deleteContact(resourceName=provider_id).execute()


def _to_canonical(person: dict) -> CanonicalContact:
    names = person.get("names", [{}])[0] if person.get("names") else {}
    emails = [Email(value=e["value"]) for e in person.get("emailAddresses", [])]
    phones = [Phone(value=p["value"]) for p in person.get("phoneNumbers", [])]
    notes = person.get("biographies", [{}])[0].get("value") if person.get("biographies") else None
    return CanonicalContact(
        display_name=names.get("displayName", ""),
        given_name=names.get("givenName"),
        family_name=names.get("familyName"),
        emails=emails,
        phones=phones,
        notes=notes,
    )


def _to_person(contact: CanonicalContact) -> dict:
    body = {
        "names": [{"givenName": contact.given_name or "", "familyName": contact.family_name or ""}],
        "emailAddresses": [{"value": e.value} for e in contact.emails],
        "phoneNumbers": [{"value": p.value} for p in contact.phones],
    }
    if contact.notes:
        body["biographies"] = [{"value": contact.notes}]
    return body
