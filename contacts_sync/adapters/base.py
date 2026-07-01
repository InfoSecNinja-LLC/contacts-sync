from dataclasses import dataclass
from typing import Optional, Protocol

from contacts_sync.models import CanonicalContact


class SyncTokenExpiredError(Exception):
    pass


@dataclass
class ChangedContact:
    provider_id: str
    contact: Optional[CanonicalContact]
    updated_at: str
    deleted: bool = False


@dataclass
class ChangeSet:
    changes: list[ChangedContact]
    next_sync_token: Optional[str]


class ProviderAdapter(Protocol):
    name: str

    def list_changes(self, since_token: Optional[str]) -> ChangeSet: ...
    def create(self, contact: CanonicalContact) -> str: ...
    def update(self, provider_id: str, contact: CanonicalContact) -> None: ...
    def delete(self, provider_id: str) -> None: ...
