# contacts-sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Python CLI (`contacts-sync`) that keeps Google, iCloud, and Microsoft personal contacts in sync via each provider's native API/protocol, using a local SQLite hub for canonical merged state.

**Architecture:** Hub-and-spoke. A local SQLite DB (`contacts.db`) holds canonical contacts, provider ID links, and sync tokens. Three provider adapters (Google People API, Microsoft Graph, iCloud CardDAV) implement a shared `ProviderAdapter` interface. A provider-agnostic `SyncEngine` pulls changes, matches/merges them into the canonical store, then pushes merged state back out.

**Tech Stack:** Python 3.12+, Typer (CLI), stdlib `sqlite3`, `google-api-python-client` + `google-auth-oauthlib` (Google), `msal` (Microsoft device code flow + token cache), `requests` (Microsoft Graph REST calls, iCloud CardDAV), `vobject` (vCard parsing), `pytest` + `pytest-mock` + `requests-mock` (tests).

**Deviations from the design spec worth flagging:**
- Microsoft: uses `azure-identity`'s conceptual approach but implemented via `msal.PublicClientApplication` directly (not `msgraph-sdk`) — `msal` exposes device-flow + serializable token cache directly, which is what refresh-without-reauth needs; `msgraph-sdk`'s generated async client would add substantial complexity for no benefit here. Graph API calls themselves go through plain `requests` against REST endpoints, which is far easier to unit-test (mock an HTTP response) than a generated SDK's model objects.
- iCloud: uses raw `requests` CardDAV calls + `vobject` for vCard parsing, not the `caldav` package — `caldav` is calendar-oriented and doesn't have first-class CardDAV/addressbook support suited to this use case.

**Known gaps deferred out of this plan (flag to the user if this matters before merging):**
- **Photo sync** is not implemented. Google/Microsoft expose photos differently (URL vs. binary stream) than iCloud's embedded base64 `PHOTO` vCard property, and round-tripping all three is a separate chunk of work. `CanonicalContact.photo_url` exists as a field for future use but adapters do not read/write it yet.
- **Address field merging** is not implemented (addresses are read into the canonical model but not merged/pushed) — merging structured multi-field data (street/city/region/etc.) needs its own dedup strategy distinct from the simple string-union used for emails/phones. Deferred to keep this plan shippable.
- **`groups`/organization/title merging** uses the same single/multi-value merge primitives as name/notes but is only wired up minimally; expanding provider mapping fidelity (e.g. Google `memberships` groups, Microsoft `categories`, iCloud vCard groups) beyond pass-through is future work.

---

### Task 1: Project scaffolding

**Files:**
- Create: `pyproject.toml`
- Create: `contacts_sync/__init__.py`
- Create: `contacts_sync/cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli.py
from typer.testing import CliRunner
from contacts_sync.cli import app

runner = CliRunner()

def test_version_command():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "contacts-sync" in result.stdout
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cli.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'contacts_sync'`)

- [ ] **Step 3: Write pyproject.toml and minimal CLI**

```toml
# pyproject.toml
[project]
name = "contacts-sync"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "typer>=0.12",
    "google-api-python-client>=2.140",
    "google-auth-oauthlib>=1.2",
    "msal>=1.30",
    "requests>=2.32",
    "vobject>=0.9.7",
    "defusedxml>=0.7.1",
]

[project.optional-dependencies]
dev = ["pytest>=8.0", "pytest-mock>=3.14", "requests-mock>=1.12"]

[project.scripts]
contacts-sync = "contacts_sync.cli:app"

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.pytest.ini_options]
pythonpath = ["."]
```

```python
# contacts_sync/__init__.py
```

```python
# contacts_sync/cli.py
import typer

app = typer.Typer()


@app.command()
def version():
    typer.echo("contacts-sync 0.1.0")


if __name__ == "__main__":
    app()
```

- [ ] **Step 4: Install deps and run test to verify it passes**

Run: `pip install -e ".[dev]" && pytest tests/test_cli.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml contacts_sync/__init__.py contacts_sync/cli.py tests/test_cli.py
git commit -m "Scaffold contacts-sync CLI project"
```

---

### Task 2: Canonical data model

**Files:**
- Create: `contacts_sync/models.py`
- Test: `tests/test_models.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_models.py
from contacts_sync.models import CanonicalContact, Email, Phone, Address

def test_canonical_contact_defaults():
    contact = CanonicalContact(display_name="Jane Doe")
    assert contact.id is None
    assert contact.emails == []
    assert contact.phones == []
    assert contact.addresses == []
    assert contact.field_meta == {}
    assert contact.extra == {}

def test_canonical_contact_with_fields():
    contact = CanonicalContact(
        display_name="Jane Doe",
        emails=[Email(value="jane@example.com", primary=True)],
        phones=[Phone(value="+15551234567", type="mobile")],
        addresses=[Address(street="1 Main St", city="Springfield")],
    )
    assert contact.emails[0].value == "jane@example.com"
    assert contact.phones[0].type == "mobile"
    assert contact.addresses[0].city == "Springfield"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_models.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'contacts_sync.models'`)

- [ ] **Step 3: Write the implementation**

```python
# contacts_sync/models.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_models.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add contacts_sync/models.py tests/test_models.py
git commit -m "Add canonical contact data model"
```

---

### Task 3: SQLite schema and access layer

**Files:**
- Create: `contacts_sync/db.py`
- Test: `tests/test_db.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_db.py
import pytest
from contacts_sync.db import Database
from contacts_sync.models import CanonicalContact, Email, Phone

@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "contacts.db"))
    database.migrate()
    return database

def test_create_and_get_contact(db):
    contact = CanonicalContact(
        display_name="Jane Doe",
        emails=[Email(value="jane@example.com", primary=True)],
        phones=[Phone(value="+15551234567")],
    )
    contact_id = db.create_contact(contact)
    fetched = db.get_contact(contact_id)
    assert fetched.display_name == "Jane Doe"
    assert fetched.emails[0].value == "jane@example.com"
    assert fetched.phones[0].value == "+15551234567"

def test_update_contact(db):
    contact_id = db.create_contact(CanonicalContact(display_name="Jane Doe"))
    contact = db.get_contact(contact_id)
    contact.display_name = "Jane Smith"
    contact.emails = [Email(value="jane.smith@example.com")]
    db.update_contact(contact)
    fetched = db.get_contact(contact_id)
    assert fetched.display_name == "Jane Smith"
    assert fetched.emails[0].value == "jane.smith@example.com"

def test_delete_contact(db):
    contact_id = db.create_contact(CanonicalContact(display_name="Jane Doe"))
    db.delete_contact(contact_id)
    assert db.get_contact(contact_id) is None

def test_list_contacts(db):
    db.create_contact(CanonicalContact(display_name="A"))
    db.create_contact(CanonicalContact(display_name="B"))
    assert len(db.list_contacts()) == 2

def test_provider_link_roundtrip(db):
    contact_id = db.create_contact(CanonicalContact(display_name="Jane Doe"))
    db.link_provider(contact_id, "google", "people/123")
    assert db.get_link("google", "people/123") == contact_id
    assert db.get_links_for_contact(contact_id) == {"google": "people/123"}

def test_sync_token_roundtrip(db):
    assert db.get_sync_token("google") is None
    db.set_sync_token("google", "token-abc")
    assert db.get_sync_token("google") == "token-abc"

def test_pending_match_roundtrip(db):
    db.save_pending_match("google", "people/999", candidate_contact_ids=[1, 2], contact_data_json="{}")
    pending = db.list_pending_matches()
    assert len(pending) == 1
    assert pending[0]["provider"] == "google"
    db.delete_pending_match(pending[0]["id"])
    assert db.list_pending_matches() == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_db.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'contacts_sync.db'`)

- [ ] **Step 3: Write the implementation**

