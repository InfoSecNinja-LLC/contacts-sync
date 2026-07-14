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


def test_sync_exits_cleanly_when_credentials_missing(mocker, tmp_path):
    mocker.patch("contacts_sync.cli.DB_PATH", str(tmp_path / "contacts.db"))
    mocker.patch(
        "contacts_sync.cli._build_adapters",
        side_effect=RuntimeError("No Google credentials found. Run `contacts-sync auth google` first."),
    )

    result = runner.invoke(app, ["sync", "--microsoft-client-id", "cid-1"])

    assert result.exit_code == 1
    assert "No Google credentials found" in result.stdout
    assert result.exception is None or not isinstance(result.exception, RuntimeError)


def test_sync_full_clears_sync_state_before_running(mocker, tmp_path):
    from contacts_sync.db import Database
    from contacts_sync.models import CanonicalContact

    db_path = str(tmp_path / "contacts.db")
    mocker.patch("contacts_sync.cli.DB_PATH", db_path)
    database = Database(db_path)
    database.migrate()
    contact_id = database.create_contact(CanonicalContact(display_name="Jane"))
    database.link_provider(contact_id, "google", "g-1")
    database.set_link_etag("google", "g-1", "etag-1")
    database.set_sync_token("google", "token-1")

    mocker.patch("contacts_sync.cli._build_adapters", return_value={})
    fake_result = mocker.Mock(created=0, updated=0, deleted=0, pending_review=0, provider_errors={})
    mocker.patch("contacts_sync.cli.SyncEngine.run", return_value=fake_result)

    result = runner.invoke(app, ["sync", "--full", "--microsoft-client-id", "cid-1"])

    assert result.exit_code == 0
    assert database.get_sync_token("google") is None
    assert database.get_link_etag("google", "g-1") is None


def test_sync_full_with_dry_run_leaves_sync_state_untouched(mocker, tmp_path):
    from contacts_sync.db import Database

    db_path = str(tmp_path / "contacts.db")
    mocker.patch("contacts_sync.cli.DB_PATH", db_path)
    database = Database(db_path)
    database.migrate()
    database.set_sync_token("google", "token-1")

    mocker.patch("contacts_sync.cli._build_adapters", return_value={})
    fake_result = mocker.Mock(created=0, updated=0, deleted=0, pending_review=0, provider_errors={})
    mocker.patch("contacts_sync.cli.SyncEngine.run", return_value=fake_result)

    result = runner.invoke(app, ["sync", "--full", "--dry-run", "--microsoft-client-id", "cid-1"])

    assert result.exit_code == 0
    assert database.get_sync_token("google") == "token-1"


def _seed_unsplit_contact(mocker, tmp_path):
    from contacts_sync.db import Database
    from contacts_sync.models import CanonicalContact

    db_path = str(tmp_path / "contacts.db")
    mocker.patch("contacts_sync.cli.DB_PATH", db_path)
    database = Database(db_path)
    database.migrate()
    contact_id = database.create_contact(
        CanonicalContact(display_name="Pallavi Sharma", given_name="Pallavi Sharma")
    )
    database.link_provider(contact_id, "google", "g-1")
    return database, contact_id


def test_fix_names_preview_shows_split_but_writes_nothing(mocker, tmp_path):
    database, contact_id = _seed_unsplit_contact(mocker, tmp_path)
    build_adapters = mocker.patch("contacts_sync.cli._build_adapters")

    result = runner.invoke(app, ["fix-names"])

    assert result.exit_code == 0
    assert "Pallavi" in result.stdout
    assert "Preview only" in result.stdout
    build_adapters.assert_not_called()
    contact = database.get_contact(contact_id)
    assert contact.given_name == "Pallavi Sharma"
    assert contact.family_name is None


def test_fix_names_apply_writes_stamps_meta_and_pushes(mocker, tmp_path):
    database, contact_id = _seed_unsplit_contact(mocker, tmp_path)
    adapter = mocker.Mock()
    adapter.update.return_value = "etag-after-fix"
    mocker.patch("contacts_sync.cli._build_adapters", return_value={"google": adapter})

    result = runner.invoke(app, ["fix-names", "--apply"])

    assert result.exit_code == 0
    contact = database.get_contact(contact_id)
    assert contact.given_name == "Pallavi"
    assert contact.family_name == "Sharma"
    # Repair is stamped so an old unsplit value pulled later can't undo it.
    assert contact.field_meta["given_name"]
    assert contact.field_meta["family_name"]
    adapter.update.assert_called_once()
    assert database.get_link_etag("google", "g-1") == "etag-after-fix"


def test_fix_names_apply_aborts_before_writing_when_credentials_missing(mocker, tmp_path):
    database, contact_id = _seed_unsplit_contact(mocker, tmp_path)
    mocker.patch(
        "contacts_sync.cli._build_adapters",
        side_effect=RuntimeError("No Google credentials found."),
    )

    result = runner.invoke(app, ["fix-names", "--apply"])

    assert result.exit_code == 1
    contact = database.get_contact(contact_id)
    assert contact.given_name == "Pallavi Sharma"
    assert contact.family_name is None


def test_push_all_pushes_every_contact(mocker, tmp_path):
    _seed_unsplit_contact(mocker, tmp_path)
    adapter = mocker.Mock()
    adapter.update.return_value = "etag-pushed"
    mocker.patch("contacts_sync.cli._build_adapters", return_value={"google": adapter})

    result = runner.invoke(app, ["push", "--all"])

    assert result.exit_code == 0
    adapter.update.assert_called_once()


def test_push_without_all_is_a_noop(mocker, tmp_path):
    build_adapters = mocker.patch("contacts_sync.cli._build_adapters")
    mocker.patch("contacts_sync.cli.DB_PATH", str(tmp_path / "contacts.db"))

    result = runner.invoke(app, ["push"])

    assert result.exit_code == 0
    build_adapters.assert_not_called()
