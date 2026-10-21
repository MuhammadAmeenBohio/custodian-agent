"""POST /runs starts the pipeline in the background when a runner is configured (1.4.4). Needs the stack up.

TestClient runs background tasks before it returns, so the run is finished when the POST comes back.
"""
import os
from datetime import date, timedelta

import psycopg
import pytest
from fastapi.testclient import TestClient

from backend.app import app, get_conn
from backend.facts import VendorRecord
from backend.pipeline import Deps, run_pipeline
from backend.summary import LABEL_MODEL, Summary
from shared.iban import make_fake_iban

pytestmark = pytest.mark.integration

TODAY = date(2026, 10, 21)
IBAN = make_fake_iban(11)
NAME, NUMBER = "Garcia-James", "INV-009-1013"
VENDOR = VendorRecord(9, NAME, "active", [(IBAN, TODAY - timedelta(days=400), None)], set(), [])
DEPS = Deps(
    load_png=lambda key: b"png",
    ocr=lambda png: f"INVOICE {NUMBER}\nTOTAL DUE: USD 1,000.00\nPay to IBAN: {IBAN}\n",
    vision=lambda png: {"vendor_name": NAME, "invoice_number": NUMBER, "total_amount": "1000.00", "iban": IBAN},
    vendor_by_name=lambda name: VENDOR if name == NAME else None,
    summarise=lambda data: Summary("note", LABEL_MODEL),
    today=lambda: TODAY,
)


@pytest.fixture
def api():
    conn = psycopg.connect(
        host=os.getenv("POSTGRES_HOST", "127.0.0.1"), port=int(os.getenv("POSTGRES_PORT", "15432")),
        dbname=os.environ["POSTGRES_DB"], user="agent_user", password=os.environ["AGENT_DB_PASSWORD"])
    conn.execute("SELECT 1")
    app.dependency_overrides[get_conn] = lambda: conn
    try:
        yield TestClient(app), conn
    finally:
        app.state.pipeline_runner = None
        app.dependency_overrides.clear()
        conn.rollback()
        conn.close()


def test_without_a_runner_a_run_only_queues(api):
    client, _ = api
    run = client.post("/runs", json={"invoice_object_key": "synthetic/1.png"}).json()
    got = client.get(f"/runs/{run['run_id']}").json()
    assert got["status"] == "queued" and got["steps"] == [] and got["outcome"] is None
    assert got["review_required"] is False and got["flags"] == []


def test_with_a_runner_the_run_is_executed_in_the_background(api):
    client, conn = api
    app.state.pipeline_runner = lambda run_id: run_pipeline(conn, run_id, DEPS)
    created = client.post("/runs", json={"invoice_object_key": "synthetic/1.png"})
    assert created.status_code == 201 and created.json()["status"] == "queued"  # the reply is the queued run
    got = client.get(f"/runs/{created.json()['run_id']}").json()
    assert (got["status"], got["outcome"], got["review_required"]) == ("completed", "payment_proposed", False)
    assert [s["name"] for s in got["steps"]][-1] == "route" and len(got["steps"]) == 7


def test_a_runner_that_fails_does_not_break_the_post(api):
    client, conn = api
    broken = Deps(**{**DEPS.__dict__, "load_png": lambda key: (_ for _ in ()).throw(OSError("object store down"))})
    app.state.pipeline_runner = lambda run_id: run_pipeline(conn, run_id, broken)
    created = client.post("/runs", json={"invoice_object_key": "synthetic/1.png"})
    assert created.status_code == 201
    got = client.get(f"/runs/{created.json()['run_id']}").json()
    assert got["status"] == "failed" and "OSError" in got["error"]