```python
# contacts_sync/db.py
import json
import sqlite3
from contacts_sync.models import CanonicalContact, Email, Phone, Address

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    display_name TEXT NOT NULL DEFAULT '',
    given_name TEXT,
    family_name TEXT,
    notes TEXT,
    organization TEXT,
    title TEXT,
    photo_url TEXT,
    groups_json TEXT NOT NULL DEFAULT '[]',
    field_meta_json TEXT NOT NULL DEFAULT '{}',
    extra_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS emails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    value TEXT NOT NULL,
    type TEXT,
    is_primary INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS phones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    value TEXT NOT NULL,
    type TEXT
);

CREATE TABLE IF NOT EXISTS addresses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    street TEXT,
    city TEXT,
    region TEXT,
    postal_code TEXT,
    country TEXT,
    type TEXT
);

CREATE TABLE IF NOT EXISTS provider_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    UNIQUE(provider, provider_id)
);

CREATE TABLE IF NOT EXISTS sync_state (
    provider TEXT PRIMARY KEY,
    sync_token TEXT
);

CREATE TABLE IF NOT EXISTS pending_matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    candidate_contact_ids_json TEXT NOT NULL,
    contact_data_json TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: str):
        self._path = path

    def _connect(self):
        conn = sqlite3.connect(self._path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        return conn

    def migrate(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def create_contact(self, contact: CanonicalContact) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO contacts (display_name, given_name, family_name, notes, "
                "organization, title, photo_url, groups_json, field_meta_json, extra_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    contact.display_name, contact.given_name, contact.family_name,
                    contact.notes, contact.organization, contact.title, contact.photo_url,
                    json.dumps(contact.groups), json.dumps(contact.field_meta),
                    json.dumps(contact.extra),
                ),
            )
            contact_id = cursor.lastrowid
            self._write_children(conn, contact_id, contact)
            return contact_id

    def get_contact(self, contact_id: int) -> CanonicalContact | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
            if row is None:
                return None
            return self._row_to_contact(conn, row)

    def update_contact(self, contact: CanonicalContact) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE contacts SET display_name = ?, given_name = ?, family_name = ?, "
                "notes = ?, organization = ?, title = ?, photo_url = ?, groups_json = ?, "
                "field_meta_json = ?, extra_json = ? WHERE id = ?",
                (
                    contact.display_name, contact.given_name, contact.family_name,
                    contact.notes, contact.organization, contact.title, contact.photo_url,
                    json.dumps(contact.groups), json.dumps(contact.field_meta),
                    json.dumps(contact.extra), contact.id,
                ),
            )
            conn.execute("DELETE FROM emails WHERE contact_id = ?", (contact.id,))
            conn.execute("DELETE FROM phones WHERE contact_id = ?", (contact.id,))
            conn.execute("DELETE FROM addresses WHERE contact_id = ?", (contact.id,))
            self._write_children(conn, contact.id, contact)

    def delete_contact(self, contact_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM contacts WHERE id = ?", (contact_id,))

    def list_contacts(self) -> list[CanonicalContact]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM contacts").fetchall()
            return [self._row_to_contact(conn, row) for row in rows]

    def link_provider(self, contact_id: int, provider: str, provider_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO provider_links (contact_id, provider, provider_id) "
                "VALUES (?, ?, ?)",
                (contact_id, provider, provider_id),
            )

    def get_link(self, provider: str, provider_id: str) -> int | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT contact_id FROM provider_links WHERE provider = ? AND provider_id = ?",
                (provider, provider_id),
            ).fetchone()
            return row["contact_id"] if row else None

    def get_links_for_contact(self, contact_id: int) -> dict[str, str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT provider, provider_id FROM provider_links WHERE contact_id = ?",
                (contact_id,),
            ).fetchall()
            return {row["provider"]: row["provider_id"] for row in rows}

    def get_sync_token(self, provider: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT sync_token FROM sync_state WHERE provider = ?", (provider,)
            ).fetchone()
            return row["sync_token"] if row else None

    def set_sync_token(self, provider: str, token: str | None) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sync_state (provider, sync_token) VALUES (?, ?) "
                "ON CONFLICT(provider) DO UPDATE SET sync_token = excluded.sync_token",
                (provider, token),
            )

    def save_pending_match(
        self, provider: str, provider_id: str, candidate_contact_ids: list[int], contact_data_json: str
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO pending_matches (provider, provider_id, candidate_contact_ids_json, "
                "contact_data_json) VALUES (?, ?, ?, ?)",
                (provider, provider_id, json.dumps(candidate_contact_ids), contact_data_json),
            )

    def list_pending_matches(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM pending_matches").fetchall()
            return [
                {
                    "id": row["id"],
                    "provider": row["provider"],
                    "provider_id": row["provider_id"],
                    "candidate_contact_ids": json.loads(row["candidate_contact_ids_json"]),
                    "contact_data": json.loads(row["contact_data_json"]),
                }
                for row in rows
            ]

    def delete_pending_match(self, pending_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM pending_matches WHERE id = ?", (pending_id,))

    def _write_children(self, conn, contact_id: int, contact: CanonicalContact) -> None:
        for email in contact.emails:
            conn.execute(
                "INSERT INTO emails (contact_id, value, type, is_primary) VALUES (?, ?, ?, ?)",
                (contact_id, email.value, email.type, int(email.primary)),
            )
        for phone in contact.phones:
            conn.execute(
                "INSERT INTO phones (contact_id, value, type) VALUES (?, ?, ?)",
                (contact_id, phone.value, phone.type),
            )
        for address in contact.addresses:
            conn.execute(
                "INSERT INTO addresses (contact_id, street, city, region, postal_code, country, type) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    contact_id, address.street, address.city, address.region,
                    address.postal_code, address.country, address.type,
                ),
            )

    def _row_to_contact(self, conn, row) -> CanonicalContact:
        emails = [
            Email(value=r["value"], type=r["type"], primary=bool(r["is_primary"]))
            for r in conn.execute("SELECT * FROM emails WHERE contact_id = ?", (row["id"],)).fetchall()
        ]
        phones = [
            Phone(value=r["value"], type=r["type"])
            for r in conn.execute("SELECT * FROM phones WHERE contact_id = ?", (row["id"],)).fetchall()
        ]
        addresses = [
            Address(
                street=r["street"], city=r["city"], region=r["region"],
                postal_code=r["postal_code"], country=r["country"], type=r["type"],
            )
            for r in conn.execute("SELECT * FROM addresses WHERE contact_id = ?", (row["id"],)).fetchall()
        ]
        return CanonicalContact(
            id=row["id"],
            display_name=row["display_name"],
            given_name=row["given_name"],
            family_name=row["family_name"],
            emails=emails,
            phones=phones,
            addresses=addresses,
            notes=row["notes"],
            organization=row["organization"],
            title=row["title"],
            photo_url=row["photo_url"],
            groups=json.loads(row["groups_json"]),
            field_meta=json.loads(row["field_meta_json"]),
            extra=json.loads(row["extra_json"]),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_db.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add contacts_sync/db.py tests/test_db.py
git commit -m "Add SQLite-backed canonical contact store"
```

---

### Task 4: Matcher logic

**Files:**
- Create: `contacts_sync/matcher.py`
- Test: `tests/test_matcher.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_matcher.py
from contacts_sync.models import CanonicalContact, Email, Phone
from contacts_sync.matcher import match_contact, normalize_email, normalize_phone

def test_normalize_email():
    assert normalize_email(" Jane@Example.com ") == "jane@example.com"

def test_normalize_phone():
    assert normalize_phone("(555) 123-4567") == "5551234567"
    assert normalize_phone("+1 555 123 4567") == "+15551234567"

def test_match_by_email():
    existing = [CanonicalContact(id=1, display_name="Jane", emails=[Email(value="jane@example.com")])]
    candidate = CanonicalContact(display_name="J. Doe", emails=[Email(value="Jane@Example.com")])
    result = match_contact(candidate, existing)
    assert result.status == "matched"
    assert result.contact_id == 1

def test_match_by_phone_when_no_email():
    existing = [CanonicalContact(id=1, display_name="Jane", phones=[Phone(value="+15551234567")])]
    candidate = CanonicalContact(display_name="J. Doe", phones=[Phone(value="(555) 123-4567")])
    result = match_contact(candidate, existing)
    assert result.status == "matched"
    assert result.contact_id == 1

def test_ambiguous_email_match():
    existing = [
        CanonicalContact(id=1, display_name="Jane A", emails=[Email(value="shared@example.com")]),
        CanonicalContact(id=2, display_name="Jane B", emails=[Email(value="shared@example.com")]),
    ]
    candidate = CanonicalContact(display_name="Jane", emails=[Email(value="shared@example.com")])
    result = match_contact(candidate, existing)
    assert result.status == "ambiguous"
    assert set(result.candidate_ids) == {1, 2}

def test_no_match_creates_new():
    existing = [CanonicalContact(id=1, display_name="Jane", emails=[Email(value="jane@example.com")])]
    candidate = CanonicalContact(display_name="Someone Else", emails=[Email(value="someone@example.com")])
    result = match_contact(candidate, existing)
    assert result.status == "no_match"

def test_name_only_match_requires_no_contact_info():
    existing = [CanonicalContact(id=1, display_name="Jane Doe")]
    candidate = CanonicalContact(display_name="Jane Doe")
    result = match_contact(candidate, existing)
    assert result.status == "matched"
    assert result.contact_id == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_matcher.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'contacts_sync.matcher'`)

- [ ] **Step 3: Write the implementation**

```python
# contacts_sync/matcher.py
from dataclasses import dataclass
from contacts_sync.models import CanonicalContact


def normalize_email(value: str) -> str:
    return value.strip().lower()


def normalize_phone(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit() or ch == "+")


@dataclass
class MatchResult:
    status: str  # "matched" | "ambiguous" | "no_match"
    contact_id: int | None = None
    candidate_ids: list[int] | None = None


def match_contact(candidate: CanonicalContact, existing: list[CanonicalContact]) -> MatchResult:
    candidate_emails = {normalize_email(e.value) for e in candidate.emails}
    if candidate_emails:
        matches = [c for c in existing if candidate_emails & {normalize_email(e.value) for e in c.emails}]
        if len(matches) == 1:
            return MatchResult("matched", contact_id=matches[0].id)
        if len(matches) > 1:
            return MatchResult("ambiguous", candidate_ids=[c.id for c in matches])

    candidate_phones = {normalize_phone(p.value) for p in candidate.phones}
    if candidate_phones:
        matches = [c for c in existing if candidate_phones & {normalize_phone(p.value) for p in c.phones}]
        if len(matches) == 1:
            return MatchResult("matched", contact_id=matches[0].id)
        if len(matches) > 1:
            return MatchResult("ambiguous", candidate_ids=[c.id for c in matches])

    if candidate.display_name and not candidate_emails and not candidate_phones:
        name = candidate.display_name.strip().lower()
        matches = [c for c in existing if c.display_name.strip().lower() == name]
        if len(matches) == 1:
            return MatchResult("matched", contact_id=matches[0].id)
        if len(matches) > 1:
            return MatchResult("ambiguous", candidate_ids=[c.id for c in matches])

    return MatchResult("no_match")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_matcher.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add contacts_sync/matcher.py tests/test_matcher.py
git commit -m "Add cross-provider contact matching logic"
```

---

### Task 5: Merger logic

