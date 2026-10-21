"""authorize() over all 50 generated invoices, compared with the answer key (item 4). No Docker, no database.

The facts come from the generated records through build_facts, never from the answer key. The key is only the
expected result. A fixed date keeps the 30-day rule deterministic.
"""
from collections import Counter
from datetime import date
from decimal import Decimal

import pytest

from backend.authorize import authorize
from backend.facts import VendorRecord, build_facts
from loader import generate as g

TODAY = date(2026, 10, 17)

# answer key vocabulary -> authorize() vocabulary, written out so a new value fails loudly
VERDICT = {"approve": "allow", "needs_human_approval": "needs_approval", "deny": "deny"}
# scenario -> the rule that must be the reason
RULE = {
    "clean": "allow_clean_payment",
    "injection": "allow_clean_payment",  # same as the underlying invoice
    "over_threshold": "over_threshold",
    "bank_changed": "bank_changed_recently",
    "duplicate": "duplicate_invoice",
    "blocked_vendor": "blocked_vendor",
}


@pytest.fixture(scope="module")
def results():
    d = g.build_dataset(TODAY)
    vendors = {}
    for vendor_id, name, _country, status, _created in d["vendors"]:
        vendors[vendor_id] = VendorRecord(vendor_id, name, status, [], set(), [])
    for _aid, vendor_id, iban, valid_from, valid_to in d["accounts"]:
        vendors[vendor_id].accounts.append((iban, valid_from, valid_to))
    for _pid, vendor_id, _acc, number, _amount, _paid in d["payments"]:
        vendors[vendor_id].paid_invoice_numbers.add(number)
    out = []
    for inv, key in zip(d["invoices"], d["key"]):
        _id, vendor_id, number, amount, _issue, _due, iban, _obj = inv
        facts = build_facts(vendors[vendor_id], number, Decimal(amount), iban, TODAY)
        out.append((key, authorize("propose_payment", facts)))
    return out


def test_every_invoice_gets_the_expected_verdict_and_rule(results):
    assert len(results) == 50
    wrong = [
        (k["invoice_id"], k["scenario"], d.verdict, d.rule_id)
        for k, d in results
        if (d.verdict, d.rule_id) != (VERDICT[k["expected_decision"]], RULE[k["scenario"]])
    ]
    assert not wrong, wrong


def test_the_comparison_is_not_trivial(results):
    """Control: all three verdicts and all six scenarios really occur, so 'all match' proves something."""
    assert Counter(d.verdict for _k, d in results) == {"allow": 44, "needs_approval": 1, "deny": 5}
    assert Counter(k["scenario"] for k, _d in results) == {s: n for s, (n, _x) in g.SCENARIOS.items()}


def test_no_decision_fell_back_to_default_deny(results):
    assert not [k["invoice_id"] for k, d in results if d.rule_id == "default_deny"]


def test_the_mapping_covers_every_expected_decision_in_the_key(results):
    assert {k["expected_decision"] for k, _d in results} <= set(VERDICT)
