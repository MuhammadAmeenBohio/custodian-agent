"""Ledger API (1.1.2b). Needs the stack up and `make ledger-schema` run.

Most tests give the app a connection inside an outer transaction that is rolled back at teardown
(posted entries cannot be deleted, so probe data must never be committed). Only the smoke test at the
bottom commits for real; its entry stays until the next `make ledger-schema`.
"""
import os
import uuid
from decimal import Decimal

import psycopg
import pytest
from fastapi.testclient import TestClient

from ledger.app import app, get_conn

pytestmark = pytest.mark.integration


def raw_connection() -> psycopg.Connection:
    return psycopg.connect(
        host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
        port=int(os.getenv("POSTGRES_PORT", "15432")),
        dbname=os.environ["POSTGRES_DB"],
        user="ledger_api",  # the same narrow login the real API uses
        password=os.environ["LEDGER_API_DB_PASSWORD"],
    )


@pytest.fixture
def client():
    conn = raw_connection()
    conn.execute("SELECT 1")  # open the outer transaction, so conn.transaction() in the app is a savepoint
    app.dependency_overrides[get_conn] = lambda: conn
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        conn.rollback()
        conn.close()


def payload(key=None, amount="250.00", debit=2000, credit=1000):
    return {
        "idempotency_key": key or f"api-{uuid.uuid4()}",
        "description": "test payment",
        "postings": [{"account_id": debit, "amount": amount}, {"account_id": credit, "amount": f"-{amount}"}],
    }


def balance(client, account_id) -> Decimal:
    r = client.get(f"/accounts/{account_id}/balance")
    assert r.status_code == 200
    return Decimal(r.json()["balance"])


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_list_accounts(client):
    r = client.get("/accounts")
    assert [a["account_id"] for a in r.json()] == [1000, 2000, 5000]


def test_post_balanced_entry_and_read_it_back(client):
    """Allowed case: the control for every denial below."""
    r = client.post("/entries", json=payload())
    assert r.status_code == 201, r.text
    entry = r.json()
    assert entry["entry_id"], "no entry_id, so the checks below would prove nothing"
    assert sum(Decimal(p["amount"]) for p in entry["postings"]) == 0
    assert client.get(f"/entries/{entry['entry_id']}").json() == entry


def test_balances_move_by_the_posted_amount(client):
    ap_before, cash_before = balance(client, 2000), balance(client, 1000)
    assert client.post("/entries", json=payload(amount="250.00")).status_code == 201
    assert balance(client, 2000) - ap_before == Decimal("250.00")
    assert balance(client, 1000) - cash_before == Decimal("-250.00")


def test_replaying_the_same_entry_posts_once(client):
    body = payload()
    before = balance(client, 2000)
    first = client.post("/entries", json=body)
    second = client.post("/entries", json=body)
    assert first.status_code == 201 and second.status_code == 200
    assert first.json()["entry_id"] == second.json()["entry_id"]
    assert balance(client, 2000) - before == Decimal("250.00")  # once, not twice


def test_same_key_with_different_postings_is_denied(client):
    key = f"api-{uuid.uuid4()}"
    assert client.post("/entries", json=payload(key=key, amount="10.00")).status_code == 201
    assert client.post("/entries", json=payload(key=key, amount="11.00")).status_code == 409


def test_unbalanced_entry_is_denied(client):
    body = payload()
    body["postings"][1]["amount"] = "-249.99"
    assert client.post("/entries", json=body).status_code == 422


def test_single_posting_is_denied(client):
    body = payload()
    body["postings"] = body["postings"][:1]
    assert client.post("/entries", json=body).status_code == 422


def test_zero_amount_is_denied(client):
    body = {"idempotency_key": f"api-{uuid.uuid4()}", "description": "x",
            "postings": [{"account_id": 2000, "amount": "0.00"}, {"account_id": 1000, "amount": "0.00"}]}
    assert client.post("/entries", json=body).status_code == 422


def test_three_decimal_places_are_denied(client):
    assert client.post("/entries", json=payload(amount="10.001")).status_code == 422


def test_unknown_account_is_denied_and_leaves_nothing_behind(client):
    key = f"api-{uuid.uuid4()}"
    assert client.post("/entries", json=payload(key=key, debit=9999)).status_code == 422
    # the failed attempt rolled back, so the same key can be used for a valid entry
    assert client.post("/entries", json=payload(key=key)).status_code == 201


def test_unknown_entry_and_account_return_404(client):
    assert client.get("/entries/999999999").status_code == 404
    assert client.get("/accounts/9999/balance").status_code == 404


def test_smoke_real_commit_without_overrides():
    """Uses the real get_conn, so it exercises the production path (autocommit + conn.transaction())."""
    c = TestClient(app)
    r = c.post("/entries", json=payload())
    assert r.status_code == 201, r.text
    assert c.get(f"/entries/{r.json()['entry_id']}").status_code == 200
