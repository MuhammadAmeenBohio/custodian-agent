"""Tracing around the pipeline graph (backend/tracing.py) on a real database with fake readers.
Needs the stack up and the trace_id column from `make backend-schema`.

The graph nodes are untouched: the only change at the call is the callback list passed to invoke.
"""
import os
import socket
from datetime import date, timedelta

import psycopg
import pytest
from langchain_core.callbacks import BaseCallbackHandler

from backend import store, tracing
from backend.facts import VendorRecord
from backend.pipeline import Deps, run_pipeline
from backend.summary import LABEL_MODEL, Summary
from backend.tracing import Trace
from shared.iban import make_fake_iban

pytestmark = pytest.mark.integration

TODAY = date(2026, 10, 21)
IBAN = make_fake_iban(11)
NAME, NUMBER = "Garcia-James", "INV-009-1013"
ON = {"LANGFUSE_BASE_URL": "http://127.0.0.1:13001", "LANGFUSE_PUBLIC_KEY": "pk-lf-fake", "LANGFUSE_SECRET_KEY": "sk-lf-fake"}


@pytest.fixture
def conn():
    c = psycopg.connect(
        host=os.getenv("POSTGRES_HOST", "127.0.0.1"), port=int(os.getenv("POSTGRES_PORT", "15432")),
        dbname=os.environ["POSTGRES_DB"], user="agent_user", password=os.environ["AGENT_DB_PASSWORD"])
    c.execute("SELECT 1")  # open the outer transaction, so the store's conn.transaction() is a savepoint
    try:
        yield c
    finally:
        c.rollback()
        c.close()


@pytest.fixture(autouse=True)
def tracing_off(monkeypatch):
    for name in ("LANGFUSE_BASE_URL", "LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)


def deps() -> Deps:
    vision = {"vendor_name": NAME, "invoice_number": NUMBER, "total_amount": "1000.00", "iban": IBAN}
    return Deps(
        load_png=lambda key: b"png-bytes",
        ocr=lambda png: f"INVOICE {NUMBER}\nTOTAL DUE: USD 1,000.00\nPay to IBAN: {IBAN}\n",
        vision=lambda png: vision,
        vendor_by_name=lambda name: VendorRecord(9, NAME, "active", [(IBAN, TODAY - timedelta(days=400), None)], set(), [])
        if name == NAME else None,
        summarise=lambda data: Summary("A short note for the reviewer.", LABEL_MODEL),
        today=lambda: TODAY,
    )


def execute(conn) -> dict:
    run = store.create_run(conn, "synthetic/1.png")
    run_pipeline(conn, run["run_id"], deps())
    return store.get_run(conn, run["run_id"])


class Recorder(BaseCallbackHandler):
    """Stands in for the Langfuse handler: records that the graph really called the callbacks it was given."""

    def __init__(self):
        self.chains = []

    def on_chain_start(self, serialized, inputs, **kwargs):
        self.chains.append(kwargs.get("name"))


def test_tracing_off_the_run_completes_with_no_trace_id(conn):
    run = execute(conn)
    assert (run["status"], run["outcome"]) == ("completed", "payment_proposed")
    assert run["trace_id"] is None


def test_tracing_on_the_callbacks_see_the_graph_and_the_trace_id_is_stored(conn, monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(tracing, "_build", lambda host, pk, sk, tid: Trace([rec], tid))
    for k, v in ON.items():
        monkeypatch.setenv(k, v)
    run = execute(conn)
    assert run["status"] == "completed"
    assert run["trace_id"] == run["run_id"].hex
    assert "read_invoice" in rec.chains and "route" in rec.chains  # the nodes ran under the callback


def test_a_dead_local_host_the_run_still_completes_and_warns(conn, monkeypatch, caplog):
    for k, v in {**ON, "LANGFUSE_BASE_URL": "http://127.0.0.1:1"}.items():
        monkeypatch.setenv(k, v)
    with caplog.at_level("WARNING", logger="backend.tracing"):
        run = execute(conn)
    assert (run["status"], run["outcome"]) == ("completed", "payment_proposed")
    assert "tracing:" in caplog.text  # either "could not start" (package absent) or the flush warning (package present)


def test_a_failing_tracer_does_not_fail_the_run(conn, monkeypatch, caplog):
    def boom(*a):
        raise RuntimeError("tracer down")

    monkeypatch.setattr(tracing, "_build", boom)
    for k, v in ON.items():
        monkeypatch.setenv(k, v)
    with caplog.at_level("WARNING", logger="backend.tracing"):
        run = execute(conn)
    assert run["status"] == "completed" and run["trace_id"] is None
    assert "could not start" in caplog.text


def test_a_failing_trace_id_write_does_not_fail_the_run(conn, monkeypatch):
    monkeypatch.setattr(tracing, "_build", lambda host, pk, sk, tid: Trace([Recorder()], tid))
    monkeypatch.setattr(store, "set_trace_id", lambda *a: (_ for _ in ()).throw(RuntimeError("no column")))
    for k, v in ON.items():
        monkeypatch.setenv(k, v)
    assert execute(conn)["status"] == "completed"


def test_a_non_local_host_never_enables_tracing(conn, monkeypatch):
    monkeypatch.setattr(tracing, "_build", lambda *a: pytest.fail("a hosted tracer must never be built"))
    for k, v in {**ON, "LANGFUSE_BASE_URL": "https://cloud.langfuse.com"}.items():
        monkeypatch.setenv(k, v)
    run = execute(conn)
    assert run["status"] == "completed" and run["trace_id"] is None


def test_the_trace_id_column_only_takes_32_lowercase_hex(conn):
    run = store.create_run(conn, "synthetic/1.png")
    with pytest.raises(psycopg.errors.CheckViolation):
        store.set_trace_id(conn, run["run_id"], "not-a-trace-id")


# ---- the guard: a normal run talks to nobody outside the machine / custodian-net ----

def test_a_normal_run_makes_no_outbound_connection(conn, monkeypatch):
    """With the readers faked, nothing in a run may open a socket to anywhere: not LangSmith, not LangChain, not
    Langfuse. (The Postgres connection is opened by libpq before the guard starts.) LangSmith must also report
    itself disabled, because it ships with langgraph and would send invoice text to a hosted service."""
    attempts = []
    real_connect, real_create = socket.socket.connect, socket.create_connection

    def spy_connect(self, address, *a, **k):
        attempts.append(address)
        raise ConnectionRefusedError("outbound connection blocked by the test")

    def spy_create(address, *a, **k):
        attempts.append(address)
        raise ConnectionRefusedError("outbound connection blocked by the test")

    monkeypatch.setattr(socket.socket, "connect", spy_connect)
    monkeypatch.setattr(socket, "create_connection", spy_create)
    try:
        run = execute(conn)
    finally:
        monkeypatch.setattr(socket.socket, "connect", real_connect)
        monkeypatch.setattr(socket, "create_connection", real_create)

    assert run["status"] == "completed"
    assert attempts == [], f"unexpected outbound connections: {attempts}"

    import langsmith.utils as ls
    assert os.environ.get("LANGSMITH_TRACING") == "false" and os.environ.get("LANGCHAIN_TRACING_V2") == "false"
    assert ls.tracing_is_enabled() is False
