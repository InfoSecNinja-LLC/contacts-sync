import json
import sqlite3
from typing import Optional

from contacts_sync.models import Address, CanonicalContact, Email, Phone

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
            # provider_links.etag was added after the initial schema. Adding it
            # via ALTER TABLE keeps existing databases usable without a full
            # rebuild; guard it so re-running migrate() (or running against a
            # fresh db whose CREATE already had the column) is a no-op.
            existing_cols = {
                row["name"] for row in conn.execute("PRAGMA table_info(provider_links)").fetchall()
            }
            if "etag" not in existing_cols:
                conn.execute("ALTER TABLE provider_links ADD COLUMN etag TEXT")

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

    def get_contact(self, contact_id: int) -> Optional[CanonicalContact]:
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

    def set_link_etag(self, provider: str, provider_id: str, etag: Optional[str]) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE provider_links SET etag = ? WHERE provider = ? AND provider_id = ?",
                (etag, provider, provider_id),
            )

    def get_link_etag(self, provider: str, provider_id: str) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT etag FROM provider_links WHERE provider = ? AND provider_id = ?",
                (provider, provider_id),
            ).fetchone()
            return row["etag"] if row else None

    def unlink_provider(self, provider: str, provider_id: str) -> None:
        """Remove a single provider link (e.g. after a 404 proves it is stale).

        Only deletes the link row; the canonical contact and its other provider
        links are left intact.
        """
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM provider_links WHERE provider = ? AND provider_id = ?",
                (provider, provider_id),
            )

    def get_link(self, provider: str, provider_id: str) -> Optional[int]:
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

    def get_sync_token(self, provider: str) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT sync_token FROM sync_state WHERE provider = ?", (provider,)
            ).fetchone()
            return row["sync_token"] if row else None

    def set_sync_token(self, provider: str, token: Optional[str]) -> None:
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
