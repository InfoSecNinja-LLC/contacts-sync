import subprocess
import pytest
from contacts_sync.auth.onepassword import op_read, op_set_field, OnePasswordError

def test_op_read_returns_stdout(mocker):
    mocker.patch(
        "contacts_sync.auth.onepassword.subprocess.run",
        return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="secret-value\n", stderr=""),
    )
    assert op_read("op://vault/item/field") == "secret-value"

def test_op_read_missing_cli_raises(mocker):
    mocker.patch("contacts_sync.auth.onepassword.subprocess.run", side_effect=FileNotFoundError())
    with pytest.raises(OnePasswordError, match="not found on PATH"):
        op_read("op://vault/item/field")

def test_op_read_locked_session_raises(mocker):
    mocker.patch(
        "contacts_sync.auth.onepassword.subprocess.run",
        return_value=subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="not signed in"),
    )
    with pytest.raises(OnePasswordError, match="unlocked"):
        op_read("op://vault/item/field")

def test_op_set_field_edits_existing_item(mocker):
    run_mock = mocker.patch(
        "contacts_sync.auth.onepassword.subprocess.run",
        return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
    )
    op_set_field("vault", "google", "refresh_token", "abc")
    assert run_mock.call_args_list[0].args[0][:3] == ["op", "item", "edit"]

def test_op_set_field_falls_back_to_create(mocker):
    edit_fail = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="no such item")
    create_ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    run_mock = mocker.patch(
        "contacts_sync.auth.onepassword.subprocess.run", side_effect=[edit_fail, create_ok]
    )
    op_set_field("vault", "google", "refresh_token", "abc")
    assert run_mock.call_args_list[1].args[0][:3] == ["op", "item", "create"]