**Files:**
- Create: `contacts_sync/merger.py`
- Test: `tests/test_merger.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_merger.py
from contacts_sync.merger import merge_single_value, merge_multi_value
from contacts_sync.matcher import normalize_email

def test_merge_single_value_no_change_keeps_current():
    value, meta = merge_single_value("Jane", "2026-01-01T00:00:00Z", None, "2026-02-01T00:00:00Z")
    assert value == "Jane"
    assert meta == "2026-01-01T00:00:00Z"

def test_merge_single_value_newer_incoming_wins():
    value, meta = merge_single_value("Jane", "2026-01-01T00:00:00Z", "Jane Doe", "2026-02-01T00:00:00Z")
    assert value == "Jane Doe"
    assert meta == "2026-02-01T00:00:00Z"

def test_merge_single_value_older_incoming_loses():
    value, meta = merge_single_value("Jane Doe", "2026-02-01T00:00:00Z", "Jane", "2026-01-01T00:00:00Z")
    assert value == "Jane Doe"
    assert meta == "2026-02-01T00:00:00Z"

def test_merge_single_value_no_prior_meta_accepts_incoming():
    value, meta = merge_single_value(None, None, "Jane", "2026-01-01T00:00:00Z")
    assert value == "Jane"
    assert meta == "2026-01-01T00:00:00Z"

def test_merge_multi_value_unions_and_dedupes():
    result = merge_multi_value(
        ["Jane@Example.com"], ["jane@example.com", "jane2@example.com"], normalize=normalize_email
    )
    assert result == ["Jane@Example.com", "jane2@example.com"]

def test_merge_multi_value_empty_incoming_keeps_current():
    result = merge_multi_value(["a@example.com"], [], normalize=normalize_email)
    assert result == ["a@example.com"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_merger.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'contacts_sync.merger'`)

- [ ] **Step 3: Write the implementation**

```python
# contacts_sync/merger.py
def merge_single_value(current_value, current_updated_at, incoming_value, incoming_updated_at):
    if not incoming_value or incoming_value == current_value:
        return current_value, current_updated_at
    if not current_updated_at or incoming_updated_at >= current_updated_at:
        return incoming_value, incoming_updated_at
    return current_value, current_updated_at


def merge_multi_value(current_values, incoming_values, normalize=lambda v: v):
    seen = {}
    for value in current_values:
        seen[normalize(value)] = value
    for value in incoming_values:
        key = normalize(value)
        if key not in seen:
            seen[key] = value
    return list(seen.values())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_merger.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add contacts_sync/merger.py tests/test_merger.py
git commit -m "Add field-level merge logic"
```

---

### Task 6: 1Password CLI wrapper

**Files:**
- Create: `contacts_sync/auth/__init__.py`
- Create: `contacts_sync/auth/onepassword.py`
- Test: `tests/test_onepassword.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_onepassword.py
import subprocess
import pytest
from contacts_sync.auth.onepassword import op_read, op_set_field, OnePasswordError

def test_op_read_returns_stdout(mocker):
    mocker.patch(
        "contacts_sync.auth.onepassword.subprocess.run",
        return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="secret-value\n", stderr=""),
    )
    assert op_read("op://vault/item/field") == "secret-value"

def test_op_read_missing_cli_raises(mocker):
    mocker.patch("contacts_sync.auth.onepassword.subprocess.run", side_effect=FileNotFoundError())
    with pytest.raises(OnePasswordError, match="not found on PATH"):
        op_read("op://vault/item/field")

def test_op_read_locked_session_raises(mocker):
    mocker.patch(
        "contacts_sync.auth.onepassword.subprocess.run",
        side_effect=subprocess.CalledProcessError(1, [], stderr="not signed in"),
    )
    with pytest.raises(OnePasswordError, match="unlocked"):
        op_read("op://vault/item/field")

def test_op_set_field_edits_existing_item(mocker):
    run_mock = mocker.patch(
        "contacts_sync.auth.onepassword.subprocess.run",
        return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
    )
    op_set_field("vault", "google", "refresh_token", "abc")
    assert run_mock.call_args_list[0].args[0][:3] == ["op", "item", "edit"]

def test_op_set_field_falls_back_to_create(mocker):
    edit_fail = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="no such item")
    create_ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    run_mock = mocker.patch(
        "contacts_sync.auth.onepassword.subprocess.run", side_effect=[edit_fail, create_ok]
    )
    op_set_field("vault", "google", "refresh_token", "abc")
    assert run_mock.call_args_list[1].args[0][:3] == ["op", "item", "create"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_onepassword.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'contacts_sync.auth'`)

- [ ] **Step 3: Write the implementation**

```python
# contacts_sync/auth/__init__.py
```

```python
# contacts_sync/auth/onepassword.py
import subprocess


class OnePasswordError(RuntimeError):
    pass


def op_read(reference: str) -> str:
    try:
        result = subprocess.run(["op", "read", reference], capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise OnePasswordError(
            "1Password CLI ('op') not found on PATH. Install it from "
            "https://developer.1password.com/docs/cli/"
        ) from exc
    if result.returncode != 0:
        raise OnePasswordError(
            f"1Password CLI failed to read '{reference}': {result.stderr.strip()}. "
            "Is the 1Password desktop app unlocked?"
        )
    return result.stdout.strip()


def op_set_field(vault: str, title: str, field_name: str, value: str) -> None:
    edit = subprocess.run(
        ["op", "item", "edit", title, f"--vault={vault}", f"{field_name}={value}"],
        capture_output=True, text=True,
    )
    if edit.returncode == 0:
        return
    create = subprocess.run(
        [
            "op", "item", "create", "--category=password",
            f"--vault={vault}", f"--title={title}", f"{field_name}={value}",
        ],
        capture_output=True, text=True,
    )
    if create.returncode != 0:
        raise OnePasswordError(f"Failed to save 1Password item '{title}': {create.stderr.strip()}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_onepassword.py -v`
Expected: PASS (fix `op_read` to catch `subprocess.CalledProcessError` too if using `check=True` — note: implementation above uses `capture_output` without `check=True`, so only `FileNotFoundError` and manual `returncode` check apply; adjust the `test_op_read_locked_session_raises` test double to return a `CompletedProcess` with `returncode=1` instead of raising `CalledProcessError`, since the implementation doesn't pass `check=True`)

- [ ] **Step 4b: Fix test to match implementation, then re-run**

```python
# tests/test_onepassword.py (replace test_op_read_locked_session_raises)
def test_op_read_locked_session_raises(mocker):
    mocker.patch(
        "contacts_sync.auth.onepassword.subprocess.run",
        return_value=subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="not signed in"),
    )
    with pytest.raises(OnePasswordError, match="unlocked"):
        op_read("op://vault/item/field")
```

Run: `pytest tests/test_onepassword.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add contacts_sync/auth/__init__.py contacts_sync/auth/onepassword.py tests/test_onepassword.py
git commit -m "Add 1Password CLI wrapper for credential storage"
```

---

### Task 7: Provider adapter interface

**Files:**
- Create: `contacts_sync/adapters/__init__.py`
- Create: `contacts_sync/adapters/base.py`
- Test: `tests/test_adapter_base.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_adapter_base.py
from contacts_sync.adapters.base import ChangedContact, ChangeSet, SyncTokenExpiredError
from contacts_sync.models import CanonicalContact

def test_changed_contact_and_change_set_construction():
    contact = CanonicalContact(display_name="Jane")
    change = ChangedContact(provider_id="123", contact=contact, updated_at="2026-01-01T00:00:00Z")
    change_set = ChangeSet(changes=[change], next_sync_token="token-1")
    assert change_set.changes[0].contact.display_name == "Jane"
    assert change_set.next_sync_token == "token-1"

def test_sync_token_expired_is_an_exception():
    assert issubclass(SyncTokenExpiredError, Exception)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_adapter_base.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'contacts_sync.adapters'`)

- [ ] **Step 3: Write the implementation**

```python
# contacts_sync/adapters/__init__.py
```

```python
# contacts_sync/adapters/base.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_adapter_base.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add contacts_sync/adapters/__init__.py contacts_sync/adapters/base.py tests/test_adapter_base.py
git commit -m "Add shared provider adapter interface"
```

---

### Task 8: Google auth

**Files:**
- Create: `contacts_sync/auth/google_auth.py`
- Test: `tests/test_google_auth.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_google_auth.py
import pytest
from contacts_sync.auth import google_auth
from contacts_sync.auth.onepassword import OnePasswordError

def test_run_installed_app_auth_saves_credentials(mocker):
    fake_creds = mocker.Mock(refresh_token="rt-1", client_id="cid-1", client_secret="secret-1")
    fake_flow = mocker.Mock()
    fake_flow.run_local_server.return_value = fake_creds
    mocker.patch(
        "contacts_sync.auth.google_auth.InstalledAppFlow.from_client_secrets_file",
        return_value=fake_flow,
    )
    save_mock = mocker.patch("contacts_sync.auth.google_auth.op_set_field")

    google_auth.run_installed_app_auth("client_secrets.json")

    save_mock.assert_any_call("contacts-sync", "google", "refresh_token", "rt-1")
    save_mock.assert_any_call("contacts-sync", "google", "client_id", "cid-1")
    save_mock.assert_any_call("contacts-sync", "google", "client_secret", "secret-1")

def test_get_credentials_raises_when_not_authed(mocker):
    mocker.patch("contacts_sync.auth.google_auth.op_read", side_effect=OnePasswordError("nope"))
    with pytest.raises(RuntimeError, match="auth google"):
        google_auth.get_credentials()

def test_get_credentials_builds_and_refreshes(mocker):
    mocker.patch(
        "contacts_sync.auth.google_auth.op_read",
        side_effect=["refresh-tok", "cid", "secret"],
    )
    fake_credentials = mocker.Mock()
    creds_cls = mocker.patch("contacts_sync.auth.google_auth.Credentials", return_value=fake_credentials)
    mocker.patch("contacts_sync.auth.google_auth.Request")

    result = google_auth.get_credentials()

    creds_cls.assert_called_once()
    fake_credentials.refresh.assert_called_once()
    assert result is fake_credentials
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_google_auth.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'contacts_sync.auth.google_auth'`)

- [ ] **Step 3: Write the implementation**

