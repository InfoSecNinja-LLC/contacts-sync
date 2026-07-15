import base64
import hashlib
import re
from typing import Optional

import requests
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from contacts_sync.adapters.base import (
    ChangedContact,
    ChangeSet,
    ProviderResourceGoneError,
    SyncTokenExpiredError,
)
from contacts_sync.models import CanonicalContact, Email, Phone

PERSON_FIELDS = "names,emailAddresses,phoneNumbers,biographies,photos,metadata"
# updateContact's updatePersonFields mask rejects "photos" - Google's API
# returns 400 "Invalid updatePersonFields mask path: photos" since a contact's
# photo isn't settable via updateContact at all, only via the separate
# updateContactPhoto call (already used below for pushing photo_data).
UPDATE_PERSON_FIELDS = "names,emailAddresses,phoneNumbers,biographies"


class GoogleAdapter:
    name = "google"

    def __init__(self, credentials):
        self._credentials = credentials
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
                response = self._service.people().connections().list(**request_args).execute(num_retries=5)
                for person in response.get("connections", []):
                    if person.get("metadata", {}).get("deleted"):
                        changes.append(
                            ChangedContact(provider_id=person["resourceName"], contact=None, updated_at="", deleted=True)
                        )
                        continue
                    changes.append(
                        ChangedContact(
                            provider_id=person["resourceName"],
                            contact=_to_canonical(person, self._credentials.token),
                            updated_at=_person_updated_at(person),
                            etag=person.get("etag"),
                        )
                    )
                page_token = response.get("nextPageToken")
                if "nextSyncToken" in response:
                    next_sync_token = response["nextSyncToken"]
                if not page_token:
                    break
        except HttpError as exc:
            status = exc.resp.status if hasattr(exc, "resp") else None
            if status == 410 or "EXPIRED_SYNC_TOKEN" in str(exc):
                raise SyncTokenExpiredError(str(exc)) from exc
            raise

        return ChangeSet(changes=changes, next_sync_token=next_sync_token)

    def create(self, contact: CanonicalContact) -> tuple[str, Optional[str]]:
        body = _to_person(contact)
        response = self._service.people().createContact(body=body).execute(num_retries=5)
        resource_name = response["resourceName"]
        if contact.photo_data:
            self._service.people().updateContactPhoto(
                resourceName=resource_name,
                body={"photoBytes": base64.b64encode(contact.photo_data).decode()},
            ).execute(num_retries=5)
            contact.extra["google_pushed_photo_sha"] = hashlib.sha256(contact.photo_data).hexdigest()
        return resource_name, response.get("etag")

    def update(self, provider_id: str, contact: CanonicalContact) -> Optional[str]:
        body = _to_person(contact)
        etag = contact.extra.get("google_etag")
        if etag:
            # The People API requires person.etag (or
            # person.metadata.sources.etag) to be set on every updateContact
            # request for optimistic concurrency - omitting it produces a 400
            # "Request must set person.etag ...". create() must NOT send this:
            # a brand-new contact has no prior etag to send.
            body["etag"] = etag
        try:
            response = self._service.people().updateContact(
                resourceName=provider_id, updatePersonFields=UPDATE_PERSON_FIELDS, body=body
            ).execute(num_retries=5)
        except HttpError as exc:
            if _is_not_found(exc):
                raise ProviderResourceGoneError(str(exc)) from exc
            if not _is_etag_conflict(exc):
                raise
            # The cached etag is stale. Google's own error message says to
            # "Clear local cache and get the latest person." Re-fetch the
            # current person to obtain a fresh top-level etag, substitute it
            # into the request body, and retry the update exactly once.
            fresh_person = (
                self._service.people()
                .get(resourceName=provider_id, personFields=PERSON_FIELDS)
                .execute(num_retries=5)
            )
            body["etag"] = fresh_person["etag"]
            response = self._service.people().updateContact(
                resourceName=provider_id, updatePersonFields=UPDATE_PERSON_FIELDS, body=body
            ).execute(num_retries=5)
        if contact.photo_data:
            self._service.people().updateContactPhoto(
                resourceName=provider_id,
                body={"photoBytes": base64.b64encode(contact.photo_data).decode()},
            ).execute(num_retries=5)
            contact.extra["google_pushed_photo_sha"] = hashlib.sha256(contact.photo_data).hexdigest()
        return response.get("etag") if isinstance(response, dict) else None

    def delete(self, provider_id: str) -> None:
        self._service.people().deleteContact(resourceName=provider_id).execute(num_retries=5)

    def fetch_contact_photo_urls(self, provider_ids: list) -> dict:
        """Map provider_id -> URL of the USER-SET (CONTACT-sourced) photo.

        Contacts without a user-set photo are omitted - PROFILE photos are
        deliberately excluded here because this feeds the fix-photos repair,
        which must trust nothing but photos the user set themselves. Uses
        getBatchGet (200 per call) to stay well inside People API quotas.
        """
        urls = {}
        for start in range(0, len(provider_ids), 200):
            chunk = provider_ids[start:start + 200]
            response = self._service.people().getBatchGet(
                resourceNames=chunk, personFields="photos"
            ).execute(num_retries=5)
            for item in response.get("responses", []):
                person = item.get("person") or {}
                provider_id = item.get("requestedResourceName") or person.get("resourceName")
                photo = _pick_photo(person.get("photos", []), contact_only=True)
                if provider_id and photo:
                    urls[provider_id] = photo["url"]
        return urls

    def download_photo(self, url: str) -> tuple[bytes, Optional[str]]:
        """Download a photo URL at original size using the adapter's token."""
        response = requests.get(
            _full_size_photo_url(url),
            headers={"Authorization": f"Bearer {self._credentials.token}"},
        )
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "").split(";")[0].strip() or None
        return response.content, content_type


