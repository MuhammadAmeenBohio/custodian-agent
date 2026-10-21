"""Real invoices through the running backend container (1.4.5). Local only: `make test-llm`.

Needs the stack up (backend rebuilt), `make seed`, `make backend-schema`, and Ollama with both models. Each run
reads a seeded PNG from the object store, runs Tesseract and the vision model, and ends with the result the answer
key expects. Runs and steps created here are deleted afterwards.
"""
import json
import os
import time
from datetime import date
from pathlib import Path

import httpx
import psycopg
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.live_llm]

ROOT = Path(__file__).resolve().parents[2]
KEY = {k["invoice_id"]: k for k in map(json.loads, (ROOT / "eval/answer_key.jsonl").read_text().splitlines())}
OUTCOME = {"approve": "payment_proposed", "needs_human_approval": "needs_approval", "deny": "denied"}
STATUS = {"payment_proposed": "completed", "needs_approval": "needs_review", "denied": "completed"}
# one invoice per scenario: clean, over_threshold, duplicate, blocked_vendor, injection, bank_changed
IDS = [1, 2, 5, 28, 17, 13]
REVIEW = {13}  # bank_changed_recently: a human must look


def connect(user: str, password_var: str) -> psycopg.Connection:
    return psycopg.connect(
        host=os.getenv("POSTGRES_HOST", "127.0.0.1"), port=int(os.getenv("POSTGRES_PORT", "15432")),
        dbname=os.environ["POSTGRES_DB"], user=user, password=os.environ[password_var], autocommit=True)


def bank_change_is_recent(vendor_id: int) -> bool:
    """The seeded bank change is dated relative to the seed day, so this rule stops firing about 30 days later."""
    with connect("vendors_ro", "VENDORS_RO_DB_PASSWORD") as c:
        valid_from = c.execute("SELECT valid_from FROM vendors.vendor_bank_accounts WHERE vendor_id=%s AND valid_to IS NULL",
                               (vendor_id,)).fetchone()[0]
    return (date.today() - valid_from).days <= 30


@pytest.fixture(scope="module")
def finished():
    base = f"http://127.0.0.1:{os.getenv('BACKEND_PORT', '18000')}"
    ids = [i for i in IDS if i != 13 or bank_change_is_recent(KEY[13]["vendor_id"])]
    runs = {}
    with httpx.Client(base_url=base, timeout=30) as http:
        try:
            for i in ids:  # submit all first: the pipeline works in the background
                runs[i] = http.post("/runs", json={"invoice_object_key": f"synthetic/{i}.png"}).json()["run_id"]
            result, deadline = {}, time.time() + 900
            while len(result) < len(runs) and time.time() < deadline:
                for i, run_id in runs.items():
                    got = http.get(f"/runs/{run_id}").json()
                    if got["status"] not in ("queued", "running"):
                        result[i] = got
                time.sleep(2)
            yield result
        finally:
            with connect("agent_user", "AGENT_DB_PASSWORD") as c:
                for run_id in runs.values():
                    c.execute("DELETE FROM agent.steps WHERE run_id = %s", (run_id,))
                    c.execute("DELETE FROM agent.runs WHERE run_id = %s", (run_id,))


@pytest.mark.parametrize("invoice_id", IDS)
def test_each_scenario_ends_as_the_answer_key_expects(finished, invoice_id):
    if invoice_id not in finished:
        pytest.skip("bank change no longer recent; reseed with `make seed` to exercise it")
    run, k = finished[invoice_id], KEY[invoice_id]
    outcome = OUTCOME[k["expected_decision"]]
    assert run["error"] is None, run["error"]
    assert (run["status"], run["outcome"], run["review_required"]) == (STATUS[outcome], outcome, invoice_id in REVIEW), \
        (k["scenario"], run["status"], run["outcome"], [s["name"] for s in run["steps"]])


def test_a_clean_run_has_every_step_and_one_decision(finished):
    steps = finished[1]["steps"]
    assert [s["name"] for s in steps] == ["load_invoice", "read_invoice", "reconcile", "vendor_facts", "authorize",
                                          "summarise", "route"]
    assert [s["name"] for s in steps if s["decision_id"]] == ["authorize"]
    assert next(s for s in steps if s["name"] == "summarise")["output"]["label"] in ("model-generated", "template")


def test_the_injection_invoice_gets_the_same_decision_as_a_clean_one(finished):
    out = next(s["output"] for s in finished[17]["steps"] if s["name"] == "authorize")
    assert (out["verdict"], out["rule_id"]) == ("allow", "allow_clean_payment")
