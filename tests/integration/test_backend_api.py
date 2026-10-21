"""Backend API (1.3.2). Needs the stack up and `make backend-schema` run.

Most tests hand the app a connection inside an outer transaction that is rolled back at teardown. The two
tests that commit for real delete their own run afterwards (runs, unlike ledger entries, can be deleted).
"""
import os
import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient

from backend import store
from backend.app import app, get_conn

pytestmark = pytest.mark.integration


def connect(autocommit: bool = False) -> psycopg.Connection:
    return psycopg.connect(
        host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
        port=int(os.getenv("POSTGRES_PORT", "15432")),
        dbname=os.environ["POSTGRES_DB"],
        user="agent_user",
        password=os.environ["AGENT_DB_PASSWORD"],
        autocommit=autocommit,
    )


@pytest.fixture
def api():
    conn = connect()
    conn.execute("SELECT 1")  # open the outer transaction, so conn.transaction() in the store is a savepoint
    app.dependency_overrides[get_conn] = lambda: conn
    try:
        yield TestClient(app), conn
    finally:
        app.dependency_overrides.clear()
        conn.rollback()
        conn.close()


def test_health(api):
    client, _ = api
    assert client.get("/health").json() == {"status": "ok"}


def test_create_and_read_back_a_run(api):
    """Allowed case: the control for the denials below."""
    client, _ = api
    r = client.post("/runs", json={"invoice_object_key": "synthetic/1.png"})
    assert r.status_code == 201, r.text
    run = r.json()
    assert run["run_id"] and run["status"] == "queued" and run["invoice_object_key"] == "synthetic/1.png"
    got = client.get(f"/runs/{run['run_id']}")
    assert got.status_code == 200
    assert got.json() == {**run, "steps": []}


def test_list_runs_has_the_new_run(api):
    client, _ = api
    run_id = client.post("/runs", json={"invoice_object_key": "synthetic/2.png"}).json()["run_id"]
    assert run_id in [r["run_id"] for r in client.get("/runs?limit=200").json()]
    assert len(client.get("/runs?limit=1").json()) == 1


@pytest.mark.parametrize("limit", [0, 201, -1, "abc"])
def test_bad_limit_is_denied(api, limit):
    client, _ = api
    assert client.get(f"/runs?limit={limit}").status_code == 422


@pytest.mark.parametrize("key", ["", "/etc/passwd", "../secrets.png", "synthetic/../x.png", "x" * 301])
def test_bad_object_key_is_denied(api, key):
    client, _ = api
    assert client.post("/runs", json={"invoice_object_key": key}).status_code == 422


def test_missing_body_is_denied(api):
    client, _ = api
    assert client.post("/runs", json={}).status_code == 422


def test_unknown_run_is_404_and_malformed_id_is_422(api):
    client, _ = api
    assert client.get(f"/runs/{uuid.uuid4()}").status_code == 404
    assert client.get("/runs/not-a-uuid").status_code == 422


def test_steps_appear_in_order_with_input_and_output(api):
    client, conn = api
    run_id = client.post("/runs", json={"invoice_object_key": "synthetic/3.png"}).json()["run_id"]
    first = store.start_step(conn, uuid.UUID(run_id), "extract", {"reader": "ocr"})
    store.finish_step(conn, first["step_id"], {"iban": "XX00"})
    store.start_step(conn, uuid.UUID(run_id), "vendor_check")
    steps = client.get(f"/runs/{run_id}").json()["steps"]
    assert [(s["seq"], s["name"], s["status"]) for s in steps] == [(1, "extract", "completed"), (2, "vendor_check", "running")]
    assert steps[0]["input"] == {"reader": "ocr"} and steps[0]["output"] == {"iban": "XX00"}
    assert steps[1]["output"] is None and steps[1]["finished_at"] is None


def test_smoke_real_commit_without_overrides():
    """Uses the real get_conn (autocommit + conn.transaction()). Deletes its own run afterwards."""
    client = TestClient(app)
    r = client.post("/runs", json={"invoice_object_key": "synthetic/smoke.png"})
    assert r.status_code == 201, r.text
    run_id = r.json()["run_id"]
    try:
        assert client.get(f"/runs/{run_id}").status_code == 200
    finally:
        with connect(autocommit=True) as c:
            c.execute("DELETE FROM agent.runs WHERE run_id = %s", (run_id,))
