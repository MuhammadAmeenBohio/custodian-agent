"""The running custodian-ledger container, called over HTTP on its dev host port (1.1.3).

Needs `docker compose up -d` and `make ledger-schema`. Only the last test commits an entry (it stays until the
next `make ledger-schema`, because posted entries cannot be deleted).
"""
import os
import uuid

import httpx
import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def http():
    base = f"http://127.0.0.1:{os.getenv('LEDGER_PORT', '18001')}"
    with httpx.Client(base_url=base, timeout=5) as c:
        yield c


def test_container_is_healthy_and_serves_accounts(http):
    assert http.get("/health").json() == {"status": "ok"}
    assert [a["account_id"] for a in http.get("/accounts").json()] == [1000, 2000, 5000]


def test_container_refuses_an_unbalanced_entry(http):
    body = {"idempotency_key": "container-unbalanced", "description": "x",
            "postings": [{"account_id": 2000, "amount": "10.00"}, {"account_id": 1000, "amount": "-9.00"}]}
    assert http.post("/entries", json=body).status_code == 422
    assert http.get("/entries/999999999").status_code == 404


def test_container_posts_a_balanced_entry_end_to_end(http):
    """The one path the other container tests skip: a successful write through the running image."""
    body = {"idempotency_key": f"container-{uuid.uuid4()}", "description": "container smoke",
            "postings": [{"account_id": 2000, "amount": "12.34"}, {"account_id": 1000, "amount": "-12.34"}]}
    r = http.post("/entries", json=body)
    assert r.status_code == 201, r.text
    assert r.json()["entry_id"], "no entry_id returned"
    assert http.get(f"/entries/{r.json()['entry_id']}").json() == r.json()
