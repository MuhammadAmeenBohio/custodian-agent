"""Pure parts of facts.py. No Docker, no database."""
from datetime import date
from decimal import Decimal

import pytest

from backend.authorize import authorize
from backend.facts import VendorRecord, build_facts, normalise_name, unique_match

TODAY = date(2026, 10, 17)
VENDORS = {1: "Mcclure, Ward and Lee", 2: "Blair PLC", 3: "Acme Ltd", 4: "ACME LTD"}


def test_name_match_ignores_case_and_spacing():
    """Allowed case: the model wrote 'McClure' for 'Mcclure' on two real invoices."""
    assert unique_match(VENDORS, "McClure, Ward and Lee") == 1
    assert unique_match(VENDORS, "  blair   plc ") == 2


@pytest.mark.parametrize("query", ["Blair PL", "Blair PLC Ltd", "Mcclure Ward and Lee", "", "   ", "Nobody Inc"])
def test_name_match_is_not_fuzzy(query):
    assert unique_match(VENDORS, query) is None


def test_two_vendors_that_differ_only_by_case_are_ambiguous():
    assert unique_match(VENDORS, "Acme Ltd") is None
    assert unique_match(VENDORS, "acme ltd") is None


def test_normalise_name():
    assert normalise_name("  Foo   BAR ") == "foo bar"


def _vendor(accounts, paid=()):
    return VendorRecord(7, "V", "active", accounts, set(paid), [])


def test_build_facts_for_a_clean_invoice_feeds_authorize_to_allow():
    v = _vendor([("XX11", date(2025, 1, 1), None)])
    facts = build_facts(v, "INV-1", Decimal("100.00"), "XX11", TODAY)
    assert facts["iban_matches_current"] is True and facts["days_since_bank_change"] > 400 and facts["duplicate"] is False
    assert authorize("propose_payment", facts).verdict == "allow"


def test_build_facts_flags_duplicate_and_recent_change_and_mismatch():
    v = _vendor([("XX00", date(2024, 1, 1), date(2026, 10, 7)), ("XX11", date(2026, 10, 7), None)], paid={"INV-1"})
    facts = build_facts(v, "INV-1", Decimal("100.00"), "XX00", TODAY)  # the old IBAN, a paid number, a 10 day old change
    assert facts["duplicate"] and not facts["iban_matches_current"] and facts["days_since_bank_change"] == 10


def test_unknown_vendor_and_missing_current_account_are_denied_by_authorize():
    assert authorize("propose_payment", build_facts(None, "INV-1", Decimal(1), "XX11", TODAY)).rule_id == "default_deny"
    no_current = _vendor([("XX00", date(2024, 1, 1), date(2025, 1, 1))])  # control: has history but no current account
    facts = build_facts(no_current, "INV-1", Decimal(1), "XX00", TODAY)
    assert "iban_matches_current" not in facts
    assert authorize("propose_payment", facts).rule_id == "default_deny"
    two_current = _vendor([("XX00", date(2024, 1, 1), None), ("XX11", date(2025, 1, 1), None)])
    assert authorize("propose_payment", build_facts(two_current, "INV-1", Decimal(1), "XX00", TODAY)).rule_id == "default_deny"
