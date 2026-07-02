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
