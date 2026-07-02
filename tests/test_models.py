from contacts_sync.models import CanonicalContact, Email, Phone, Address


def test_canonical_contact_defaults():
    contact = CanonicalContact(display_name="Jane Doe")
    assert contact.id is None
    assert contact.emails == []
    assert contact.phones == []
    assert contact.addresses == []
    assert contact.field_meta == {}
    assert contact.extra == {}


def test_canonical_contact_with_fields():
    contact = CanonicalContact(
        display_name="Jane Doe",
        emails=[Email(value="jane@example.com", primary=True)],
        phones=[Phone(value="+15551234567", type="mobile")],
        addresses=[Address(street="1 Main St", city="Springfield")],
    )
    assert contact.emails[0].value == "jane@example.com"
    assert contact.phones[0].type == "mobile"
    assert contact.addresses[0].city == "Springfield"