```python
# contacts_sync/auth/google_auth.py
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from contacts_sync.auth.onepassword import op_read, op_set_field, OnePasswordError

SCOPES = ["https://www.googleapis.com/auth/contacts"]
VAULT = "contacts-sync"


def run_installed_app_auth(client_secrets_path: str) -> None:
    flow = InstalledAppFlow.from_client_secrets_file(client_secrets_path, SCOPES)
    credentials = flow.run_local_server(port=0)
    op_set_field(VAULT, "google", "refresh_token", credentials.refresh_token)
    op_set_field(VAULT, "google", "client_id", credentials.client_id)
    op_set_field(VAULT, "google", "client_secret", credentials.client_secret)


def get_credentials() -> Credentials:
    try:
        refresh_token = op_read(f"op://{VAULT}/google/refresh_token")
        client_id = op_read(f"op://{VAULT}/google/client_id")
        client_secret = op_read(f"op://{VAULT}/google/client_secret")
    except OnePasswordError as exc:
        raise RuntimeError("No Google credentials found. Run `contacts-sync auth google` first.") from exc

    credentials = Credentials(
        token=None,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )
    credentials.refresh(Request())
    return credentials
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_google_auth.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add contacts_sync/auth/google_auth.py tests/test_google_auth.py
git commit -m "Add Google OAuth installed-app auth flow"
```

---

### Task 9: Google adapter

**Files:**
- Create: `contacts_sync/adapters/google.py`
- Test: `tests/test_google_adapter.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_google_adapter.py
import pytest
from contacts_sync.adapters.google import GoogleAdapter
from contacts_sync.adapters.base import SyncTokenExpiredError

FAKE_PERSON = {
    "resourceName": "people/123",
    "emailAddresses": [{"value": "jane@example.com"}],
    "phoneNumbers": [{"value": "+15551234567"}],
    "names": [{"displayName": "Jane Doe", "givenName": "Jane", "familyName": "Doe"}],
    "biographies": [{"value": "met at conf"}],
}


def _fake_service(mocker, response, side_effect=None):
    connections = mocker.Mock()
    if side_effect:
        connections.list.return_value.execute.side_effect = side_effect
    else:
        connections.list.return_value.execute.return_value = response
    people = mocker.Mock()
    people.connections.return_value = connections
    service = mocker.Mock()
    service.people.return_value = people
    return service


def test_list_changes_maps_person_to_canonical(mocker):
    service = _fake_service(
        mocker,
        {"connections": [FAKE_PERSON], "nextSyncToken": "sync-2"},
    )
    mocker.patch("contacts_sync.adapters.google.build", return_value=service)
    adapter = GoogleAdapter(credentials=mocker.Mock())

    change_set = adapter.list_changes(None)

    assert len(change_set.changes) == 1
    change = change_set.changes[0]
    assert change.provider_id == "people/123"
    assert change.contact.display_name == "Jane Doe"
    assert change.contact.emails[0].value == "jane@example.com"
    assert change_set.next_sync_token == "sync-2"


def test_list_changes_flags_deleted_contacts(mocker):
    deleted_person = {"resourceName": "people/456", "metadata": {"deleted": True}}
    service = _fake_service(mocker, {"connections": [deleted_person], "nextSyncToken": "sync-3"})
    mocker.patch("contacts_sync.adapters.google.build", return_value=service)
    adapter = GoogleAdapter(credentials=mocker.Mock())

    change_set = adapter.list_changes("sync-2")

    assert change_set.changes[0].deleted is True
    assert change_set.changes[0].provider_id == "people/456"


def test_list_changes_raises_on_expired_sync_token(mocker):
    class FakeHttpError(Exception):
        pass

    service = _fake_service(mocker, None, side_effect=FakeHttpError("EXPIRED_SYNC_TOKEN"))
    mocker.patch("contacts_sync.adapters.google.build", return_value=service)
    adapter = GoogleAdapter(credentials=mocker.Mock())

    with pytest.raises(SyncTokenExpiredError):
        adapter.list_changes("stale-token")


def test_create_sends_person_and_returns_resource_name(mocker):
    from contacts_sync.models import CanonicalContact, Email

    people = mocker.Mock()
    people.createContact.return_value.execute.return_value = {"resourceName": "people/789"}
    service = mocker.Mock()
    service.people.return_value = people
    mocker.patch("contacts_sync.adapters.google.build", return_value=service)
    adapter = GoogleAdapter(credentials=mocker.Mock())

    resource_name = adapter.create(CanonicalContact(display_name="New Person", emails=[Email(value="n@e.com")]))

    assert resource_name == "people/789"
    people.createContact.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_google_adapter.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'contacts_sync.adapters.google'`)

- [ ] **Step 3: Write the implementation**

```python
# contacts_sync/adapters/google.py
from googleapiclient.discovery import build
from contacts_sync.models import CanonicalContact, Email, Phone
from contacts_sync.adapters.base import ChangeSet, ChangedContact, SyncTokenExpiredError

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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_google_adapter.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add contacts_sync/adapters/google.py tests/test_google_adapter.py
git commit -m "Add Google People API adapter"
```

---

### Task 10: Microsoft auth

**Files:**
- Create: `contacts_sync/auth/microsoft_auth.py`
- Test: `tests/test_microsoft_auth.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_microsoft_auth.py
from contacts_sync.auth import microsoft_auth

def test_run_device_code_auth_saves_token_cache(mocker):
    fake_app = mocker.Mock()
    fake_app.initiate_device_flow.return_value = {"message": "go to https://microsoft.com/devicelogin"}
    mocker.patch("contacts_sync.auth.microsoft_auth.msal.PublicClientApplication", return_value=fake_app)
    mocker.patch("contacts_sync.auth.microsoft_auth._load_cache", return_value=mocker.Mock(has_state_changed=True, serialize=lambda: "cache-blob"))
    save_mock = mocker.patch("contacts_sync.auth.microsoft_auth.op_set_field")

    microsoft_auth.run_device_code_auth("client-id-1")

    fake_app.acquire_token_by_device_flow.assert_called_once()
    save_mock.assert_called_once_with("contacts-sync", "microsoft", "token_cache", "cache-blob")

def test_get_token_provider_uses_cached_account(mocker):
    fake_account = {"username": "me@outlook.com"}
    fake_app = mocker.Mock()
    fake_app.get_accounts.return_value = [fake_account]
    fake_app.acquire_token_silent.return_value = {"access_token": "tok-1"}
    mocker.patch("contacts_sync.auth.microsoft_auth.msal.PublicClientApplication", return_value=fake_app)
    mocker.patch("contacts_sync.auth.microsoft_auth._load_cache", return_value=mocker.Mock(has_state_changed=False))

    get_token = microsoft_auth.get_token_provider("client-id-1")
    token = get_token()

    assert token == "tok-1"
    fake_app.acquire_token_silent.assert_called_once_with(microsoft_auth.SCOPES, account=fake_account)

def test_get_token_provider_raises_when_no_cached_token(mocker):
    fake_app = mocker.Mock()
    fake_app.get_accounts.return_value = []
    fake_app.acquire_token_silent.return_value = None
    mocker.patch("contacts_sync.auth.microsoft_auth.msal.PublicClientApplication", return_value=fake_app)
    mocker.patch("contacts_sync.auth.microsoft_auth._load_cache", return_value=mocker.Mock(has_state_changed=False))

    get_token = microsoft_auth.get_token_provider("client-id-1")
    import pytest
    with pytest.raises(RuntimeError, match="auth microsoft"):
        get_token()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_microsoft_auth.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'contacts_sync.auth.microsoft_auth'`)

- [ ] **Step 3: Write the implementation**

```python
# contacts_sync/auth/microsoft_auth.py
import msal
from contacts_sync.auth.onepassword import op_read, op_set_field, OnePasswordError

AUTHORITY = "https://login.microsoftonline.com/consumers"
SCOPES = ["Contacts.ReadWrite"]
VAULT = "contacts-sync"


def _load_cache() -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    try:
        cache.deserialize(op_read(f"op://{VAULT}/microsoft/token_cache"))
    except OnePasswordError:
        pass
    return cache


def _save_cache(cache: msal.SerializableTokenCache) -> None:
    if cache.has_state_changed:
        op_set_field(VAULT, "microsoft", "token_cache", cache.serialize())


def run_device_code_auth(client_id: str) -> None:
    cache = _load_cache()
    app = msal.PublicClientApplication(client_id, authority=AUTHORITY, token_cache=cache)
    flow = app.initiate_device_flow(scopes=SCOPES)
    print(flow["message"])
    app.acquire_token_by_device_flow(flow)
    _save_cache(cache)


def get_token_provider(client_id: str):
    def get_token() -> str:
        cache = _load_cache()
        app = msal.PublicClientApplication(client_id, authority=AUTHORITY, token_cache=cache)
        accounts = app.get_accounts()
        result = app.acquire_token_silent(SCOPES, account=accounts[0] if accounts else None)
        if not result:
            raise RuntimeError("No cached Microsoft token. Run `contacts-sync auth microsoft` first.")
        _save_cache(cache)
        return result["access_token"]

    return get_token
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_microsoft_auth.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add contacts_sync/auth/microsoft_auth.py tests/test_microsoft_auth.py
git commit -m "Add Microsoft device code auth with persistent token cache"
```

---

### Task 11: Microsoft adapter

