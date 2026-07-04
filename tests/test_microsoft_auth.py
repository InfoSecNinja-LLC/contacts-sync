from contacts_sync.auth import microsoft_auth


def test_run_device_code_auth_saves_token_cache(mocker):
    fake_app = mocker.Mock()
    fake_app.initiate_device_flow.return_value = {"message": "go to https://microsoft.com/devicelogin"}
    mocker.patch("contacts_sync.auth.microsoft_auth.msal.PublicClientApplication", return_value=fake_app)
    mocker.patch("contacts_sync.auth.microsoft_auth._load_cache", return_value=mocker.Mock(has_state_changed=True, serialize=lambda: "cache-blob"))
    save_mock = mocker.patch("contacts_sync.auth.microsoft_auth.op_set_field")

    microsoft_auth.run_device_code_auth("client-id-1")

    fake_app.acquire_token_by_device_flow.assert_called_once()
    save_mock.assert_called_once_with("Private", "microsoft", "token_cache", "cache-blob")


def test_get_token_provider_uses_cached_account(mocker):
    fake_account = {"username": "me@outlook.com"}
    fake_app = mocker.Mock()
    fake_app.get_accounts.return_value = [fake_account]
    fake_app.acquire_token_silent.return_value = {"access_token": "tok-1"}
    mocker.patch("contacts_sync.auth.microsoft_auth.msal.PublicClientApplication", return_value=fake_app)
    mocker.patch("contacts_sync.auth.microsoft_auth._load_cache", return_value=mocker.Mock(has_state_changed=False))

    get_token = microsoft_auth.get_token_provider("client-id-1")
    token = get_token()

    assert token == "tok-1"
    fake_app.acquire_token_silent.assert_called_once_with(microsoft_auth.SCOPES, account=fake_account)


def test_get_token_provider_raises_when_no_cached_token(mocker):
    fake_app = mocker.Mock()
    fake_app.get_accounts.return_value = []
    fake_app.acquire_token_silent.return_value = None
    mocker.patch("contacts_sync.auth.microsoft_auth.msal.PublicClientApplication", return_value=fake_app)
    mocker.patch("contacts_sync.auth.microsoft_auth._load_cache", return_value=mocker.Mock(has_state_changed=False))

    get_token = microsoft_auth.get_token_provider("client-id-1")
    import pytest
    with pytest.raises(RuntimeError, match="auth microsoft"):
        get_token()


def test_get_token_provider_caches_token_across_calls(mocker):
    fake_account = {"username": "me@outlook.com"}
    fake_app = mocker.Mock()
    fake_app.get_accounts.return_value = [fake_account]
    fake_app.acquire_token_silent.return_value = {"access_token": "tok-1", "expires_in": 3600}
    mocker.patch("contacts_sync.auth.microsoft_auth.msal.PublicClientApplication", return_value=fake_app)
    load_cache = mocker.patch(
        "contacts_sync.auth.microsoft_auth._load_cache",
        return_value=mocker.Mock(has_state_changed=False),
    )

    get_token = microsoft_auth.get_token_provider("client-id-1")
    t1 = get_token()
    t2 = get_token()
    t3 = get_token()

    assert t1 == t2 == t3 == "tok-1"
    # 1Password (_load_cache) and MSAL acquisition happen ONCE, not per call.
    assert load_cache.call_count == 1
    assert fake_app.acquire_token_silent.call_count == 1
