from dataclasses import dataclass
from contacts_sync.models import CanonicalContact


def normalize_email(value: str) -> str:
    return value.strip().lower()


def normalize_phone(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit() or ch == "+")


def canonicalize_phone(value: str) -> str:
    """Return a single stable representation of a phone number for storage.

    Providers (notably Google) normalize/dedupe phone numbers on write, so
    storing the same number in two source formats (e.g. "(972) 799-4768" and
    "+19727994768") makes the push/pull round-trip never converge - the merge
    keeps both, we push both, the provider collapses them, and the next pull
    looks changed again. Canonicalizing to E.164 where possible keeps exactly
    one representation so the round-trip stabilizes.

    US/NANP-centric (matching the rest of this module's assumptions): a bare
    10-digit number gets a "+1" prefix, an 11-digit "1..." gets a "+", and a
    value already starting with "+" is kept (digits only). Anything else
    (short codes, international numbers written without a "+", extensions) is
    returned trimmed but otherwise unchanged, since it can't be canonicalized
    to E.164 without guessing a country.
    """
    stripped = value.strip()
    if stripped.startswith("+"):
        return "+" + "".join(ch for ch in stripped if ch.isdigit())
    digits = "".join(ch for ch in stripped if ch.isdigit())
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return stripped


def _phone_match_key(value: str) -> str:
    """Comparison key for phone matching, tolerant of a leading country code.

    normalize_phone() preserves a leading "+" and any country code as-is, so
    "(555) 123-4567" -> "5551234567" while "+1 555 123 4567" -> "+15551234567".
    These represent the same number but don't compare equal as plain strings.
    For matching purposes we compare on the trailing digits (national
    significant number), which is stable regardless of whether a country
    code/"+" prefix was present in the source data.
    """
    digits = normalize_phone(value).lstrip("+")
    return digits[-10:] if len(digits) >= 10 else digits


@dataclass
class MatchResult:
    status: str  # "matched" | "ambiguous" | "no_match"
    contact_id: int | None = None
    candidate_ids: list[int] | None = None


def match_contact(candidate: CanonicalContact, existing: list[CanonicalContact]) -> MatchResult:
    candidate_emails = {normalize_email(e.value) for e in candidate.emails}
    if candidate_emails:
        matches = [c for c in existing if candidate_emails & {normalize_email(e.value) for e in c.emails}]
        if len(matches) == 1:
            return MatchResult("matched", contact_id=matches[0].id)
        if len(matches) > 1:
            return MatchResult("ambiguous", candidate_ids=[c.id for c in matches])

    candidate_phones = {_phone_match_key(p.value) for p in candidate.phones}
    if candidate_phones:
        matches = [c for c in existing if candidate_phones & {_phone_match_key(p.value) for p in c.phones}]
        if len(matches) == 1:
            return MatchResult("matched", contact_id=matches[0].id)
        if len(matches) > 1:
            return MatchResult("ambiguous", candidate_ids=[c.id for c in matches])

    if candidate.display_name and not candidate_emails and not candidate_phones:
        name = candidate.display_name.strip().lower()
        matches = [c for c in existing if c.display_name.strip().lower() == name]
        if len(matches) == 1:
            return MatchResult("matched", contact_id=matches[0].id)
        if len(matches) > 1:
            return MatchResult("ambiguous", candidate_ids=[c.id for c in matches])

    return MatchResult("no_match")
