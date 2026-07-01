from typer.testing import CliRunner

from contacts_sync.cli import app

runner = CliRunner()


def test_version_command():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "contacts-sync" in result.stdout
