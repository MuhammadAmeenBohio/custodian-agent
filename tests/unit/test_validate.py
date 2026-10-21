"""Bad-data tests for the Great Expectations checks (0.3.7). No Docker: frames are built in memory.

Control first: the clean dataset passes all 8 checks. Then each corruption must fail exactly
the check designed to catch it, and no other. That proves the checks can actually fail.
"""
from datetime import date, timedelta

import pandas as pd
import pytest

from loader import generate as g
from loader import validate as v

TODAY = date(2026, 10, 17)


def clean_frames() -> dict:
    d = g.build_dataset(TODAY)
    invoices = pd.DataFrame(
        d["invoices"],
        columns=["invoice_id", "vendor_id", "invoice_number", "amount", "issue_date", "due_date",
                 "iban_on_invoice", "object_key"],
    )
    invoices["object_key"] = invoices["invoice_id"].map(lambda i: f"synthetic/{i}.png")
    return {
        "vendors": pd.DataFrame(d["vendors"], columns=["vendor_id", "name", "country", "status", "created_at"]),
        "accounts": pd.DataFrame(d["accounts"], columns=["account_id", "vendor_id", "iban", "valid_from", "valid_to"]),
        "payments": pd.DataFrame(d["payments"], columns=["payment_id", "vendor_id", "account_id",
                                                         "invoice_number", "amount", "paid_at"]),
        "invoices": invoices,
        "key": pd.DataFrame(d["key"]),
    }


def run(frames, key_exists=lambda k: True) -> dict[int, bool]:
    """Returns {check number: passed}."""
    return {int(name.split()[0]): ok for name, ok in v.run_checks(frames, key_exists)}


def test_clean_dataset_passes_all_eight_checks():
    results = run(clean_frames())
    assert sorted(results) == [1, 2, 3, 4, 5, 6, 7, 8]
    assert all(results.values()), results


# Each mutation edits a fresh copy of the clean frames.
def orphan_invoice(f):
    f["invoices"].loc[0, "vendor_id"] = 999


def negative_amount(f):
    f["invoices"] = f["invoices"].astype({"amount": object})
    f["invoices"].loc[0, "amount"] = -5


def second_current_account(f):
    row = f["accounts"].iloc[0].copy()
    row["account_id"] = 9001
    row["iban"] = "XX00FAKE9999999001"
    f["accounts"] = pd.concat([f["accounts"], row.to_frame().T], ignore_index=True)


def overlapping_accounts(f):
    vendor1 = f["vendors"].loc[f["vendors"]["vendor_id"] == 1, "created_at"].iloc[0]
    row = {"account_id": 9002, "vendor_id": 1, "iban": "XX00FAKE9999999002",
           "valid_from": vendor1 + timedelta(days=5), "valid_to": vendor1 + timedelta(days=10)}
    f["accounts"] = pd.concat([f["accounts"], pd.DataFrame([row])], ignore_index=True)


def real_looking_iban(f):
    f["invoices"].loc[0, "iban_on_invoice"] = "GB00REAL0000000001"


def extra_clean_scenario(f):
    f["key"].loc[0, "scenario"] = "clean" if f["key"].loc[0, "scenario"] != "clean" else "injection"


def key_mismatch(f):
    f["key"].loc[0, "invoice_number"] = "INV-TAMPERED"


def missing_key_row(f):
    f["key"] = f["key"].iloc[1:].reset_index(drop=True)


# Some corruptions legitimately break two rules. A second current account also overlaps the first;
# an invoice for vendor 999 or with a bad IBAN no longer matches the answer key; a missing key row
# also shifts the scenario count. Each case lists the exact set of checks that must fail.
CASES = [
    ("orphan_invoice", orphan_invoice, [1, 8]),
    ("negative_amount", negative_amount, [2]),
    ("second_current_account", second_current_account, [3, 4]),
    ("overlapping_accounts", overlapping_accounts, [4]),
    ("real_looking_iban", real_looking_iban, [5, 8]),
    ("extra_clean_scenario", extra_clean_scenario, [7]),
    ("key_mismatch", key_mismatch, [8]),
    ("missing_key_row", missing_key_row, [7, 8]),
]


@pytest.mark.parametrize("name,mutate,expected_failures", CASES, ids=[c[0] for c in CASES])
def test_corruption_fails_exactly_its_checks(name, mutate, expected_failures):
    frames = clean_frames()
    mutate(frames)
    failed = sorted(n for n, ok in run(frames).items() if not ok)
    assert failed == expected_failures, f"{name}: expected {expected_failures}, got {failed}"


def test_missing_object_in_bucket_fails_check_6_only():
    missing = "synthetic/7.png"
    results = run(clean_frames(), key_exists=lambda k: k != missing)
    assert [n for n, ok in results.items() if not ok] == [6]


def test_empty_object_key_fails_check_6():
    frames = clean_frames()
    frames["invoices"] = frames["invoices"].astype({"object_key": object})
    frames["invoices"].loc[0, "object_key"] = None
    assert run(frames)[6] is False


def test_empty_source_never_passes():
    frames = clean_frames()
    frames["payments"] = frames["payments"].iloc[0:0]
    frames["invoices"] = frames["invoices"].iloc[0:0]
    results = run(frames)
    assert results[1] is False and results[2] is False