**Files:**
- Create: `contacts_sync/adapters/microsoft.py`
- Test: `tests/test_microsoft_adapter.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_microsoft_adapter.py
import pytest
from contacts_sync.adapters.microsoft import MicrosoftAdapter
from contacts_sync.adapters.base import SyncTokenExpiredError
from contacts_sync.models import CanonicalContact, Email, Phone

BASE = "https://graph.microsoft.com/v1.0"

def test_list_changes_maps_contact_and_captures_delta_link(requests_mock):
    requests_mock.get(
        f"{BASE}/me/contactFolders/contacts/contacts/delta",
        json={
            "value": [
                {
                    "id": "AAMk123",
                    "displayName": "Jane Doe",
                    "givenName": "Jane",
                    "surname": "Doe",
                    "emailAddresses": [{"address": "jane@example.com"}],
                    "businessPhones": ["5551234567"],
                    "lastModifiedDateTime": "2026-01-01T00:00:00Z",
                }
            ],
            "@odata.deltaLink": f"{BASE}/me/contactFolders/contacts/contacts/delta?$deltatoken=abc",
        },
    )
    adapter = MicrosoftAdapter(token_provider=lambda: "fake-token")

    change_set = adapter.list_changes(None)

    assert len(change_set.changes) == 1
    assert change_set.changes[0].contact.display_name == "Jane Doe"
    assert change_set.changes[0].contact.emails[0].value == "jane@example.com"
    assert change_set.next_sync_token.endswith("$deltatoken=abc")

def test_list_changes_flags_removed_contacts(requests_mock):
    requests_mock.get(
        f"{BASE}/me/contactFolders/contacts/contacts/delta",
        json={"value": [{"id": "AAMk999", "@removed": {"reason": "deleted"}}], "@odata.deltaLink": f"{BASE}/x?$deltatoken=y"},
    )
    adapter = MicrosoftAdapter(token_provider=lambda: "fake-token")

    change_set = adapter.list_changes(None)

    assert change_set.changes[0].deleted is True
    assert change_set.changes[0].provider_id == "AAMk999"

def test_list_changes_raises_on_410(requests_mock):
    requests_mock.get(
        f"{BASE}/me/contactFolders/contacts/contacts/delta",
        status_code=410,
        json={"error": {"code": "syncStateNotFound"}},
    )
    adapter = MicrosoftAdapter(token_provider=lambda: "fake-token")

    with pytest.raises(SyncTokenExpiredError):
        adapter.list_changes("stale-delta-link")

def test_create_posts_contact_and_returns_id(requests_mock):
    requests_mock.post(f"{BASE}/me/contacts", json={"id": "AAMk-new"})
    adapter = MicrosoftAdapter(token_provider=lambda: "fake-token")

    provider_id = adapter.create(CanonicalContact(display_name="New", emails=[Email(value="n@e.com")]))

    assert provider_id == "AAMk-new"

def test_delete_treats_404_as_success(requests_mock):
    requests_mock.delete(f"{BASE}/me/contacts/AAMk1", status_code=404)
    adapter = MicrosoftAdapter(token_provider=lambda: "fake-token")
    adapter.delete("AAMk1")  # should not raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_microsoft_adapter.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'contacts_sync.adapters.microsoft'`)

- [ ] **Step 3: Write the implementation**

```python
# contacts_sync/adapters/microsoft.py
import requests
from contacts_sync.models import CanonicalContact, Email, Phone
from contacts_sync.adapters.base import ChangeSet, ChangedContact, SyncTokenExpiredError

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
        if since_token:
            url = since_token
            params = None
        else:
            url = f"{GRAPH_BASE}/me/contactFolders/contacts/contacts/delta"
            params = {"$select": CONTACT_SELECT}

        changes = []
        next_token = since_token
        while url:
            response = requests.get(url, headers=self._headers(), params=params)
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
        response = requests.post(f"{GRAPH_BASE}/me/contacts", headers=self._headers(), json=_to_graph(contact))
        response.raise_for_status()
        return response.json()["id"]

    def update(self, provider_id: str, contact: CanonicalContact) -> None:
        response = requests.patch(
            f"{GRAPH_BASE}/me/contacts/{provider_id}", headers=self._headers(), json=_to_graph(contact)
        )
        response.raise_for_status()

    def delete(self, provider_id: str) -> None:
        response = requests.delete(f"{GRAPH_BASE}/me/contacts/{provider_id}", headers=self._headers())
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
    body = {
        "displayName": contact.display_name,
        "givenName": contact.given_name,
        "surname": contact.family_name,
        "emailAddresses": [{"address": e.value, "name": contact.display_name} for e in contact.emails],
        "companyName": contact.organization,
        "jobTitle": contact.title,
        "categories": contact.groups,
    }
    if contact.notes:
        body["personalNotes"] = contact.notes
    business_phones = [p.value for p in contact.phones if p.type != "mobile"]
    mobile = next((p.value for p in contact.phones if p.type == "mobile"), None)
    if business_phones:
        body["businessPhones"] = business_phones
    if mobile:
        body["mobilePhone"] = mobile
    return body
```

- [ ] **Step 4: Add `requests-mock` fixture support and run test to verify it passes**

Run: `pytest tests/test_microsoft_adapter.py -v`
Expected: PASS (the `requests_mock` fixture is auto-registered by the `requests-mock` pytest plugin declared in `pyproject.toml`'s dev dependencies)

- [ ] **Step 5: Commit**

```bash
git add contacts_sync/adapters/microsoft.py tests/test_microsoft_adapter.py
git commit -m "Add Microsoft Graph contacts adapter"
```

---

### Task 12: iCloud auth

**Files:**
- Create: `contacts_sync/auth/icloud_auth.py`
- Test: `tests/test_icloud_auth.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_icloud_auth.py
import pytest
from contacts_sync.auth import icloud_auth
from contacts_sync.auth.onepassword import OnePasswordError

def test_run_icloud_auth_saves_credentials(mocker):
    mocker.patch("builtins.input", return_value="me@icloud.com")
    mocker.patch("contacts_sync.auth.icloud_auth.getpass.getpass", return_value="app-specific-pass")
    save_mock = mocker.patch("contacts_sync.auth.icloud_auth.op_set_field")

    icloud_auth.run_icloud_auth()

    save_mock.assert_any_call("contacts-sync", "icloud", "apple_id", "me@icloud.com")
    save_mock.assert_any_call("contacts-sync", "icloud", "app_password", "app-specific-pass")

def test_get_credentials_returns_stored_values(mocker):
    mocker.patch("contacts_sync.auth.icloud_auth.op_read", side_effect=["me@icloud.com", "app-specific-pass"])
    apple_id, app_password = icloud_auth.get_credentials()
    assert apple_id == "me@icloud.com"
    assert app_password == "app-specific-pass"

def test_get_credentials_raises_when_not_authed(mocker):
    mocker.patch("contacts_sync.auth.icloud_auth.op_read", side_effect=OnePasswordError("nope"))
    with pytest.raises(RuntimeError, match="auth icloud"):
        icloud_auth.get_credentials()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_icloud_auth.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'contacts_sync.auth.icloud_auth'`)

- [ ] **Step 3: Write the implementation**

```python
# contacts_sync/auth/icloud_auth.py
import getpass
from contacts_sync.auth.onepassword import op_read, op_set_field, OnePasswordError

VAULT = "contacts-sync"


def run_icloud_auth() -> None:
    apple_id = input("Apple ID (email): ").strip()
    app_password = getpass.getpass("App-specific password (from appleid.apple.com): ").strip()
    op_set_field(VAULT, "icloud", "apple_id", apple_id)
    op_set_field(VAULT, "icloud", "app_password", app_password)


def get_credentials() -> tuple[str, str]:
    try:
        apple_id = op_read(f"op://{VAULT}/icloud/apple_id")
        app_password = op_read(f"op://{VAULT}/icloud/app_password")
    except OnePasswordError as exc:
        raise RuntimeError("No iCloud credentials found. Run `contacts-sync auth icloud` first.") from exc
    return apple_id, app_password
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_icloud_auth.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add contacts_sync/auth/icloud_auth.py tests/test_icloud_auth.py
git commit -m "Add iCloud app-specific-password auth"
```

---

### Task 13: iCloud adapter

**Files:**
- Create: `contacts_sync/adapters/icloud.py`
- Test: `tests/test_icloud_adapter.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_icloud_adapter.py
import pytest
from contacts_sync.adapters.icloud import ICloudAdapter
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_icloud_adapter.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'contacts_sync.adapters.icloud'`)

- [ ] **Step 3: Write the implementation**

```python
# contacts_sync/adapters/icloud.py
import defusedxml.ElementTree as ET
import requests
import vobject
from contacts_sync.models import CanonicalContact, Email, Phone
from contacts_sync.adapters.base import ChangeSet, ChangedContact, SyncTokenExpiredError

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
        response = requests.request(
            "REPORT", self._addressbook_url, data=body, auth=self._auth,
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
        response = requests.put(
            href, data=vcard.serialize(), auth=self._auth,
            headers={"Content-Type": "text/vcard; charset=utf-8"},
        )
        response.raise_for_status()
        return href

    def update(self, provider_id: str, contact: CanonicalContact) -> None:
        vcard = _to_vcard(contact)
        response = requests.put(
            provider_id, data=vcard.serialize(), auth=self._auth,
            headers={"Content-Type": "text/vcard; charset=utf-8"},
        )
        response.raise_for_status()

    def delete(self, provider_id: str) -> None:
        response = requests.delete(provider_id, auth=self._auth)
        if response.status_code not in (204, 404):
            response.raise_for_status()


def _parse_multistatus(xml_text: str):
    root = ET.fromstring(xml_text)
    results = []
    for response in root.findall("D:response", NS):
        href = response.findtext("D:href", default="", namespaces=NS)
        propstat = response.find("D:propstat", NS)
        status = propstat.findtext("D:status", default="200", namespaces=NS) if propstat is not None else "404"
        status_code = status.split(" ")[1] if " " in status else status
        etag = propstat.findtext("D:prop/D:getetag", default="", namespaces=NS) if propstat is not None else ""
        address_data = (
            propstat.findtext("D:prop/C:address-data", default="", namespaces=NS) if propstat is not None else ""
        )
        results.append((href, status_code, etag, address_data))
    return results


def _extract_sync_token(xml_text: str) -> str:
    root = ET.fromstring(xml_text)
    return root.findtext("D:sync-token", default="", namespaces=NS)


def _to_canonical(vcard) -> CanonicalContact:
    emails = [Email(value=e.value) for e in getattr(vcard, "email_list", [])]
    phones = [Phone(value=t.value) for t in getattr(vcard, "tel_list", [])]
    return CanonicalContact(
        display_name=vcard.fn.value if hasattr(vcard, "fn") else "",
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_icloud_adapter.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add contacts_sync/adapters/icloud.py tests/test_icloud_adapter.py
git commit -m "Add iCloud CardDAV contacts adapter"
```

