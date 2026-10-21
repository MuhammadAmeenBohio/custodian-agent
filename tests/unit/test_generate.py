"""Unit tests for the synthetic data generator. No Docker, no database."""
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal

import pytest

from loader import generate as g
from shared.iban import is_valid

TODAY = date(2026, 10, 17)


@pytest.fixture(scope="module")
def data():
    return g.build_dataset(TODAY)


def test_same_seed_same_data():
    """Determinism: two builds on the same day are identical."""
    assert g.build_dataset(TODAY) == g.build_dataset(TODAY)


def test_vendor_counts_and_blocked(data):
    assert len(data["vendors"]) == 40
    blocked = {v[0] for v in data["vendors"] if v[3] == "blocked"}
    assert blocked == {38, 39, 40}


def test_vendor_5_changed_ten_days_ago_and_only_vendor_5(data):
    recent = [a for a in data["accounts"] if a[4] is None and a[3] >= TODAY - timedelta(days=30)]
    assert [a[1] for a in recent] == [5]
    assert recent[0][3] == TODAY - timedelta(days=10)


def test_every_vendor_has_exactly_one_current_account(data):
    current = Counter(a[1] for a in data["accounts"] if a[4] is None)
    assert len(current) == 40
    assert set(current.values()) == {1}


def test_all_ibans_are_fake(data):
    ibans = [a[2] for a in data["accounts"]]
    assert ibans and all(i.startswith("XX") for i in ibans)


def test_all_ibans_pass_the_checksum(data):
    ibans = [a[2] for a in data["accounts"]]
    assert ibans and all(is_valid(i) for i in ibans)
    assert all(is_valid(i["iban"]) for i in data["key"])


def test_payments_use_the_account_valid_on_the_day(data):
    accounts = {a[0]: a for a in data["accounts"]}
    assert data["payments"], "no payments generated, so this test would prove nothing"
    for _pid, vendor_id, account_id, _num, _amt, paid_at in data["payments"]:
        acc = accounts[account_id]
        assert acc[1] == vendor_id
        assert acc[3] <= paid_at and (acc[4] is None or paid_at < acc[4])


def test_payments_per_vendor_between_6_and_12(data):
    per_vendor = Counter(p[1] for p in data["payments"])
    assert len(per_vendor) == 40
    assert all(6 <= n <= 12 for n in per_vendor.values())


def test_scenario_counts_match_the_plan(data):
    counts = Counter(k["scenario"] for k in data["key"])
    assert counts == {s: n for s, (n, _d) in g.SCENARIOS.items()}
    assert len(data["invoices"]) == 50


def test_answer_key_matches_invoices_one_to_one(data):
    assert [i[0] for i in data["invoices"]] == [k["invoice_id"] for k in data["key"]]
    for inv, key in zip(data["invoices"], data["key"]):
        assert inv[1] == key["vendor_id"]
        assert inv[2] == key["invoice_number"]
        assert inv[6] == key["iban"]


def _by_scenario(data, name):
    return [(i, k) for i, k in zip(data["invoices"], data["key"]) if k["scenario"] == name]


def test_duplicate_really_matches_a_historical_payment(data):
    (inv, _key), = _by_scenario(data, "duplicate")
    assert any(p[1] == inv[1] and p[3] == inv[2] for p in data["payments"])


def test_clean_invoices_do_not_match_any_payment(data):
    """Control for the test above: only the duplicate may collide with history."""
    history = {(p[1], p[3]) for p in data["payments"]}
    clean = _by_scenario(data, "clean") + _by_scenario(data, "injection")
    assert len(clean) == 44
    assert all((i[1], i[2]) not in history for i, _k in clean)


def test_bank_changed_is_vendor_5_with_the_new_iban(data):
    (inv, _key), = _by_scenario(data, "bank_changed")
    assert inv[1] == 5
    new_iban = next(a[2] for a in data["accounts"] if a[1] == 5 and a[4] is None)
    old_iban = next(a[2] for a in data["accounts"] if a[1] == 5 and a[4] is not None)
    assert inv[6] == new_iban != old_iban


def test_blocked_invoices_are_blocked_vendors_and_one_each(data):
    vendors = sorted(i[1] for i, _k in _by_scenario(data, "blocked_vendor"))
    assert vendors == [38, 39, 40]


def test_amounts_around_the_threshold(data):
    over = _by_scenario(data, "over_threshold")
    assert len(over) == 1 and over[0][0][3] > g.APPROVAL_THRESHOLD
    others = [i for i in data["invoices"] if i[0] != over[0][0][0]]
    assert others and all(Decimal(0) < i[3] < g.APPROVAL_THRESHOLD for i in others)


def test_expected_decisions(data):
    decisions = Counter(k["expected_decision"] for k in data["key"])
    assert decisions == {"approve": 44, "needs_human_approval": 1, "deny": 5}


def test_normal_invoices_never_use_vendor_5_or_blocked_vendors(data):
    normal = _by_scenario(data, "clean") + _by_scenario(data, "injection") + _by_scenario(data, "over_threshold")
    assert normal
    assert all(i[1] not in {5, 38, 39, 40} for i, _k in normal)