def _is_etag_conflict(exc: HttpError) -> bool:
    status = exc.resp.status if hasattr(exc, "resp") else None
    return status == 400 and "etag" in str(exc).lower()


def _is_not_found(exc: HttpError) -> bool:
    status = exc.resp.status if hasattr(exc, "resp") else None
    return status == 404


def _pick_photo(photos: list, contact_only: bool = False) -> Optional[dict]:
    """Choose which of a person's photos to sync.

    A person's `photos` can contain both a CONTACT-sourced photo (set by the
    user on the contact itself) and a PROFILE-sourced photo (the avatar of
    whatever Google ACCOUNT Google linked to one of the contact's emails or
    phone numbers). Profile linkage is fuzzy and account avatars change with
    their owners - syncing one once attached a completely different person's
    face to a contact. The user-set CONTACT photo must therefore always win;
    PROFILE is only a fallback (or skipped entirely with contact_only=True,
    used by repair flows that should trust nothing but user-set photos).
    """
    non_default = [p for p in photos if not p.get("default")]
    contact_sourced = [
        p for p in non_default
        if p.get("metadata", {}).get("source", {}).get("type") == "CONTACT"
    ]
    if contact_sourced:
        return contact_sourced[0]
    if contact_only:
        return None
    return non_default[0] if non_default else None


def _person_updated_at(person: dict) -> str:
    """The CONTACT source's updateTime, so merges have a real timestamp.

    Without this every Google change carried updated_at="" and lost all
    newest-edit-wins merges against Microsoft's real timestamps (and tied
    arbitrarily with iCloud's).
    """
    for source in person.get("metadata", {}).get("sources", []):
        if source.get("type") == "CONTACT":
            return source.get("updateTime", "")
    return ""


# Google People photo URLs end in a size directive (e.g. "=s100"), which
# serves a 100px thumbnail. Pulling that would permanently degrade every
# synced photo, since the thumbnail is what gets pushed to the other
# providers. Rewriting the directive to "=s0" requests the original size.
# URLs without a recognizable suffix are used unchanged.
_PHOTO_SIZE_SUFFIX = re.compile(r"=s\d+(-c)?$")


def _full_size_photo_url(url: str) -> str:
    return _PHOTO_SIZE_SUFFIX.sub("=s0", url)


def _to_canonical(person: dict, access_token: Optional[str] = None) -> CanonicalContact:
    names = person.get("names", [{}])[0] if person.get("names") else {}
    emails = [Email(value=e["value"]) for e in person.get("emailAddresses", [])]
    phones = [Phone(value=p["value"]) for p in person.get("phoneNumbers", [])]
    notes = person.get("biographies", [{}])[0].get("value") if person.get("biographies") else None
    photo_data = None
    photo_content_type = None
    picked_photo = _pick_photo(person.get("photos", []))
    if picked_photo and access_token:
        try:
            response = requests.get(
                _full_size_photo_url(picked_photo["url"]),
                headers={"Authorization": f"Bearer {access_token}"},
            )
            response.raise_for_status()
            photo_data = response.content
            photo_content_type = response.headers.get("Content-Type", "").split(";")[0].strip() or None
        except requests.exceptions.RequestException:
            pass
    return CanonicalContact(
        display_name=names.get("displayName", ""),
        given_name=names.get("givenName"),
        family_name=names.get("familyName"),
        emails=emails,
        phones=phones,
        notes=notes,
        photo_data=photo_data,
        photo_content_type=photo_content_type,
        extra={"google_etag": person.get("etag")},
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
