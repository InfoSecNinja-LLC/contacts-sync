import pytest
from contacts_sync.auth.env_store import env_read, env_set, EnvStoreError

def test_env_read_returns_value(mocker):
    mocker.patch("contacts_sync.auth.env_store.dotenv_values", return_value={"KEY": "secret-value"})
    assert env_read("KEY") == "secret-value"

def test_env_read_missing_key_raises(mocker):
    mocker.patch("contacts_sync.auth.env_store.dotenv_values", return_value={})
    with pytest.raises(EnvStoreError, match="KEY"):
        env_read("KEY")

def test_env_set_writes_key(mocker, tmp_path):
    env_path = tmp_path / ".env"
    mocker.patch("contacts_sync.auth.env_store.ENV_PATH", env_path)
    set_key_mock = mocker.patch("contacts_sync.auth.env_store.set_key")

    env_set("KEY", "value")

    assert env_path.exists()
    set_key_mock.assert_called_once_with(str(env_path), "KEY", "value", quote_mode="always")
