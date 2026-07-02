from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Email:
    value: str
    type: Optional[str] = None
    primary: bool = False


@dataclass
class Phone:
    value: str
    type: Optional[str] = None


@dataclass
class Address:
    street: Optional[str] = None
    city: Optional[str] = None
    region: Optional[str] = None
    postal_code: Optional[str] = None
    country: Optional[str] = None
    type: Optional[str] = None


@dataclass
class CanonicalContact:
    id: Optional[int] = None
    display_name: str = ""
    given_name: Optional[str] = None
    family_name: Optional[str] = None
    emails: list[Email] = field(default_factory=list)
    phones: list[Phone] = field(default_factory=list)
    addresses: list[Address] = field(default_factory=list)
    notes: Optional[str] = None
    organization: Optional[str] = None
    title: Optional[str] = None
    groups: list[str] = field(default_factory=list)
    photo_url: Optional[str] = None
    field_meta: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)