---

### Task 14: Sync engine

**Files:**
- Create: `contacts_sync/sync_engine.py`
- Test: `tests/test_sync_engine.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_sync_engine.py
import pytest
from contacts_sync.db import Database
from contacts_sync.sync_engine import SyncEngine
from contacts_sync.adapters.base import ChangeSet, ChangedContact, SyncTokenExpiredError
from contacts_sync.models import CanonicalContact, Email

@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "contacts.db"))
    database.migrate()
    return database


class FakeAdapter:
    def __init__(self, name, changes=None, next_token="tok-1", raise_expired_once=False):
        self.name = name
        self._changes = changes or []
        self._next_token = next_token
        self._raise_expired_once = raise_expired_once
        self.created = []
        self.updated = []

    def list_changes(self, since_token):
        if self._raise_expired_once and since_token is not None:
            self._raise_expired_once = False
            raise SyncTokenExpiredError("expired")
        return ChangeSet(changes=self._changes, next_sync_token=self._next_token)

    def create(self, contact):
        provider_id = f"{self.name}-new-{len(self.created)}"
        self.created.append(contact)
        return provider_id

    def update(self, provider_id, contact):
        self.updated.append((provider_id, contact))

    def delete(self, provider_id):
        pass


def test_new_contact_from_one_provider_is_created_and_pushed_to_others(db):
    incoming = ChangedContact(
        provider_id="g-1", contact=CanonicalContact(display_name="Jane", emails=[Email(value="jane@e.com")]),
        updated_at="2026-01-01T00:00:00Z",
    )
    google = FakeAdapter("google", changes=[incoming])
    microsoft = FakeAdapter("microsoft")
    engine = SyncEngine(db, {"google": google, "microsoft": microsoft})

    result = engine.run()

    assert result.created == 1
    assert len(microsoft.created) == 1
    assert microsoft.created[0].display_name == "Jane"


def test_matched_contact_merges_emails_across_providers(db):
    existing_id = db.create_contact(CanonicalContact(display_name="Jane", emails=[Email(value="jane@e.com")]))
    db.link_provider(existing_id, "google", "g-1")

    incoming = ChangedContact(
        provider_id="ms-1",
        contact=CanonicalContact(display_name="Jane", emails=[Email(value="jane.doe@work.com")]),
        updated_at="2026-01-02T00:00:00Z",
    )
    google = FakeAdapter("google")
    microsoft = FakeAdapter("microsoft", changes=[incoming])
    engine = SyncEngine(db, {"google": google, "microsoft": microsoft})

    result = engine.run()

    updated_contact = db.get_contact(existing_id)
    emails = {e.value for e in updated_contact.emails}
    assert emails == {"jane@e.com", "jane.doe@work.com"}
    assert result.updated == 1


def test_deleted_contact_is_removed_locally(db):
    existing_id = db.create_contact(CanonicalContact(display_name="Jane"))
    db.link_provider(existing_id, "google", "g-1")
    deletion = ChangedContact(provider_id="g-1", contact=None, updated_at="", deleted=True)
    google = FakeAdapter("google", changes=[deletion])
    engine = SyncEngine(db, {"google": google})

    result = engine.run()

    assert db.get_contact(existing_id) is None
    assert result.deleted == 1


def test_ambiguous_match_is_recorded_for_review(db):
    db.create_contact(CanonicalContact(display_name="Jane Smith"))
    db.create_contact(CanonicalContact(display_name="Jane Smith"))
    incoming = ChangedContact(
        provider_id="g-1", contact=CanonicalContact(display_name="Jane Smith"), updated_at="2026-01-01T00:00:00Z",
    )
    google = FakeAdapter("google", changes=[incoming])
    engine = SyncEngine(db, {"google": google})

    result = engine.run()

    assert result.pending_review == 1


def test_expired_sync_token_triggers_full_resync(db):
    google = FakeAdapter("google", raise_expired_once=True)
    db.set_sync_token("google", "stale-token")
    engine = SyncEngine(db, {"google": google})

    result = engine.run()

    assert "google" not in result.provider_errors


def test_dry_run_does_not_write_anything(db):
    incoming = ChangedContact(
        provider_id="g-1", contact=CanonicalContact(display_name="Jane"), updated_at="2026-01-01T00:00:00Z",
    )
    google = FakeAdapter("google", changes=[incoming])
    engine = SyncEngine(db, {"google": google})

    result = engine.run(dry_run=True)

    assert result.created == 1
    assert db.list_contacts() == []


def test_provider_error_is_isolated_and_reported(db):
    class ExplodingAdapter(FakeAdapter):
        def list_changes(self, since_token):
            raise RuntimeError("boom")

    google = ExplodingAdapter("google")
    microsoft = FakeAdapter("microsoft")
    engine = SyncEngine(db, {"google": google, "microsoft": microsoft})

    result = engine.run()

    assert "google" in result.provider_errors
    assert "boom" in result.provider_errors["google"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_sync_engine.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'contacts_sync.sync_engine'`)

- [ ] **Step 3: Write the implementation**

```python
# contacts_sync/sync_engine.py
from dataclasses import dataclass
from contacts_sync.db import Database
from contacts_sync.matcher import match_contact, normalize_email, normalize_phone
from contacts_sync.merger import merge_single_value, merge_multi_value
from contacts_sync.models import Email, Phone
from contacts_sync.adapters.base import SyncTokenExpiredError


@dataclass
class SyncResult:
    provider_errors: dict
    created: int = 0
    updated: int = 0
    deleted: int = 0
    pending_review: int = 0


class SyncEngine:
    def __init__(self, db: Database, adapters: dict):
        self._db = db
        self._adapters = adapters

    def run(self, dry_run: bool = False) -> SyncResult:
        errors = {}
        created = updated = deleted = pending_review = 0

        for name, adapter in self._adapters.items():
            try:
                token = self._db.get_sync_token(name)
                try:
                    change_set = adapter.list_changes(token)
                except SyncTokenExpiredError:
                    change_set = adapter.list_changes(None)

                for change in change_set.changes:
                    contact_id = self._db.get_link(name, change.provider_id)

                    if change.deleted:
                        if contact_id and not dry_run:
                            self._db.delete_contact(contact_id)
                        if contact_id:
                            deleted += 1
                        continue

                    if contact_id is None:
                        existing = self._db.list_contacts()
                        match = match_contact(change.contact, existing)
                        if match.status == "matched":
                            contact_id = match.contact_id
                            if not dry_run:
                                self._db.link_provider(contact_id, name, change.provider_id)
                        elif match.status == "ambiguous":
                            pending_review += 1
                            if not dry_run:
                                self._db.save_pending_match(name, change.provider_id, match.candidate_ids, "{}")
                            continue
                        else:
                            created += 1
                            if not dry_run:
                                contact_id = self._db.create_contact(change.contact)
                                self._db.link_provider(contact_id, name, change.provider_id)
                            continue

                    existing_contact = self._db.get_contact(contact_id)
                    self._merge_into(existing_contact, change, name, dry_run)
                    updated += 1

                if not dry_run:
                    self._db.set_sync_token(name, change_set.next_sync_token)
            except Exception as exc:
                errors[name] = str(exc)

        if not dry_run:
            self._push_to_providers(errors)

        return SyncResult(errors, created, updated, deleted, pending_review)

    def _merge_into(self, existing_contact, change, provider_name, dry_run):
        incoming = change.contact
        meta = existing_contact.field_meta

        new_name, new_name_meta = merge_single_value(
            existing_contact.display_name, meta.get("display_name"), incoming.display_name, change.updated_at,
        )
        existing_contact.display_name = new_name
        meta["display_name"] = new_name_meta

        new_notes, new_notes_meta = merge_single_value(
            existing_contact.notes, meta.get("notes"), incoming.notes, change.updated_at,
        )
        existing_contact.notes = new_notes
        meta["notes"] = new_notes_meta

        existing_contact.emails = [
            Email(value=v)
            for v in merge_multi_value(
                [e.value for e in existing_contact.emails], [e.value for e in incoming.emails], normalize=normalize_email
            )
        ]
        existing_contact.phones = [
            Phone(value=v)
            for v in merge_multi_value(
                [p.value for p in existing_contact.phones], [p.value for p in incoming.phones], normalize=normalize_phone
            )
        ]

        existing_contact.field_meta = meta
        if not dry_run:
            self._db.update_contact(existing_contact)

    def _push_to_providers(self, errors):
        for contact in self._db.list_contacts():
            links = self._db.get_links_for_contact(contact.id)
            for name, adapter in self._adapters.items():
                if name in errors:
                    continue
                try:
                    if name not in links:
                        provider_id = adapter.create(contact)
                        self._db.link_provider(contact.id, name, provider_id)
                    else:
                        adapter.update(links[name], contact)
                except Exception as exc:
                    errors[name] = str(exc)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_sync_engine.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add contacts_sync/sync_engine.py tests/test_sync_engine.py
git commit -m "Add provider-agnostic sync engine"
```

---

### Task 15: CLI auth and sync commands

