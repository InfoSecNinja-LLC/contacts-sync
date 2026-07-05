# Photo Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Sync contact photos bi-directionally across Google, Microsoft, and iCloud, using the same newest-edit-wins merge model already used for `display_name`/`notes`/etc.

**Architecture:** Replace the unused `photo_url` stub with `photo_data: bytes` + `photo_content_type: str` on `CanonicalContact`. Each adapter's `list_changes` fetches photo bytes only for contacts that already came back as delta-changed (iCloud: free, embedded in the vCard already fetched; Google/Microsoft: one extra authenticated request per changed contact). Each adapter's `create`/`update` pushes the photo after the normal fields succeed (iCloud: folded into the same vCard PUT; Google/Microsoft: one extra call). `sync_engine._merge_into` treats photo as one more `merge_single_value` field.

**Tech Stack:** Python 3.12, sqlite3, `vobject` (iCloud vCard), `googleapiclient`/`requests` (Google), `requests` (Microsoft Graph), `pytest`/`pytest-mock`/`requests-mock`.

Full design rationale: [`docs/superpowers/specs/2026-07-04-photo-sync-design.md`](../specs/2026-07-04-photo-sync-design.md)

---

### Task 1: Data model — `photo_data`/`photo_content_type`

**Files:**
- Modify: `contacts_sync/models.py:28-43`
- Test: `tests/test_models.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_models.py`:

```python
def test_canonical_contact_photo_defaults_to_none():
    contact = CanonicalContact(display_name="Jane Doe")
    assert contact.photo_data is None
    assert contact.photo_content_type is None


def test_canonical_contact_with_photo():
    contact = CanonicalContact(display_name="Jane Doe", photo_data=b"fakebytes", photo_content_type="image/jpeg")
    assert contact.photo_data == b"fakebytes"
    assert contact.photo_content_type == "image/jpeg"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_models.py -v -k photo`
