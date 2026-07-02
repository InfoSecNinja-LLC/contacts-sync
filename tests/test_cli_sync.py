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
