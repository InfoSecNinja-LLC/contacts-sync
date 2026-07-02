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


def test_configure_logging_is_idempotent():
    from contacts_sync.cli import _configure_logging

    logger = logging.getLogger("contacts_sync.sync")
    original_handlers = list(logger.handlers)
    try:
        logger.handlers = []
        _configure_logging()
        _configure_logging()
        _configure_logging()
        assert len(logger.handlers) == 1
    finally:
        logger.handlers = original_handlers
