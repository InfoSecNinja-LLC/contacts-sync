"""Heuristic repair for contacts whose full name is stuck in given_name.

Several import paths (old phone SIM imports, WhatsApp-synced contacts,
Google contacts created from unstructured names) leave the ENTIRE display
name in the first-name field with no last name at all. Providers then render
"Pallavi Sharma" as a first name, which sorts and displays wrong everywhere.

The split heuristic matches what phones and Google's own parser do: the last
whitespace-separated word becomes the family name, everything before it the
given name. It is deliberately conservative - contacts that already have a
family name, or whose given name is a single word, are never touched.
"""


def split_full_name(full_name: str) -> tuple[str, str | None]:
    """Split a full name into (given, family) at the LAST whitespace.

    "Pallavi Sharma"   -> ("Pallavi", "Sharma")
    "Kinjal Dad USA"   -> ("Kinjal Dad", "USA")
    "Josh"             -> ("Josh", None)   # nothing to split
    """
    parts = full_name.strip().rsplit(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return (parts[0] if parts else ""), None


def find_name_fix_candidates(contacts):
    """Return [(contact, new_given, new_family)] for every contact whose
    given_name holds a multi-word name while family_name is empty.

    Contacts with a family name already set are assumed to be correctly
    structured and are never candidates, so re-running the repair is safe
    and idempotent.
    """
    candidates = []
    for contact in contacts:
        if contact.family_name:
            continue
        given = (contact.given_name or "").strip()
        if len(given.split()) < 2:
            continue
        new_given, new_family = split_full_name(given)
        if new_family is None:
            continue
        candidates.append((contact, new_given, new_family))
    return candidates
