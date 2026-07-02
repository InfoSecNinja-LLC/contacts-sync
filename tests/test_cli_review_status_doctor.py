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

def test_review_handles_non_numeric_input_without_crashing(mocker, tmp_path):
    db_path = str(tmp_path / "contacts.db")
    mocker.patch("contacts_sync.cli.DB_PATH", db_path)
    db = Database(db_path)
    db.migrate()
    from contacts_sync.models import CanonicalContact
    id_a = db.create_contact(CanonicalContact(display_name="Jane A"))
    db.save_pending_match("google", "g-1", [id_a], "{}")
    mocker.patch("contacts_sync.cli.typer.prompt", return_value="banana")

    result = runner.invoke(app, ["review"])

    assert result.exit_code == 0
    assert result.exception is None
    assert db.get_link("google", "g-1") is None
    assert len(db.list_pending_matches()) == 1

def test_review_handles_out_of_range_candidate_id_without_crashing(mocker, tmp_path):
    db_path = str(tmp_path / "contacts.db")
    mocker.patch("contacts_sync.cli.DB_PATH", db_path)
    db = Database(db_path)
    db.migrate()
    from contacts_sync.models import CanonicalContact
    id_a = db.create_contact(CanonicalContact(display_name="Jane A"))
    db.save_pending_match("google", "g-1", [id_a], "{}")
    mocker.patch("contacts_sync.cli.typer.prompt", return_value="999")

    result = runner.invoke(app, ["review"])

    assert result.exit_code == 0
    assert result.exception is None
    assert db.get_link("google", "g-1") is None
    assert len(db.list_pending_matches()) == 1

def test_review_skip_leaves_match_pending(mocker, tmp_path):
    db_path = str(tmp_path / "contacts.db")
    mocker.patch("contacts_sync.cli.DB_PATH", db_path)
    db = Database(db_path)
    db.migrate()
    from contacts_sync.models import CanonicalContact
    id_a = db.create_contact(CanonicalContact(display_name="Jane A"))
    db.save_pending_match("google", "g-1", [id_a], "{}")
    mocker.patch("contacts_sync.cli.typer.prompt", return_value="skip")

    result = runner.invoke(app, ["review"])

    assert result.exit_code == 0
    assert db.get_link("google", "g-1") is None
    assert len(db.list_pending_matches()) == 1

def test_status_reports_nonzero_contact_count(mocker, tmp_path):
    db_path = str(tmp_path / "contacts.db")
    mocker.patch("contacts_sync.cli.DB_PATH", db_path)
    db = Database(db_path)
    db.migrate()
    from contacts_sync.models import CanonicalContact
    db.create_contact(CanonicalContact(display_name="A"))
    db.create_contact(CanonicalContact(display_name="B"))

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    assert "2 contacts" in result.stdout

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
