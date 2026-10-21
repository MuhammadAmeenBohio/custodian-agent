"""Runs and steps persistence (1.3.1). Needs the stack up and `make backend-schema` run.

Every test runs inside a transaction that is rolled back at teardown. The store wraps each write in
conn.transaction(), which is a savepoint here, so a refused change does not abort the whole test.
"""
import os
import uuid

import psycopg
import pytest
from psycopg import errors

from backend import store

pytestmark = pytest.mark.integration


@pytest.fixture
def conn():
    c = psycopg.connect(
        host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
        port=int(os.getenv("POSTGRES_PORT", "15432")),
        dbname=os.environ["POSTGRES_DB"],
        user="agent_user",
        password=os.environ["AGENT_DB_PASSWORD"],
    )
    c.execute("SELECT 1")  # open the outer transaction, so conn.transaction() in the store is a savepoint
    try:
        yield c
    finally:
        c.rollback()
        c.close()


def new_run(conn) -> dict:
    run = store.create_run(conn, "synthetic/1.png")
    assert run["run_id"], "no run_id returned, so the tests below would prove nothing"
    return run


def test_create_run_starts_queued_with_no_steps(conn):
    """Allowed case: the control for the denials below."""
    run = new_run(conn)
    assert run["status"] == "queued" and run["invoice_object_key"] == "synthetic/1.png"
    fetched = store.get_run(conn, run["run_id"])
    assert fetched["run_id"] == run["run_id"] and fetched["steps"] == []


def test_run_without_an_invoice_key_is_denied(conn):
    with pytest.raises(errors.CheckViolation):
        store.create_run(conn, "")


def test_unknown_run_is_none(conn):
    assert store.get_run(conn, uuid.uuid4()) is None


def test_normal_lifecycle_is_allowed(conn):
    run = new_run(conn)
    assert store.set_run_status(conn, run["run_id"], "running")["status"] == "running"
    done = store.set_run_status(conn, run["run_id"], "completed")
    assert done["status"] == "completed" and done["updated_at"] >= run["updated_at"]


def test_review_path_is_allowed(conn):
    run = new_run(conn)
    store.set_run_status(conn, run["run_id"], "running")
    store.set_run_status(conn, run["run_id"], "needs_review")
    assert store.set_run_status(conn, run["run_id"], "completed")["status"] == "completed"


def test_failure_records_the_error(conn):
    run = new_run(conn)
    store.set_run_status(conn, run["run_id"], "running")
    failed = store.set_run_status(conn, run["run_id"], "failed", error="extraction returned no IBAN")
    assert failed["status"] == "failed" and failed["error"] == "extraction returned no IBAN"


@pytest.mark.parametrize("path,bad", [
    ([], "completed"),                       # skipping running
    (["running", "completed"], "running"),   # reopening a finished run
    (["running", "failed"], "running"),
    (["running", "failed"], "completed"),
    ([], "bogus"),                           # not a status at all
])
def test_invalid_transition_is_denied(conn, path, bad):
    run = new_run(conn)
    for status in path:
        store.set_run_status(conn, run["run_id"], status)  # the allowed steps leading up to the bad one
    with pytest.raises(store.InvalidTransition):
        store.set_run_status(conn, run["run_id"], bad)
    assert store.get_run(conn, run["run_id"])["status"] == (path[-1] if path else "queued")  # unchanged


def test_set_status_on_unknown_run(conn):
    with pytest.raises(store.RunNotFound):
        store.set_run_status(conn, uuid.uuid4(), "running")


def test_steps_are_numbered_in_order_and_keep_input_and_output(conn):
    run = new_run(conn)
    s1 = store.start_step(conn, run["run_id"], "extract", {"invoice": "synthetic/1.png"})
    assert s1["seq"] == 1 and s1["status"] == "running" and s1["finished_at"] is None
    done = store.finish_step(conn, s1["step_id"], {"iban": "XX00"})
    assert done["status"] == "completed" and done["output"] == {"iban": "XX00"} and done["finished_at"]
    s2 = store.start_step(conn, run["run_id"], "vendor_check")
    assert s2["seq"] == 2 and s2["input"] == {}
    got = store.get_run(conn, run["run_id"])
    assert [(s["seq"], s["name"]) for s in got["steps"]] == [(1, "extract"), (2, "vendor_check")]
    assert got["steps"][0]["input"] == {"invoice": "synthetic/1.png"}


def test_step_with_an_error_is_marked_failed(conn):
    run = new_run(conn)
    s = store.start_step(conn, run["run_id"], "extract")
    assert store.finish_step(conn, s["step_id"], error="model timeout")["status"] == "failed"


def test_finished_step_cannot_be_finished_again(conn):
    run = new_run(conn)
    s = store.start_step(conn, run["run_id"], "extract")
    store.finish_step(conn, s["step_id"], {"ok": True})  # allowed the first time
    with pytest.raises(store.StepNotRunning):
        store.finish_step(conn, s["step_id"], {"ok": False})
    assert store.get_run(conn, run["run_id"])["steps"][0]["output"] == {"ok": True}  # the first result stands


def test_step_on_unknown_run_is_denied(conn):
    with pytest.raises(store.RunNotFound):
        store.start_step(conn, uuid.uuid4(), "extract")


def test_unknown_step_cannot_be_finished(conn):
    with pytest.raises(store.StepNotRunning):
        store.finish_step(conn, 999999999, {})


def test_list_runs_newest_first_and_limited(conn):
    older, newer = new_run(conn), new_run(conn)
    conn.execute("UPDATE agent.runs SET created_at = now() - interval '1 day' WHERE run_id = %s", (older["run_id"],))
    ids = [r["run_id"] for r in store.list_runs(conn, limit=1000)]
    assert ids.index(newer["run_id"]) < ids.index(older["run_id"])
    assert len(store.list_runs(conn, limit=1)) == 1


def test_step_keeps_the_decision_id_it_was_given(conn):
    run = new_run(conn)
    plain = store.start_step(conn, run["run_id"], "extract")
    assert store.finish_step(conn, plain["step_id"], {})["decision_id"] is None  # control: no decision, no id
    decided = store.start_step(conn, run["run_id"], "propose")
    did = uuid.uuid4()
    assert store.finish_step(conn, decided["step_id"], {"verdict": "allow"}, decision_id=did)["decision_id"] == did
    assert store.get_run(conn, run["run_id"])["steps"][1]["decision_id"] == did


def test_decision_id_must_be_a_uuid(conn):
    run = new_run(conn)
    s = store.start_step(conn, run["run_id"], "propose")
    with pytest.raises(errors.DataError):
        store.finish_step(conn, s["step_id"], {}, decision_id="not-a-uuid")
