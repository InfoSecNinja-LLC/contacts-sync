from contacts_sync.models import CanonicalContact, Email, Phone
from contacts_sync.matcher import match_contact, normalize_email, normalize_phone


def test_normalize_email():
    assert normalize_email(" Jane@Example.com ") == "jane@example.com"


def test_normalize_phone():
    assert normalize_phone("(555) 123-4567") == "5551234567"
    assert normalize_phone("+1 555 123 4567") == "+15551234567"


def test_match_by_email():
    existing = [CanonicalContact(id=1, display_name="Jane", emails=[Email(value="jane@example.com")])]
    candidate = CanonicalContact(display_name="J. Doe", emails=[Email(value="Jane@Example.com")])
    result = match_contact(candidate, existing)
    assert result.status == "matched"
    assert result.contact_id == 1


def test_match_by_phone_when_no_email():
    existing = [CanonicalContact(id=1, display_name="Jane", phones=[Phone(value="+15551234567")])]
    candidate = CanonicalContact(display_name="J. Doe", phones=[Phone(value="(555) 123-4567")])
    result = match_contact(candidate, existing)
    assert result.status == "matched"
    assert result.contact_id == 1


def test_ambiguous_email_match():
    existing = [
        CanonicalContact(id=1, display_name="Jane A", emails=[Email(value="shared@example.com")]),
        CanonicalContact(id=2, display_name="Jane B", emails=[Email(value="shared@example.com")]),
    ]
    candidate = CanonicalContact(display_name="Jane", emails=[Email(value="shared@example.com")])
    result = match_contact(candidate, existing)
    assert result.status == "ambiguous"
    assert set(result.candidate_ids) == {1, 2}


def test_no_match_creates_new():
    existing = [CanonicalContact(id=1, display_name="Jane", emails=[Email(value="jane@example.com")])]
    candidate = CanonicalContact(display_name="Someone Else", emails=[Email(value="someone@example.com")])
    result = match_contact(candidate, existing)
    assert result.status == "no_match"


def test_name_only_match_requires_no_contact_info():
    existing = [CanonicalContact(id=1, display_name="Jane Doe")]
    candidate = CanonicalContact(display_name="Jane Doe")
    result = match_contact(candidate, existing)
    assert result.status == "matched"
    assert result.contact_id == 1


def test_canonicalize_phone_collapses_same_number_formats():
    from contacts_sync.matcher import canonicalize_phone
    # The exact real-world case that caused non-converging sync churn:
    assert canonicalize_phone("(972) 799-4768") == "+19727994768"
    assert canonicalize_phone("+19727994768") == "+19727994768"
    # 11-digit with leading 1
    assert canonicalize_phone("1-972-799-4768") == "+19727994768"
    # already-plus international kept as-is (digits only)
    assert canonicalize_phone("+44 7911 123456") == "+447911123456"
    # short codes / un-E.164-able values kept unchanged (trimmed)
    assert canonicalize_phone("24273") == "24273"
    assert canonicalize_phone("  555-CALL  ") == "555-CALL"
