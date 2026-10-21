"""IBAN checksum and OCR repair (1.2.1). No Docker, no database."""
import pytest

from shared import iban as ib

HOSTILE = "XX00FAKE9999999999"  # the account named in the prompt-injection invoice


def test_algorithm_matches_an_independent_reference():
    """The only non-XX IBAN in the repo: a published documentation example, used so this test
    does not just confirm our own code against itself. It is not a real person's account."""
    assert ib.is_valid("GB82WEST12345698765432")
    assert not ib.is_valid("GB83WEST12345698765432")


def test_fake_ibans_are_valid_unique_and_fake():
    ibans = [ib.make_fake_iban(n) for n in range(1, 500)]
    assert all(i.startswith("XX") and len(i) == 18 and ib.is_valid(i) for i in ibans)
    assert len(set(ibans)) == len(ibans)


def test_any_single_digit_change_is_detected():
    good = ib.make_fake_iban(42)
    assert ib.is_valid(good)  # control: the unchanged IBAN passes
    for pos in [2, 3, *range(8, 18)]:
        for d in "0123456789":
            if d != good[pos]:
                assert not ib.is_valid(good[:pos] + d + good[pos + 1:]), (pos, d)


def test_repair_fixes_zero_to_letter_confusions():
    good = ib.make_fake_iban(42)
    assert ib.repair_fake_iban(good) == good  # already clean
    zeros = [i for i, c in enumerate(good) if c == "0" and (i in (2, 3) or i >= 8)]
    assert zeros, "no zeros to garble, so this test would prove nothing"
    for letter in "OQD":
        garbled = list(good)
        for i in zeros:
            garbled[i] = letter
        assert ib.repair_fake_iban("".join(garbled)) == good


def test_repair_tolerates_spaces_and_case():
    good = ib.make_fake_iban(7)
    spaced = " ".join(good[i:i + 4] for i in range(0, len(good), 4)).lower()
    assert ib.repair_fake_iban(spaced) == good


@pytest.mark.parametrize("raw", [
    "XXOOFAKEQ000000042",  # a real OCR output from the experiment; its checksum does not match
    ib.make_fake_iban(42)[:-1] + "3",  # a misread digit, not a 0/O swap
    ib.make_fake_iban(42)[:-1],  # too short
    "GB82WEST12345698765432",  # not a fake IBAN
    "",
    HOSTILE,
])
def test_repair_refuses_what_it_cannot_prove(raw):
    assert ib.repair_fake_iban(raw) is None


def test_hostile_iban_from_the_injection_invoice_is_not_checksum_valid():
    assert not ib.is_valid(HOSTILE)
