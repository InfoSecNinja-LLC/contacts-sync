from contacts_sync.models import CanonicalContact
from contacts_sync.names import find_name_fix_candidates, split_full_name


def test_split_two_word_name():
    assert split_full_name("Pallavi Sharma") == ("Pallavi", "Sharma")


def test_split_multi_word_name_keeps_all_but_last_in_given():
    assert split_full_name("Kinjal Dad USA") == ("Kinjal Dad", "USA")
    assert split_full_name("Hina Shah Khusbu's Mom") == ("Hina Shah Khusbu's", "Mom")


def test_split_single_word_returns_no_family():
    assert split_full_name("Josh") == ("Josh", None)


def test_split_handles_surrounding_and_multiple_whitespace():
    assert split_full_name("  Pallavi   Sharma  ") == ("Pallavi", "Sharma")


def test_candidates_selects_only_unsplit_multiword_names():
    unsplit = CanonicalContact(id=1, display_name="Pallavi Sharma", given_name="Pallavi Sharma")
    already_split = CanonicalContact(id=2, display_name="Jagruti Patel", given_name="Jagruti", family_name="Patel")
    single_word = CanonicalContact(id=3, display_name="Josh", given_name="Josh")
    no_given = CanonicalContact(id=4, display_name="Whoever", given_name=None)

    result = find_name_fix_candidates([unsplit, already_split, single_word, no_given])

    assert [(c.id, g, f) for c, g, f in result] == [(1, "Pallavi", "Sharma")]


def test_candidates_is_idempotent_after_applying():
    contact = CanonicalContact(id=1, display_name="Pallavi Sharma", given_name="Pallavi Sharma")
    [(c, g, f)] = find_name_fix_candidates([contact])
    c.given_name, c.family_name = g, f
    assert find_name_fix_candidates([contact]) == []
