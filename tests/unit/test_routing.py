"""Routing (1.4.4): run status, outcome and review_required for every kind of result. No Docker, no model.

Decisions come from the real authorize() over real facts, never hand-built, so a change to a rule shows up here.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from backend import routing
from backend.authorize import Decision, authorize
from backend.facts import VendorRecord, build_facts
from backend.readers import OcrFields, reconcile
from backend.routing import route_decision, route_unreadable
from shared.iban import make_fake_iban

TODAY = date(2026, 10, 21)
CURRENT, OTHER = make_fake_iban(11), make_fake_iban(12)
LONG_AGO = TODAY - timedelta(days=400)


def vendor(*, status="active", changed=LONG_AGO, paid=()) -> VendorRecord:
    return VendorRecord(9, "Garcia-James", status, [(CURRENT, changed, None)], set(paid), [])


def decide(v: VendorRecord | None, *, amount="1000.00", iban=CURRENT, number="INV-009-1013") -> Decision:
    return authorize("propose_payment", build_facts(v, number, Decimal(amount), iban, TODAY))


# ---- one test per row of the decision table: status, outcome AND flag ----

@pytest.mark.parametrize("name,decision,status,outcome,review", [
    ("clean", decide(vendor()), "completed", "payment_proposed", False),
    ("blocked_vendor", decide(vendor(status="blocked")), "completed", "denied", False),
    ("duplicate", decide(vendor(paid={"INV-009-1013"})), "completed", "denied", False),
    ("bank_changed_recently", decide(vendor(changed=TODAY - timedelta(days=10))), "completed", "denied", True),
    ("iban_mismatch", decide(vendor(), iban=OTHER), "completed", "denied", True),
    ("over_threshold", decide(vendor(), amount="18477.10"), "needs_review", "needs_approval", False),
    ("unknown_vendor", decide(None), "completed", "denied", True),
])
def test_decision_table(name, decision, status, outcome, review):
    r = route_decision(decision)
    assert (r.run_status, r.outcome, r.review_required) == (status, outcome, review), (name, decision)


def test_the_table_rows_really_fire_the_rule_they_are_named_for():
    """Control: otherwise a row could pass because some other rule fired."""
    assert decide(vendor(status="blocked")).rule_id == "blocked_vendor"
    assert decide(vendor(paid={"INV-009-1013"})).rule_id == "duplicate_invoice"
    assert decide(vendor(changed=TODAY - timedelta(days=10))).rule_id == "bank_changed_recently"
    assert decide(vendor(), iban=OTHER).rule_id == "iban_mismatch"
    assert decide(vendor(), amount="18477.10").rule_id == "over_threshold"
    assert decide(None).rule_id == "default_deny"


def test_a_duplicate_denial_is_logged_but_not_reviewed():
    r = route_decision(decide(vendor(paid={"INV-009-1013"})))
    assert r.log_rules == ("duplicate_invoice",) and not r.review_required


# ---- the precedence trap: the first rule hides the others ----

def test_blocked_vendor_plus_iban_mismatch_still_needs_a_human():
    d = decide(vendor(status="blocked"), iban=OTHER)
    assert d.rule_id == "blocked_vendor"  # the first rule is routine ...
    r = route_decision(d)
    assert r.review_required and "iban_mismatch fired" in r.reasons  # ... but the second is not


def test_duplicate_plus_bank_change_still_needs_a_human():
    d = decide(vendor(paid={"INV-009-1013"}, changed=TODAY - timedelta(days=3)))
    assert d.rule_id == "duplicate_invoice" and route_decision(d).review_required


def test_deny_beats_over_threshold_and_still_reports_the_review_rule():
    d = decide(vendor(changed=TODAY - timedelta(days=3)), amount="50000.00")
    r = route_decision(d)
    assert (r.run_status, r.outcome, r.review_required) == ("completed", "denied", True)


def test_fired_rules_lists_every_rule_with_deny_first():
    d = decide(vendor(status="blocked", paid={"INV-009-1013"}), iban=OTHER, amount="50000.00")
    assert d.fired_rules == ("blocked_vendor", "duplicate_invoice", "iban_mismatch", "over_threshold")
    assert decide(vendor()).fired_rules == ()


def test_an_unknown_verdict_is_a_denial_a_human_must_see():
    odd = Decision("maybe", "x", "y", "id", "v")
    r = route_decision(odd)
    assert (r.run_status, r.outcome, r.review_required) == ("completed", "denied", True)


def test_routing_is_decided_by_the_decision_alone():
    """The summary and the model never reach this module."""
    assert not {"summary", "summarise", "httpx", "psycopg"} & set(vars(routing))


# ---- the three IBAN cases ----

def reconciled(ocr_iban: str, vision_iban: str):
    ocr = OcrFields("INV-009-1013", "1,000.00", ocr_iban)
    vis = {"vendor_name": "Garcia-James", "invoice_number": "INV-009-1013", "total_amount": "1000.00", "iban": vision_iban}
    return reconcile(ocr, vis)


def broken(iban: str) -> str:
    return iban[:-1] + ("0" if iban[-1] != "0" else "1")


def test_iban_soft_flag_is_recorded_but_does_not_route():
    """One reader failed the checksum, the other passed and won."""
    rec = reconciled(broken(CURRENT), CURRENT)
    assert rec.resolved and rec.fields["iban"] == CURRENT
    assert "iban: ocr value failed the checksum" in rec.notes  # recorded
    d = decide(vendor(), iban=rec.fields["iban"])
    r = route_decision(d)
    assert (r.run_status, r.outcome, r.review_required) == ("completed", "payment_proposed", False)  # not routed


def test_iban_invalid_after_reconciliation_is_unreadable_and_goes_to_a_human():
    """No reading passes the checksum, so there is no reliable fact to decide on. authorize() must not be reached."""
    rec = reconciled(broken(CURRENT), broken(CURRENT))
    assert not rec.resolved and "iban" not in rec.fields
    r = route_unreadable(rec)
    assert (r.run_status, r.outcome, r.review_required) == ("needs_review", "unreadable", True)
    assert "iban: neither reader passes the checksum" in r.reasons


def test_iban_valid_checksum_but_not_the_vendors_current_account_is_denied_for_review():
    """An attacker can write a checksum-valid IBAN, so the checksum is no protection: rule iban_mismatch is."""
    rec = reconciled(OTHER, OTHER)
    assert rec.resolved and rec.fields["iban"] == OTHER  # both readers agree, checksum passes
    d = decide(vendor(), iban=rec.fields["iban"])
    assert d.rule_id == "iban_mismatch"
    r = route_decision(d)
    assert (r.run_status, r.outcome, r.review_required) == ("completed", "denied", True)
