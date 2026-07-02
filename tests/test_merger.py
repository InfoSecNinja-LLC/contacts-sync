from contacts_sync.merger import merge_single_value, merge_multi_value
from contacts_sync.matcher import normalize_email


def test_merge_single_value_no_change_keeps_current():
    value, meta = merge_single_value("Jane", "2026-01-01T00:00:00Z", None, "2026-02-01T00:00:00Z")
    assert value == "Jane"
    assert meta == "2026-01-01T00:00:00Z"


def test_merge_single_value_newer_incoming_wins():
    value, meta = merge_single_value("Jane", "2026-01-01T00:00:00Z", "Jane Doe", "2026-02-01T00:00:00Z")
    assert value == "Jane Doe"
    assert meta == "2026-02-01T00:00:00Z"


def test_merge_single_value_older_incoming_loses():
    value, meta = merge_single_value("Jane Doe", "2026-02-01T00:00:00Z", "Jane", "2026-01-01T00:00:00Z")
    assert value == "Jane Doe"
    assert meta == "2026-02-01T00:00:00Z"


def test_merge_single_value_no_prior_meta_accepts_incoming():
    value, meta = merge_single_value(None, None, "Jane", "2026-01-01T00:00:00Z")
    assert value == "Jane"
    assert meta == "2026-01-01T00:00:00Z"


def test_merge_multi_value_unions_and_dedupes():
    result = merge_multi_value(
        ["Jane@Example.com"], ["jane@example.com", "jane2@example.com"], normalize=normalize_email
    )
    assert result == ["Jane@Example.com", "jane2@example.com"]


def test_merge_multi_value_empty_incoming_keeps_current():
    result = merge_multi_value(["a@example.com"], [], normalize=normalize_email)
    assert result == ["a@example.com"]