**Files:**
- Modify: `contacts_sync/cli.py`
- Test: `tests/test_cli_sync.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli_sync.py
from typer.testing import CliRunner
from contacts_sync.cli import app

runner = CliRunner()

def test_auth_google_invokes_flow(mocker):
    mock_auth = mocker.patch("contacts_sync.cli.google_auth.run_installed_app_auth")
    result = runner.invoke(app, ["auth", "google", "--client-secrets", "secrets.json"])
    assert result.exit_code == 0
    mock_auth.assert_called_once_with("secrets.json")

def test_auth_microsoft_invokes_flow(mocker):
    mock_auth = mocker.patch("contacts_sync.cli.microsoft_auth.run_device_code_auth")
    result = runner.invoke(app, ["auth", "microsoft", "--client-id", "cid-1"])
    assert result.exit_code == 0
    mock_auth.assert_called_once_with("cid-1")

def test_auth_icloud_invokes_flow(mocker):
    mock_auth = mocker.patch("contacts_sync.cli.icloud_auth.run_icloud_auth")
    result = runner.invoke(app, ["auth", "icloud"])
    assert result.exit_code == 0
    mock_auth.assert_called_once()

def test_sync_reports_summary_and_exits_zero_on_success(mocker, tmp_path):
    mocker.patch("contacts_sync.cli.DB_PATH", str(tmp_path / "contacts.db"))
    mocker.patch("contacts_sync.cli._build_adapters", return_value={})
    fake_result = mocker.Mock(created=1, updated=2, deleted=0, pending_review=0, provider_errors={})
    mocker.patch("contacts_sync.cli.SyncEngine.run", return_value=fake_result)

    result = runner.invoke(app, ["sync", "--microsoft-client-id", "cid-1"])

    assert result.exit_code == 0
    assert "Created: 1" in result.stdout

def test_sync_exits_nonzero_on_provider_error(mocker, tmp_path):
    mocker.patch("contacts_sync.cli.DB_PATH", str(tmp_path / "contacts.db"))
    mocker.patch("contacts_sync.cli._build_adapters", return_value={})
    fake_result = mocker.Mock(created=0, updated=0, deleted=0, pending_review=0, provider_errors={"google": "boom"})
    mocker.patch("contacts_sync.cli.SyncEngine.run", return_value=fake_result)

    result = runner.invoke(app, ["sync", "--microsoft-client-id", "cid-1"])

    assert result.exit_code == 1
    assert "boom" in result.stdout
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cli_sync.py -v`
Expected: FAIL (`AttributeError: module 'contacts_sync.cli' has no attribute 'google_auth'` or similar, since `auth`/`sync` commands don't exist yet)

- [ ] **Step 3: Write the implementation**

```python
# contacts_sync/cli.py
import typer
from contacts_sync.db import Database
from contacts_sync.sync_engine import SyncEngine
from contacts_sync.auth import google_auth, microsoft_auth, icloud_auth
from contacts_sync.adapters.google import GoogleAdapter
from contacts_sync.adapters.microsoft import MicrosoftAdapter
from contacts_sync.adapters.icloud import ICloudAdapter

app = typer.Typer()
auth_app = typer.Typer()
app.add_typer(auth_app, name="auth")

DB_PATH = "contacts.db"
ICLOUD_ADDRESSBOOK_PATH = "/carddavhome/addressbooks/card/"


@app.command()
def version():
    typer.echo("contacts-sync 0.1.0")


@auth_app.command("google")
def auth_google(client_secrets: str = typer.Option(..., help="Path to Google OAuth client_secrets.json")):
    google_auth.run_installed_app_auth(client_secrets)
    typer.echo("Google credentials saved to 1Password.")


@auth_app.command("microsoft")
def auth_microsoft(client_id: str = typer.Option(..., help="Entra app registration client ID")):
    microsoft_auth.run_device_code_auth(client_id)
    typer.echo("Microsoft credentials saved to 1Password.")


@auth_app.command("icloud")
def auth_icloud():
    icloud_auth.run_icloud_auth()
    typer.echo("iCloud credentials saved to 1Password.")


def _build_adapters(microsoft_client_id: str) -> dict:
    google_creds = google_auth.get_credentials()
    apple_id, app_password = icloud_auth.get_credentials()
    ms_token_provider = microsoft_auth.get_token_provider(microsoft_client_id)
    return {
        "google": GoogleAdapter(google_creds),
        "microsoft": MicrosoftAdapter(ms_token_provider),
        "icloud": ICloudAdapter(apple_id, app_password, ICLOUD_ADDRESSBOOK_PATH),
    }


@app.command()
def sync(
    dry_run: bool = typer.Option(False, "--dry-run"),
    microsoft_client_id: str = typer.Option(..., envvar="CONTACTS_SYNC_MS_CLIENT_ID"),
):
    db = Database(DB_PATH)
    db.migrate()
    adapters = _build_adapters(microsoft_client_id)
    engine = SyncEngine(db, adapters)
    result = engine.run(dry_run=dry_run)

    typer.echo(
        f"Created: {result.created}, Updated: {result.updated}, "
        f"Deleted: {result.deleted}, Pending review: {result.pending_review}"
    )
    if result.provider_errors:
        for provider, error in result.provider_errors.items():
            typer.echo(f"ERROR [{provider}]: {error}")
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_cli_sync.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add contacts_sync/cli.py tests/test_cli_sync.py
git commit -m "Wire up auth and sync CLI commands"
```

---

### Task 16: CLI review, status, and doctor commands

**Files:**
- Modify: `contacts_sync/cli.py`
- Test: `tests/test_cli_review_status_doctor.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli_review_status_doctor.py
from typer.testing import CliRunner
from contacts_sync.cli import app
from contacts_sync.db import Database

runner = CliRunner()

def test_status_reports_contact_count_and_tokens(mocker, tmp_path):
    db_path = str(tmp_path / "contacts.db")
    mocker.patch("contacts_sync.cli.DB_PATH", db_path)
    db = Database(db_path)
    db.migrate()
    db.set_sync_token("google", "tok-1")

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    assert "0 contacts" in result.stdout
    assert "google: sync token set" in result.stdout
    assert "microsoft: sync token not set" in result.stdout

def test_review_lists_pending_matches_and_lets_user_link(mocker, tmp_path):
    db_path = str(tmp_path / "contacts.db")
    mocker.patch("contacts_sync.cli.DB_PATH", db_path)
    db = Database(db_path)
    db.migrate()
    from contacts_sync.models import CanonicalContact
    id_a = db.create_contact(CanonicalContact(display_name="Jane A"))
    id_b = db.create_contact(CanonicalContact(display_name="Jane B"))
    db.save_pending_match("google", "g-1", [id_a, id_b], "{}")
    mocker.patch("contacts_sync.cli.typer.prompt", return_value=str(id_a))

    result = runner.invoke(app, ["review"])

    assert result.exit_code == 0
    assert db.get_link("google", "g-1") == id_a
    assert db.list_pending_matches() == []

def test_doctor_reports_each_provider_status(mocker):
    mocker.patch("contacts_sync.cli.google_auth.get_credentials", return_value=mocker.Mock())
    mocker.patch("contacts_sync.cli.icloud_auth.get_credentials", return_value=("me@icloud.com", "pw"))
    mocker.patch("contacts_sync.cli.microsoft_auth.get_token_provider", return_value=lambda: "tok")

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert "google: OK" in result.stdout
    assert "icloud: OK" in result.stdout
    assert "microsoft: OK" in result.stdout

def test_doctor_reports_failure_for_missing_credentials(mocker):
    mocker.patch("contacts_sync.cli.google_auth.get_credentials", side_effect=RuntimeError("auth google first"))
    mocker.patch("contacts_sync.cli.icloud_auth.get_credentials", return_value=("me@icloud.com", "pw"))
    mocker.patch("contacts_sync.cli.microsoft_auth.get_token_provider", return_value=lambda: "tok")

    result = runner.invoke(app, ["doctor"])

    assert "google: FAILED" in result.stdout
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cli_review_status_doctor.py -v`
Expected: FAIL (commands don't exist yet)

- [ ] **Step 3: Add the commands to cli.py**

```python
# Append to contacts_sync/cli.py

@app.command()
def status():
    db = Database(DB_PATH)
    db.migrate()
    contacts = db.list_contacts()
    typer.echo(f"{len(contacts)} contacts in local store.")
    for provider in ("google", "microsoft", "icloud"):
        token = db.get_sync_token(provider)
        typer.echo(f"{provider}: sync token {'set' if token else 'not set'}")


@app.command()
def review():
    db = Database(DB_PATH)
    db.migrate()
    pending = db.list_pending_matches()
    if not pending:
        typer.echo("No pending matches to review.")
        return
    for match in pending:
        typer.echo(f"\nProvider {match['provider']} contact {match['provider_id']} could be:")
        for candidate_id in match["candidate_contact_ids"]:
            candidate = db.get_contact(candidate_id)
            name = candidate.display_name if candidate else "(deleted)"
            typer.echo(f"  [{candidate_id}] {name}")
        choice = typer.prompt("Enter contact id to link, or 'skip'")
        if choice == "skip":
            continue
        db.link_provider(int(choice), match["provider"], match["provider_id"])
        db.delete_pending_match(match["id"])


@app.command()
def doctor(microsoft_client_id: str = typer.Option(None, envvar="CONTACTS_SYNC_MS_CLIENT_ID")):
    for provider, check in (
        ("google", lambda: google_auth.get_credentials()),
        ("icloud", lambda: icloud_auth.get_credentials()),
        ("microsoft", lambda: microsoft_auth.get_token_provider(microsoft_client_id)()),
    ):
        try:
            check()
            typer.echo(f"{provider}: OK")
        except Exception as exc:
            typer.echo(f"{provider}: FAILED ({exc})")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_cli_review_status_doctor.py -v`
Expected: PASS

- [ ] **Step 5: Run the full test suite**

Run: `pytest -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add contacts_sync/cli.py tests/test_cli_review_status_doctor.py
git commit -m "Add review, status, and doctor CLI commands"
```

---

### Task 17: HTTP retry/backoff for Microsoft and iCloud adapters

**Files:**
- Create: `contacts_sync/http_retry.py`
- Modify: `contacts_sync/adapters/microsoft.py`
- Modify: `contacts_sync/adapters/icloud.py`
- Modify: `contacts_sync/adapters/google.py`
- Test: `tests/test_http_retry.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_http_retry.py
from contacts_sync.http_retry import request_with_retry

def test_returns_immediately_on_success(requests_mock):
    requests_mock.get("https://example.com/x", status_code=200, text="ok")
    response = request_with_retry("GET", "https://example.com/x")
    assert response.status_code == 200

def test_retries_on_429_then_succeeds(requests_mock):
    requests_mock.get(
        "https://example.com/x",
        [{"status_code": 429, "headers": {"Retry-After": "0"}}, {"status_code": 200, "text": "ok"}],
    )
    sleeps = []
    response = request_with_retry("GET", "https://example.com/x", sleep=sleeps.append)
    assert response.status_code == 200
    assert sleeps == [0.0]

def test_gives_up_after_max_attempts(requests_mock):
    requests_mock.get("https://example.com/x", status_code=503)
    sleeps = []
    response = request_with_retry("GET", "https://example.com/x", max_attempts=3, sleep=sleeps.append)
    assert response.status_code == 503
    assert len(sleeps) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_http_retry.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'contacts_sync.http_retry'`)

- [ ] **Step 3: Write the implementation**

```python
# contacts_sync/http_retry.py
import time
import requests

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def request_with_retry(method: str, url: str, max_attempts: int = 5, sleep=time.sleep, **kwargs) -> requests.Response:
    attempt = 0
    while True:
        attempt += 1
        response = requests.request(method, url, **kwargs)
        if response.status_code not in RETRYABLE_STATUS_CODES or attempt >= max_attempts:
            return response
        retry_after = response.headers.get("Retry-After")
        delay = float(retry_after) if retry_after else min(2 ** attempt, 30)
        sleep(delay)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_http_retry.py -v`
Expected: PASS

- [ ] **Step 5: Route Microsoft and iCloud adapters through the retry wrapper**

In `contacts_sync/adapters/microsoft.py`, replace every direct `requests.get(...)`, `requests.post(...)`, `requests.patch(...)`, `requests.delete(...)` call with the equivalent `request_with_retry("GET", ...)`, `request_with_retry("POST", ...)`, etc., and add `from contacts_sync.http_retry import request_with_retry` to the imports (drop the now-unused `import requests` if nothing else in the file uses it directly).

In `contacts_sync/adapters/icloud.py`, replace `requests.request("REPORT", ...)`, `requests.put(...)`, `requests.delete(...)` the same way, adding the same import.

- [ ] **Step 6: Add retry to Google's execute() calls**

In `contacts_sync/adapters/google.py`, add `num_retries=5` to every `.execute()` call (`connections().list(...).execute(num_retries=5)`, `createContact(...).execute(num_retries=5)`, `updateContact(...).execute(num_retries=5)`, `deleteContact(...).execute(num_retries=5)`) — the `google-api-python-client` library has this retry behavior built in for exactly this purpose.

- [ ] **Step 7: Run the full test suite**

Run: `pytest -v`
Expected: All tests PASS (existing adapter tests for Microsoft/iCloud still pass since `request_with_retry` behaves identically to `requests.request` on a first-try success)

- [ ] **Step 8: Commit**

```bash
git add contacts_sync/http_retry.py contacts_sync/adapters/microsoft.py contacts_sync/adapters/icloud.py contacts_sync/adapters/google.py tests/test_http_retry.py
git commit -m "Add exponential backoff retry for 429/5xx responses"
```

---

### Task 18: Sync audit log

**Files:**
- Modify: `contacts_sync/sync_engine.py`
- Modify: `contacts_sync/cli.py`
- Test: `tests/test_sync_engine_logging.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_sync_engine_logging.py
import logging
import pytest
from contacts_sync.db import Database
from contacts_sync.sync_engine import SyncEngine
from contacts_sync.adapters.base import ChangeSet, ChangedContact
from contacts_sync.models import CanonicalContact, Email


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "contacts.db"))
    database.migrate()
    return database


class FakeAdapter:
    def __init__(self, name, changes=None, next_token="tok-1"):
        self.name = name
        self._changes = changes or []
        self._next_token = next_token

    def list_changes(self, since_token):
        return ChangeSet(changes=self._changes, next_sync_token=self._next_token)

    def create(self, contact):
        return f"{self.name}-new"

    def update(self, provider_id, contact):
        pass

    def delete(self, provider_id):
        pass


def test_create_logs_contact_and_provider(db, caplog):
    incoming = ChangedContact(
        provider_id="g-1",
        contact=CanonicalContact(display_name="Jane", emails=[Email(value="jane@e.com")]),
        updated_at="2026-01-01T00:00:00Z",
    )
    google = FakeAdapter("google", changes=[incoming])
    engine = SyncEngine(db, {"google": google})

    with caplog.at_level(logging.INFO, logger="contacts_sync.sync"):
        engine.run()

    assert any("CREATE" in r.message and "Jane" in r.message for r in caplog.records)


def test_delete_logs_contact_id_and_provider(db, caplog):
    contact_id = db.create_contact(CanonicalContact(display_name="Jane"))
    db.link_provider(contact_id, "google", "g-1")
    deletion = ChangedContact(provider_id="g-1", contact=None, updated_at="", deleted=True)
    google = FakeAdapter("google", changes=[deletion])
    engine = SyncEngine(db, {"google": google})

    with caplog.at_level(logging.INFO, logger="contacts_sync.sync"):
        engine.run()

    assert any("DELETE" in r.message and str(contact_id) in r.message for r in caplog.records)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_sync_engine_logging.py -v`
Expected: FAIL (`AssertionError` — no matching log records, since nothing is logged yet)

- [ ] **Step 3: Add logging calls to sync_engine.py**

Add `import logging` near the top of `contacts_sync/sync_engine.py` and `logger = logging.getLogger("contacts_sync.sync")` after the imports. Then add these log calls at the corresponding points inside `SyncEngine.run`:

```python
# In the `deleted` branch, right after `if contact_id: deleted += 1`:
logger.info(f"DELETE contact_id={contact_id} provider={name} provider_id={change.provider_id}")

# In the "no_match" branch, right after `created += 1`:
logger.info(f'CREATE contact="{change.contact.display_name}" provider={name} provider_id={change.provider_id}')

# At the end of `_merge_into`, right before `if not dry_run:`:
logger.info(
    f"UPDATE contact_id={existing_contact.id} provider={provider_name} "
    f"fields=display_name,notes,emails,phones"
)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_sync_engine_logging.py -v`
Expected: PASS

- [ ] **Step 5: Wire a file handler in the CLI**

Add to `contacts_sync/cli.py`, near the top after the imports:

```python
import logging


def _configure_logging():
    logger = logging.getLogger("contacts_sync.sync")
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler("sync.log")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(handler)
```

Call `_configure_logging()` as the first line inside the `sync()` command function, before `db = Database(DB_PATH)`.

- [ ] **Step 6: Run the full test suite**

Run: `pytest -v`
Expected: All tests PASS

- [ ] **Step 7: Commit**

```bash
git add contacts_sync/sync_engine.py contacts_sync/cli.py tests/test_sync_engine_logging.py
git commit -m "Add sync audit logging to sync.log"
```

---

### Task 19: README and GitHub-publish polish

**Files:**
- Create: `README.md`

- [ ] **Step 1: Write the README**

```markdown
# contacts-sync

Keep Google, iCloud, and Microsoft/Outlook personal contacts in sync using each provider's native API/protocol — no unofficial scraping.

## How it works

A local SQLite database (`contacts.db`) holds a canonical merged copy of every contact plus links to its ID on each service. Each sync run pulls incremental changes from all three providers, merges them (multi-value fields like emails/phones are unioned; single-value fields use newest-edit-wins), and pushes the merged result back out. See [`docs/superpowers/specs/2026-07-01-contacts-sync-design.md`](docs/superpowers/specs/2026-07-01-contacts-sync-design.md) for the full design.

## Setup

1. `pip install -e ".[dev]"`
2. Install the [1Password CLI](https://developer.1password.com/docs/cli/) and create a vault named `contacts-sync`.
3. **Google**: create OAuth credentials (Desktop app type) in Google Cloud Console for the People API, set the consent screen's publishing status to "In production" (you'll see an "unverified app" warning when you authorize — that's expected for a personal-use app), then run:
   `contacts-sync auth google --client-secrets path/to/client_secrets.json`
4. **Microsoft**: register a public-client app in Entra ID (Azure Portal), enable "Allow public client flows," support "personal Microsoft accounts," then run:
   `contacts-sync auth microsoft --client-id <your-client-id>`
5. **iCloud**: generate an app-specific password at [appleid.apple.com](https://appleid.apple.com), then run:
   `contacts-sync auth icloud`

## Usage

```
contacts-sync doctor                                   # check all three providers are reachable
contacts-sync sync --dry-run --microsoft-client-id X    # preview changes without writing
contacts-sync sync --microsoft-client-id X               # run a real sync
contacts-sync review                                      # resolve ambiguous first-time matches
contacts-sync status                                       # see contact counts and sync token state
```

Set `CONTACTS_SYNC_MS_CLIENT_ID` in your environment to avoid passing `--microsoft-client-id` every time.

## Known limitations

- Photo sync is not implemented.
- Address fields are read but not merged/pushed yet.
- No scheduler is built in — run `contacts-sync sync` manually or via your OS's task scheduler/cron.

## Development

```
pytest -v
```
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "Add README"
```
