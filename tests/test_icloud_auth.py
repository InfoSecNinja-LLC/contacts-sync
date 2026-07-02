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
