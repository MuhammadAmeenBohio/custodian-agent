"""authorize() over the 50 seeded invoices through the real SQL loader, vs the committed answer key (item 4).

Needs the stack up and `make seed`. Facts come from the live vendors schema (as vendors_ro), never from the key.
The as-of date is derived from the seed: vendor 5's account changed 10 days before the seed date.
"""
import json
import os
from datetime import date, timedelta
from pathlib import Path

import psycopg
import pytest

from backend.authorize import authorize
from backend.facts import build_facts, find_vendor_id, load_vendor

pytestmark = pytest.mark.integration

KEY = Path(__file__).resolve().parent.parent.parent / "eval" / "answer_key.jsonl"
VERDICT = {"approve": "allow", "needs_human_approval": "needs_approval", "deny": "deny"}
RULE = {"clean": "allow_clean_payment", "injection": "allow_clean_payment", "over_threshold": "over_threshold",
        "bank_changed": "bank_changed_recently", "duplicate": "duplicate_invoice", "blocked_vendor": "blocked_vendor"}


@pytest.fixture(scope="module")
def conn():
    c = psycopg.connect(
        host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
        port=int(os.getenv("POSTGRES_PORT", "15432")),
        dbname=os.environ["POSTGRES_DB"],
        user="vendors_ro",
        password=os.environ["VENDORS_RO_DB_PASSWORD"],
    )
    yield c
    c.close()


def test_live_authorize_matches_the_answer_key(conn):
    seed_day = conn.execute(
        "SELECT valid_from FROM vendors.vendor_bank_accounts WHERE vendor_id = 5 AND valid_to IS NULL"
    ).fetchone()[0] + timedelta(days=10)
    keys = [json.loads(x) for x in KEY.read_text().splitlines()]
    rows = {r[0]: r for r in conn.execute("SELECT invoice_id, vendor_id, invoice_number, amount, iban_on_invoice "
                                          "FROM vendors.invoices").fetchall()}
    assert len(keys) == len(rows) == 50
    wrong = []
    for k in keys:
        _id, vendor_id, number, amount, iban = rows[k["invoice_id"]]
        facts = build_facts(load_vendor(conn, vendor_id), number, amount, iban, seed_day)
        d = authorize("propose_payment", facts)
        if (d.verdict, d.rule_id) != (VERDICT[k["expected_decision"]], RULE[k["scenario"]]):
            wrong.append((k["invoice_id"], k["scenario"], d.verdict, d.rule_id))
    assert not wrong, wrong


def test_unknown_vendor_name_is_denied_by_default(conn):
    assert find_vendor_id(conn, "No Such Vendor Ltd") is None  # control: lookup really finds nothing
    d = authorize("propose_payment", build_facts(None, "INV-X", 1, "XX00", date.today()))
    assert (d.verdict, d.rule_id) == ("deny", "default_deny")


def test_vendor_loader_reads_the_real_data(conn):
    v = load_vendor(conn, 5)
    assert v and v.status == "active" and len(v.accounts) == 2  # vendor 5 has history: old closed + new current
    assert v.current_account() is not None and v.paid_invoice_numbers
    assert load_vendor(conn, 9999) is None
