from dataclasses import dataclass
from typing import Optional, Protocol

from contacts_sync.models import CanonicalContact


class SyncTokenExpiredError(Exception):
    pass


class ProviderItemRejectedError(Exception):
    """The provider rejected THIS item's data (e.g. iCloud returning 403 for
    a vCard whose embedded photo exceeds Apple's size ceiling). This is a
    per-contact data problem, not a provider-wide failure: the engine logs
    it and continues pushing the remaining contacts instead of aborting the
    provider."""


class ProviderResourceGoneError(Exception):
    """Raised by an adapter when a provider resource we hold a link to no longer
    exists (HTTP 404 on update). Signals the sync engine to drop the stale link
    rather than error the whole provider for the rest of the run."""
    pass


@dataclass
class ChangedContact:
    provider_id: str
    contact: Optional[CanonicalContact]
    updated_at: str
    deleted: bool = False
    etag: Optional[str] = None


@dataclass
class ChangeSet:
    changes: list[ChangedContact]
    next_sync_token: Optional[str]


class ProviderAdapter(Protocol):
    name: str

    def list_changes(self, since_token: Optional[str]) -> ChangeSet: ...
    def create(self, contact: CanonicalContact) -> tuple[str, Optional[str]]: ...
    def update(self, provider_id: str, contact: CanonicalContact) -> Optional[str]: ...
    def delete(self, provider_id: str) -> None: ...
