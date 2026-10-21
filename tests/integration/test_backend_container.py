"""The running custodian-backend container, called over HTTP on its dev host port (1.3.2).

Needs `docker compose up -d` and `make backend-schema`. The write test deletes its own run afterwards.
"""
import os
import time

import httpx
import psycopg
import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def http():
    base = f"http://127.0.0.1:{os.getenv('BACKEND_PORT', '18000')}"
    with httpx.Client(base_url=base, timeout=5) as c:
        yield c


def test_container_is_healthy(http):
    assert http.get("/health").json() == {"status": "ok"}


def test_container_refuses_a_bad_object_key(http):
    assert http.post("/runs", json={"invoice_object_key": "../secrets.png"}).status_code == 422


def test_container_creates_and_reads_a_run_end_to_end(http):
    r = http.post("/runs", json={"invoice_object_key": "synthetic/container-smoke.png"})
    assert r.status_code == 201, r.text
    run_id = r.json()["run_id"]
    try:
        # The pipeline starts at once (1.4.5), so the status is no longer fixed. This key does not exist, so the
        # run ends failed; what matters here is that the run is created and can be read back.
        got = http.get(f"/runs/{run_id}").json()
        assert got["run_id"] == run_id and got["status"] in ("queued", "running", "failed")
        for _ in range(50):
            if http.get(f"/runs/{run_id}").json()["status"] != "queued" and                     http.get(f"/runs/{run_id}").json()["status"] != "running":
                break
            time.sleep(0.2)
    finally:
        with psycopg.connect(
            host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
            port=int(os.getenv("POSTGRES_PORT", "15432")),
            dbname=os.environ["POSTGRES_DB"],
            user="agent_user",
            password=os.environ["AGENT_DB_PASSWORD"],
            autocommit=True,
        ) as c:
            c.execute("DELETE FROM agent.steps WHERE run_id = %s", (run_id,))
            c.execute("DELETE FROM agent.runs WHERE run_id = %s", (run_id,))
