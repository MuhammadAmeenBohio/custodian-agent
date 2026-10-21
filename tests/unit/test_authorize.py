"""The TEMPORARY authorize() stub. Every test goes through authorize() only. No Docker, no database.

Per rule: one case where it fires (denied / needs_approval) and one neighbouring case where it does not (allowed),
plus the boundaries. Then precedence, default deny, and the Decision contract.
"""
import uuid
from decimal import Decimal

import pytest

from backend.authorize import POLICY_VERSION, authorize

CLEAN = {
    "vendor_exists": True,
    "vendor_status": "active",
    "amount": Decimal("500.00"),
    "iban_matches_current": True,
    "days_since_bank_change": 400,
    "duplicate": False,
}


def decide(**override):
    return authorize("propose_payment", {**CLEAN, **override})


def test_clean_payment_is_allowed():
    d = decide()
    assert (d.verdict, d.rule_id) == ("allow", "allow_clean_payment")


# (rule_id, facts that make it fire, expected verdict, neighbouring facts where it must NOT fire)
RULES = [
    ("blocked_vendor", {"vendor_status": "blocked"}, "deny", {"vendor_status": "active"}),
    ("duplicate_invoice", {"duplicate": True}, "deny", {"duplicate": False}),
    ("iban_mismatch", {"iban_matches_current": False}, "deny", {"iban_matches_current": True}),
    ("bank_changed_recently", {"days_since_bank_change": 10}, "deny", {"days_since_bank_change": 400}),
    ("over_threshold", {"amount": Decimal("10000.01")}, "needs_approval", {"amount": Decimal("9999.99")}),
]


@pytest.mark.parametrize("rule_id,fires,verdict,_", RULES, ids=[r[0] for r in RULES])
def test_rule_fires(rule_id, fires, verdict, _):
    d = decide(**fires)
    assert (d.verdict, d.rule_id) == (verdict, rule_id)
    assert d.reason


@pytest.mark.parametrize("rule_id,_f,_v,neighbour", RULES, ids=[r[0] for r in RULES])
def test_rule_does_not_fire_on_the_neighbouring_case(rule_id, _f, _v, neighbour):
    d = decide(**neighbour)
    assert (d.verdict, d.rule_id) == ("allow", "allow_clean_payment")


def test_amount_boundary_exactly_at_the_threshold_is_allowed():
    assert decide(amount=Decimal("10000.00")).verdict == "allow"
    assert decide(amount=Decimal("10000.01")).verdict == "needs_approval"


def test_bank_change_boundary_is_thirty_days_inclusive():
    assert decide(days_since_bank_change=30).rule_id == "bank_changed_recently"
    assert decide(days_since_bank_change=31).verdict == "allow"


# ---- precedence: deny > needs_approval > allow
def test_deny_beats_needs_approval():
    d = decide(vendor_status="blocked", amount=Decimal("50000.00"))
    assert (d.verdict, d.rule_id) == ("deny", "blocked_vendor")


def test_needs_approval_beats_allow():
    assert decide(amount=Decimal("50000.00")).verdict == "needs_approval"


def test_several_denies_report_the_first_and_mention_the_rest():
    d = decide(vendor_status="blocked", duplicate=True)
    assert d.verdict == "deny" and d.rule_id == "blocked_vendor"
    assert "duplicate_invoice" in d.reason


# ---- deny by default
def test_unknown_vendor_is_denied_by_default():
    d = authorize("propose_payment", {"vendor_exists": False})
    assert (d.verdict, d.rule_id) == ("deny", "default_deny") and "unknown vendor" in d.reason


@pytest.mark.parametrize("fact", ["vendor_status", "amount", "iban_matches_current", "days_since_bank_change", "duplicate"])
def test_a_missing_fact_is_denied_by_default(fact):
    facts = {k: v for k, v in CLEAN.items() if k != fact}
    d = authorize("propose_payment", facts)
    assert (d.verdict, d.rule_id) == ("deny", "default_deny") and fact in d.reason


@pytest.mark.parametrize("fact,value", [
    ("amount", "500.00"),           # a string, not a number
    ("amount", 500.0),              # a float
    ("amount", True),
    ("duplicate", "no"),
    ("iban_matches_current", 1),
    ("days_since_bank_change", "400"),
    ("vendor_status", "suspended"),
])
def test_a_badly_typed_fact_is_denied_by_default(fact, value):
    d = decide(**{fact: value})
    assert (d.verdict, d.rule_id) == ("deny", "default_deny") and fact in d.reason


@pytest.mark.parametrize("facts", [None, [], "x", {}, {"vendor_exists": "yes"}])
def test_garbage_input_is_denied_by_default_and_never_raises(facts):
    assert authorize("propose_payment", facts).verdict == "deny"


def test_unknown_action_is_denied_even_with_clean_facts():
    d = authorize("execute_payment", CLEAN)
    assert (d.verdict, d.rule_id) == ("deny", "default_deny")


# ---- the Decision contract
def test_decision_carries_a_fresh_id_and_the_policy_version():
    a, b = decide(), decide()
    assert uuid.UUID(a.decision_id) and a.decision_id != b.decision_id
    assert a.policy_version == b.policy_version == POLICY_VERSION
    assert "TEMPORARY" in POLICY_VERSION