Expected: FAIL with `AttributeError: 'CanonicalContact' object has no attribute 'photo_data'` (the field doesn't exist yet — `photo_url` does, but nothing reads it in these new tests).

- [ ] **Step 3: Replace `photo_url` with `photo_data`/`photo_content_type`**

In `contacts_sync/models.py`, replace this line (currently line 41):

```python
    photo_url: Optional[str] = None
```

with:

```python
    photo_data: Optional[bytes] = None
    photo_content_type: Optional[str] = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_models.py -v`
Expected: PASS (all tests in the file, not just the new ones)

- [ ] **Step 5: Commit**

```bash
git add contacts_sync/models.py tests/test_models.py
git commit -m "Replace CanonicalContact.photo_url stub with photo_data/photo_content_type"
```

---

### Task 2: DB storage for photo bytes

**Files:**
- Modify: `contacts_sync/db.py:81-134`, `contacts_sync/db.py:260-291`
- Test: `tests/test_db.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_db.py`:

```python
def test_create_and_get_contact_with_photo(db):
    contact = CanonicalContact(
        display_name="Jane Doe", photo_data=b"fake-jpeg-bytes", photo_content_type="image/jpeg"
    )
    contact_id = db.create_contact(contact)
    fetched = db.get_contact(contact_id)
    assert fetched.photo_data == b"fake-jpeg-bytes"
    assert fetched.photo_content_type == "image/jpeg"


def test_update_contact_photo(db):
    contact_id = db.create_contact(CanonicalContact(display_name="Jane Doe"))
    contact = db.get_contact(contact_id)
    contact.photo_data = b"updated-bytes"
    contact.photo_content_type = "image/png"
    db.update_contact(contact)
    fetched = db.get_contact(contact_id)
    assert fetched.photo_data == b"updated-bytes"
    assert fetched.photo_content_type == "image/png"


def test_migrate_is_idempotent_with_photo_columns(tmp_path):
    database = Database(str(tmp_path / "photo-idempotent.db"))
    database.migrate()
    database.migrate()  # must not error even though photo columns already exist

    contact_id = database.create_contact(CanonicalContact(display_name="Jane", photo_data=b"x"))
    assert database.get_contact(contact_id).photo_data == b"x"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_db.py -v -k photo`
Expected: FAIL with `sqlite3.OperationalError: table contacts has no column named photo_data` (from `create_contact`, which doesn't reference `photo_data` yet).

- [ ] **Step 3: Add the guarded column migration**

In `contacts_sync/db.py`, in `migrate()` (currently lines 81-92), add a second guarded block right after the existing `provider_links.etag` one:

```python
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
            # contacts.photo_data/photo_content_type were added after the initial
            # schema (replacing the never-implemented photo_url stub column,
            # which is left in place rather than dropped to avoid a destructive
            # migration on existing databases). Same guarded-ALTER pattern as
            # provider_links.etag above.
            existing_contact_cols = {
                row["name"] for row in conn.execute("PRAGMA table_info(contacts)").fetchall()
            }
            if "photo_data" not in existing_contact_cols:
                conn.execute("ALTER TABLE contacts ADD COLUMN photo_data BLOB")
            if "photo_content_type" not in existing_contact_cols:
                conn.execute("ALTER TABLE contacts ADD COLUMN photo_content_type TEXT")
```

- [ ] **Step 4: Update `create_contact` to write photo columns**

In `contacts_sync/db.py`, replace `create_contact` (currently lines 94-109):

```python
    def create_contact(self, contact: CanonicalContact) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO contacts (display_name, given_name, family_name, notes, "
                "organization, title, photo_data, photo_content_type, groups_json, "
                "field_meta_json, extra_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    contact.display_name, contact.given_name, contact.family_name,
                    contact.notes, contact.organization, contact.title,
                    contact.photo_data, contact.photo_content_type,
                    json.dumps(contact.groups), json.dumps(contact.field_meta),
                    json.dumps(contact.extra),
                ),
            )
            contact_id = cursor.lastrowid
            self._write_children(conn, contact_id, contact)
            return contact_id
```

- [ ] **Step 5: Update `update_contact` to write photo columns**

Replace `update_contact` (currently lines 118-134):

```python
    def update_contact(self, contact: CanonicalContact) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE contacts SET display_name = ?, given_name = ?, family_name = ?, "
                "notes = ?, organization = ?, title = ?, photo_data = ?, photo_content_type = ?, "
                "groups_json = ?, field_meta_json = ?, extra_json = ? WHERE id = ?",
                (
                    contact.display_name, contact.given_name, contact.family_name,
                    contact.notes, contact.organization, contact.title,
                    contact.photo_data, contact.photo_content_type,
                    json.dumps(contact.groups), json.dumps(contact.field_meta),
                    json.dumps(contact.extra), contact.id,
                ),
            )
            conn.execute("DELETE FROM emails WHERE contact_id = ?", (contact.id,))
            conn.execute("DELETE FROM phones WHERE contact_id = ?", (contact.id,))
            conn.execute("DELETE FROM addresses WHERE contact_id = ?", (contact.id,))
            self._write_children(conn, contact.id, contact)
```

- [ ] **Step 6: Update `_row_to_contact` to read photo columns**

In `_row_to_contact` (currently lines 260-291), replace:

```python
            photo_url=row["photo_url"],
```

with:

```python
            photo_data=row["photo_data"],
            photo_content_type=row["photo_content_type"],
```

- [ ] **Step 7: Run test to verify it passes**

Run: `pytest tests/test_db.py -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add contacts_sync/db.py tests/test_db.py
git commit -m "Store contact photo bytes/content-type in SQLite"
```

---

### Task 3: Merge photo through `sync_engine._merge_into`

**Files:**
- Modify: `contacts_sync/sync_engine.py:132-222`
- Test: `tests/test_sync_engine.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_sync_engine.py`:

```python
def test_incoming_photo_is_merged_onto_existing_contact(db):
    existing_id = db.create_contact(CanonicalContact(display_name="Jane", emails=[Email(value="jane@e.com")]))
    db.link_provider(existing_id, "google", "g-1")

    incoming = ChangedContact(
        provider_id="g-1",
        contact=CanonicalContact(
            display_name="Jane",
            emails=[Email(value="jane@e.com")],
            photo_data=b"new-photo-bytes",
            photo_content_type="image/jpeg",
        ),
        updated_at="2026-01-02T00:00:00Z",
    )
    google = FakeAdapter("google", changes=[incoming])
    engine = SyncEngine(db, {"google": google})

    result = engine.run()

    updated_contact = db.get_contact(existing_id)
    assert updated_contact.photo_data == b"new-photo-bytes"
    assert updated_contact.photo_content_type == "image/jpeg"
    assert result.updated == 1


def test_photo_only_change_marks_contact_dirty_for_repush(db):
    existing_id = db.create_contact(CanonicalContact(display_name="Jane", emails=[Email(value="jane@e.com")]))
    db.link_provider(existing_id, "google", "g-1")
    db.link_provider(existing_id, "microsoft", "ms-1")

    incoming = ChangedContact(
        provider_id="g-1",
        contact=CanonicalContact(
            display_name="Jane",
            emails=[Email(value="jane@e.com")],
            photo_data=b"new-photo-bytes",
            photo_content_type="image/jpeg",
        ),
        updated_at="2026-01-02T00:00:00Z",
    )
    google = FakeAdapter("google", changes=[incoming])
    microsoft = FakeAdapter("microsoft")
    engine = SyncEngine(db, {"google": google, "microsoft": microsoft})

    engine.run()

    assert [pid for pid, _ in microsoft.updated] == ["ms-1"]
    assert microsoft.updated[0][1].photo_data == b"new-photo-bytes"


def test_no_incoming_photo_keeps_existing_photo(db):
    existing_id = db.create_contact(
        CanonicalContact(display_name="Jane", photo_data=b"existing-bytes", photo_content_type="image/jpeg")
    )
    db.link_provider(existing_id, "google", "g-1")

    incoming = ChangedContact(
        provider_id="g-1",
        contact=CanonicalContact(display_name="Jane Updated"),
        updated_at="2026-01-02T00:00:00Z",
    )
    google = FakeAdapter("google", changes=[incoming])
    engine = SyncEngine(db, {"google": google})

    engine.run()

    updated_contact = db.get_contact(existing_id)
    assert updated_contact.photo_data == b"existing-bytes"
    assert updated_contact.photo_content_type == "image/jpeg"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_sync_engine.py -v -k photo`
Expected: FAIL — `test_incoming_photo_is_merged_onto_existing_contact` and `test_photo_only_change_marks_contact_dirty_for_repush` fail because `photo_data` is never merged (stays `None`); `test_no_incoming_photo_keeps_existing_photo` currently passes trivially (nothing touches photo yet) but is included now so it fails loudly later if the merge logic is ever wrong.

- [ ] **Step 3: Merge photo through `_merge_into`**

In `contacts_sync/sync_engine.py`, `_merge_into` (currently lines 132-222):

Update `_snapshot` (currently lines 144-152) to include `photo_data`:

```python
        def _snapshot(c):
            return (
                c.display_name,
                c.notes,
                c.given_name,
                c.family_name,
                sorted(e.value for e in c.emails),
                sorted(p.value for p in c.phones),
                c.photo_data,
            )
```

Add the photo merge, right after the `family_name` merge block (currently ending at line 178, right before the `existing_contact.emails = [...]` block):

```python
        new_photo_data, new_photo_meta = merge_single_value(
            existing_contact.photo_data, meta.get("photo"), incoming.photo_data, change.updated_at,
        )
        if new_photo_data != existing_contact.photo_data:
            # photo_content_type always travels with whichever photo_data value
            # won the merge - it's metadata about that value, not an
            # independently-mergeable field.
            existing_contact.photo_content_type = incoming.photo_content_type
        existing_contact.photo_data = new_photo_data
        meta["photo"] = new_photo_meta
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_sync_engine.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 5: Commit**

```bash
git add contacts_sync/sync_engine.py tests/test_sync_engine.py
git commit -m "Merge contact photo through newest-edit-wins like other single-value fields"
```

---

### Task 4: iCloud adapter — photo pull + push

**Files:**
- Modify: `contacts_sync/adapters/icloud.py:347-383`
- Test: `tests/test_icloud_adapter.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_icloud_adapter.py`:

```python
def test_to_canonical_decodes_embedded_photo():
    vcard = vobject.readOne(
        "BEGIN:VCARD\nVERSION:3.0\nFN:Jane Doe\n"
        "PHOTO;ENCODING=b;TYPE=JPEG:ZmFrZS1qcGVnLWJ5dGVz\nEND:VCARD\n"
    )

    canonical = _to_canonical(vcard)

    assert canonical.photo_data == b"fake-jpeg-bytes"
    assert canonical.photo_content_type == "image/jpeg"


def test_to_canonical_handles_vcard_without_photo():
    vcard = vobject.readOne("BEGIN:VCARD\nVERSION:3.0\nFN:Jane Doe\nEND:VCARD\n")

    canonical = _to_canonical(vcard)

    assert canonical.photo_data is None
    assert canonical.photo_content_type is None


def test_to_vcard_embeds_photo_when_present():
    contact = CanonicalContact(id=1, display_name="Jane Doe", photo_data=b"fake-png-bytes", photo_content_type="image/png")

    vcard = _to_vcard(contact)

    assert hasattr(vcard, "photo")
    assert vcard.photo.type_param == "PNG"
    # Round-trip through serialize/parse to confirm the base64 encoding is correct.
    parsed = vobject.readOne(vcard.serialize())
    assert parsed.photo.value == b"fake-png-bytes"


def test_to_vcard_omits_photo_when_absent():
    contact = CanonicalContact(id=1, display_name="Jane Doe")

    vcard = _to_vcard(contact)

    assert not hasattr(vcard, "photo")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_icloud_adapter.py -v -k photo`
Expected: FAIL with `AttributeError: 'CanonicalContact' object has no attribute 'photo_data'` style errors, or assertions failing because nothing sets/reads `photo`/`photo_data` yet.

- [ ] **Step 3: Implement photo pull/push in `icloud.py`**

Add two helper functions right before `_to_canonical` (currently at line 347):

```python
def _content_type_to_vcard_type(content_type) -> str:
    if not content_type:
        return "JPEG"
    return content_type.split("/")[-1].upper()


def _vcard_type_to_content_type(type_param) -> str:
    if not type_param:
        return "image/jpeg"
    return f"image/{type_param.lower()}"
```

Update `_to_canonical` (currently lines 347-362) to decode an embedded `PHOTO` property:

```python
def _to_canonical(vcard) -> CanonicalContact:
    emails = [Email(value=e.value) for e in getattr(vcard, "email_list", [])]
    phones = [Phone(value=t.value) for t in getattr(vcard, "tel_list", [])]
    given_name = None
    family_name = None
    if hasattr(vcard, "n"):
        given_name = vcard.n.value.given or None
        family_name = vcard.n.value.family or None
    photo_data = None
    photo_content_type = None
    if hasattr(vcard, "photo"):
        photo_data = vcard.photo.value
        photo_content_type = _vcard_type_to_content_type(getattr(vcard.photo, "type_param", None))
    return CanonicalContact(
        display_name=vcard.fn.value if hasattr(vcard, "fn") else "",
        given_name=given_name,
        family_name=family_name,
        emails=emails,
        phones=phones,
        notes=vcard.note.value if hasattr(vcard, "note") else None,
        photo_data=photo_data,
        photo_content_type=photo_content_type,
    )
```

Update `_to_vcard` (currently lines 365-382) to embed the photo when present, right before the final `return vcard`:

```python
def _to_vcard(contact: CanonicalContact):
    vcard = vobject.vCard()
    vcard.add("fn").value = contact.display_name
    # Apple's CardDAV server rejects any vCard PUT without a UID property
    # ("null vcard or UID missing from vcard"). Derive it from the contact's
    # stable local canonical id so repeated pushes/updates of the SAME
    # contact keep the same UID rather than getting a new random one each
    # time (which would confuse iCloud's own change tracking).
    vcard.add("uid").value = f"contacts-sync-{contact.id}"
    name = vcard.add("n")
    name.value = vobject.vcard.Name(family=contact.family_name or "", given=contact.given_name or "")
    for email in contact.emails:
        vcard.add("email").value = email.value
    for phone in contact.phones:
        vcard.add("tel").value = phone.value
    if contact.notes:
        vcard.add("note").value = contact.notes
    if contact.photo_data:
        photo = vcard.add("photo")
        photo.value = contact.photo_data
        photo.encoding_param = "b"
        photo.type_param = _content_type_to_vcard_type(contact.photo_content_type)
    return vcard
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_icloud_adapter.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add contacts_sync/adapters/icloud.py tests/test_icloud_adapter.py
git commit -m "Sync contact photos through embedded iCloud vCard PHOTO property"
```

---

### Task 5: Google adapter — photo pull + push

**Files:**
- Modify: `contacts_sync/adapters/google.py`
- Test: `tests/test_google_adapter.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_google_adapter.py`:

```python
def test_list_changes_fetches_photo_bytes_for_non_default_photo(mocker):
    person_with_photo = {
        **FAKE_PERSON,
        "photos": [{"url": "https://photo.example/jane.jpg", "default": False}],
    }
    service = _fake_service(mocker, {"connections": [person_with_photo], "nextSyncToken": "sync-2"})
    mocker.patch("contacts_sync.adapters.google.build", return_value=service)
    fake_response = mocker.Mock(content=b"fake-photo-bytes", headers={"Content-Type": "image/jpeg"})
    fake_response.raise_for_status = mocker.Mock()
    get_mock = mocker.patch("contacts_sync.adapters.google.requests.get", return_value=fake_response)
    adapter = GoogleAdapter(credentials=mocker.Mock(token="fake-token"))

    change_set = adapter.list_changes(None)

    assert change_set.changes[0].contact.photo_data == b"fake-photo-bytes"
    assert change_set.changes[0].contact.photo_content_type == "image/jpeg"
    get_mock.assert_called_once_with(
        "https://photo.example/jane.jpg", headers={"Authorization": "Bearer fake-token"}
    )


def test_list_changes_skips_photo_fetch_when_only_default_photo(mocker):
    person_with_default_photo = {
        **FAKE_PERSON,
        "photos": [{"url": "https://photo.example/default.jpg", "default": True}],
    }
    service = _fake_service(mocker, {"connections": [person_with_default_photo], "nextSyncToken": "sync-2"})
    mocker.patch("contacts_sync.adapters.google.build", return_value=service)
    get_mock = mocker.patch("contacts_sync.adapters.google.requests.get")
    adapter = GoogleAdapter(credentials=mocker.Mock(token="fake-token"))

    change_set = adapter.list_changes(None)

    assert change_set.changes[0].contact.photo_data is None
    get_mock.assert_not_called()


def test_create_pushes_photo_via_update_contact_photo(mocker):
    import base64
    from contacts_sync.models import CanonicalContact, Email

    people = mocker.Mock()
    people.createContact.return_value.execute.return_value = {"resourceName": "people/789", "etag": "e1"}
    service = mocker.Mock()
    service.people.return_value = people
    mocker.patch("contacts_sync.adapters.google.build", return_value=service)
    adapter = GoogleAdapter(credentials=mocker.Mock(token="fake-token"))

    contact = CanonicalContact(
        display_name="New", emails=[Email(value="n@e.com")],
        photo_data=b"fake-photo-bytes", photo_content_type="image/jpeg",
    )
    adapter.create(contact)

    people.updateContactPhoto.assert_called_once_with(
        resourceName="people/789",
        body={"photoBytes": base64.b64encode(b"fake-photo-bytes").decode()},
    )


def test_update_does_not_push_photo_when_absent(mocker):
    from contacts_sync.models import CanonicalContact, Email

    people = mocker.Mock()
    people.updateContact.return_value.execute.return_value = {}
    service = mocker.Mock()
    service.people.return_value = people
    mocker.patch("contacts_sync.adapters.google.build", return_value=service)
    adapter = GoogleAdapter(credentials=mocker.Mock(token="fake-token"))

    adapter.update("people/123", CanonicalContact(display_name="Jane", emails=[Email(value="j@e.com")]))

    people.updateContactPhoto.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_google_adapter.py -v -k photo`
Expected: FAIL — `photos` isn't in `PERSON_FIELDS`, `_to_canonical` doesn't fetch bytes, and `create`/`update` never call `updateContactPhoto`.

- [ ] **Step 3: Implement photo pull in `google.py`**

Add `photos` to `PERSON_FIELDS` (currently line 14):

```python
PERSON_FIELDS = "names,emailAddresses,phoneNumbers,biographies,photos"
```

Add `import requests` and `import base64` to the top of the file (after the existing imports, currently lines 1-12):

```python
import base64

import requests
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
```

Store credentials on the instance in `__init__` (currently lines 20-21):

```python
    def __init__(self, credentials):
        self._credentials = credentials
        self._service = build("people", "v1", credentials=credentials)
```

Update the `_to_canonical` call site inside `list_changes` (currently line 47, `contact=_to_canonical(person)`):

```python
                    changes.append(
                        ChangedContact(
                            provider_id=person["resourceName"],
                            contact=_to_canonical(person, self._credentials.token),
                            updated_at="",
                            etag=person.get("etag"),
                        )
                    )
```

Update `_to_canonical` itself (currently lines 118-131):

```python
def _to_canonical(person: dict, access_token: Optional[str] = None) -> CanonicalContact:
    names = person.get("names", [{}])[0] if person.get("names") else {}
    emails = [Email(value=e["value"]) for e in person.get("emailAddresses", [])]
    phones = [Phone(value=p["value"]) for p in person.get("phoneNumbers", [])]
    notes = person.get("biographies", [{}])[0].get("value") if person.get("biographies") else None
    photo_data = None
    photo_content_type = None
    non_default_photos = [p for p in person.get("photos", []) if not p.get("default")]
    if non_default_photos and access_token:
        response = requests.get(
            non_default_photos[0]["url"], headers={"Authorization": f"Bearer {access_token}"}
        )
        response.raise_for_status()
        photo_data = response.content
        photo_content_type = response.headers.get("Content-Type")
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
```

- [ ] **Step 4: Implement photo push in `google.py`**

Update `create` (currently lines 65-68):

```python
    def create(self, contact: CanonicalContact) -> tuple[str, Optional[str]]:
        body = _to_person(contact)
        response = self._service.people().createContact(body=body).execute(num_retries=5)
        resource_name = response["resourceName"]
        if contact.photo_data:
            self._service.people().updateContactPhoto(
                resourceName=resource_name,
                body={"photoBytes": base64.b64encode(contact.photo_data).decode()},
            ).execute(num_retries=5)
        return resource_name, response.get("etag")
```

Update `update` (currently lines 70-102) — add the photo push right before the final `return`:

```python
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
                resourceName=provider_id, updatePersonFields=PERSON_FIELDS, body=body
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
                resourceName=provider_id, updatePersonFields=PERSON_FIELDS, body=body
            ).execute(num_retries=5)
        if contact.photo_data:
            self._service.people().updateContactPhoto(
                resourceName=provider_id,
                body={"photoBytes": base64.b64encode(contact.photo_data).decode()},
            ).execute(num_retries=5)
        return response.get("etag") if isinstance(response, dict) else None
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_google_adapter.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 6: Commit**

```bash
git add contacts_sync/adapters/google.py tests/test_google_adapter.py
git commit -m "Sync contact photos via Google People API photo endpoints"
```

---

### Task 6: Microsoft adapter — photo pull + push

**Files:**
- Modify: `contacts_sync/adapters/microsoft.py`
- Test: `tests/test_microsoft_adapter.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_microsoft_adapter.py`:

```python
def test_list_changes_fetches_photo_for_changed_contact(requests_mock):
    requests_mock.get(
        f"{BASE}/me/contactFolders/contacts/contacts/delta",
        json={
            "value": [{"id": "AAMk123", "displayName": "Jane Doe", "emailAddresses": [{"address": "jane@example.com"}]}],
            "@odata.deltaLink": f"{BASE}/x?$deltatoken=y",
        },
    )
    requests_mock.get(
        f"{BASE}/me/contacts/AAMk123/photo/$value",
        content=b"fake-photo-bytes",
        headers={"Content-Type": "image/jpeg"},
    )
    adapter = MicrosoftAdapter(token_provider=lambda: "fake-token")

    change_set = adapter.list_changes(None)

    assert change_set.changes[0].contact.photo_data == b"fake-photo-bytes"
    assert change_set.changes[0].contact.photo_content_type == "image/jpeg"


def test_list_changes_treats_404_photo_as_no_photo(requests_mock):
    requests_mock.get(
        f"{BASE}/me/contactFolders/contacts/contacts/delta",
        json={
            "value": [{"id": "AAMk123", "displayName": "Jane Doe"}],
            "@odata.deltaLink": f"{BASE}/x?$deltatoken=y",
        },
    )
    requests_mock.get(f"{BASE}/me/contacts/AAMk123/photo/$value", status_code=404)
    adapter = MicrosoftAdapter(token_provider=lambda: "fake-token")

    change_set = adapter.list_changes(None)

    assert change_set.changes[0].contact.photo_data is None


def test_create_pushes_photo_bytes(requests_mock):
    requests_mock.post(f"{BASE}/me/contacts", json={"id": "AAMk-new"})
    photo_put = requests_mock.put(f"{BASE}/me/contacts/AAMk-new/photo/$value", status_code=204)
    adapter = MicrosoftAdapter(token_provider=lambda: "fake-token")

    contact = CanonicalContact(
        display_name="New", emails=[Email(value="n@e.com")],
        photo_data=b"fake-photo-bytes", photo_content_type="image/jpeg",
    )
    adapter.create(contact)

    assert photo_put.call_count == 1
    assert requests_mock.last_request.body == b"fake-photo-bytes"
    assert requests_mock.last_request.headers["Content-Type"] == "image/jpeg"


def test_update_does_not_push_photo_when_absent(requests_mock):
    requests_mock.patch(f"{BASE}/me/contacts/AAMk1", json={"id": "AAMk1"})
    photo_put = requests_mock.put(f"{BASE}/me/contacts/AAMk1/photo/$value", status_code=204)
    adapter = MicrosoftAdapter(token_provider=lambda: "fake-token")

    adapter.update("AAMk1", CanonicalContact(display_name="Jane", emails=[Email(value="j@e.com")]))

    assert photo_put.call_count == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_microsoft_adapter.py -v -k photo`
Expected: FAIL — `list_changes` never fetches `/photo/$value`, and `create`/`update` never PUT photo bytes.

- [ ] **Step 3: Implement photo pull in `microsoft.py`**

Add a helper function right after `MicrosoftAdapter` class (currently after line 103, before `_to_canonical` at line 106):

```python
def _populate_photo(contact: CanonicalContact, contact_id: str, headers: dict) -> None:
    response = request_with_retry("GET", f"{GRAPH_BASE}/me/contacts/{contact_id}/photo/$value", headers=headers)
    if response.status_code == 404:
        return
    response.raise_for_status()
    contact.photo_data = response.content
    contact.photo_content_type = response.headers.get("Content-Type")
```

Update the loop body in `list_changes` (currently lines 54-65) to call it for non-deleted items:

```python
            for item in body.get("value", []):
                if "@removed" in item:
                    changes.append(ChangedContact(provider_id=item["id"], contact=None, updated_at="", deleted=True))
                    continue
                contact = _to_canonical(item)
                _populate_photo(contact, item["id"], self._headers())
                changes.append(
                    ChangedContact(
                        provider_id=item["id"],
                        contact=contact,
                        updated_at=item.get("lastModifiedDateTime", ""),
                        etag=item.get("@odata.etag"),
                    )
                )
```

- [ ] **Step 4: Implement photo push in `microsoft.py`**

Update `create` (currently lines 72-78):

```python
    def create(self, contact: CanonicalContact) -> tuple[str, Optional[str]]:
        response = request_with_retry(
            "POST", f"{GRAPH_BASE}/me/contacts", headers=self._headers(), json=_to_graph(contact)
        )
        response.raise_for_status()
        body = response.json()
        contact_id = body["id"]
        if contact.photo_data:
            self._push_photo(contact_id, contact)
        return contact_id, body.get("@odata.etag")
```

Update `update` (currently lines 80-98) — add the photo push before the final `return`:

```python
    def update(self, provider_id: str, contact: CanonicalContact) -> Optional[str]:
        # Graph's PATCH returns 204 No Content by default; asking for
        # `return=representation` makes it echo back the updated entity
        # (including its new @odata.etag) so we can record it and suppress the
        # inevitable echo on the next delta pull.
        headers = {**self._headers(), "Prefer": "return=representation"}
        response = request_with_retry(
            "PATCH", f"{GRAPH_BASE}/me/contacts/{provider_id}", headers=headers, json=_to_graph(contact)
        )
        if response.status_code == 404:
            raise ProviderResourceGoneError(
                f"Microsoft contact {provider_id} not found (404) - link is stale"
            )
        response.raise_for_status()
        if contact.photo_data:
            self._push_photo(provider_id, contact)
        # Defensively handle an empty/204 body despite the Prefer header - the
        # next pull will backfill the etag in that case.
        if response.status_code == 204 or not response.content:
            return None
        return response.json().get("@odata.etag")
```

Add the `_push_photo` helper method right after `update` (before `delete`, currently at line 100):

```python
    def _push_photo(self, contact_id: str, contact: CanonicalContact) -> None:
        headers = {**self._headers(), "Content-Type": contact.photo_content_type or "image/jpeg"}
        request_with_retry(
            "PUT", f"{GRAPH_BASE}/me/contacts/{contact_id}/photo/$value", headers=headers, data=contact.photo_data
        )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_microsoft_adapter.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 6: Commit**

```bash
git add contacts_sync/adapters/microsoft.py tests/test_microsoft_adapter.py
git commit -m "Sync contact photos via Microsoft Graph photo/\$value endpoint"
```

---

### Task 7: Update README known limitations

**Files:**
- Modify: `README.md:44`

- [ ] **Step 1: Remove the now-stale limitation line**

In `README.md`, delete this line from the "Known limitations" section (currently line 44):

```markdown
- Photo sync is not implemented.
```

- [ ] **Step 2: Run the full test suite**

Run: `pytest -q`
Expected: PASS, all tests (README has no tests, but this confirms nothing else broke)

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "Remove stale 'photo sync not implemented' limitation from README"
```
