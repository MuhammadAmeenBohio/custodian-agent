"""Backend API (1.3.2): create runs and read them back with their steps.

Run locally: uv run uvicorn backend.app:app --port 18000   (docs page: /docs)
POST /runs records a queued run and, when a pipeline runner is configured (app.state.pipeline_runner), starts it in
the background. With no runner it only queues, as before; the container gets its real readers in 1.4.5.
"""
import os
from collections.abc import Callable, Iterator
from datetime import datetime
from typing import Annotated
from uuid import UUID

import psycopg
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from backend import store

load_dotenv(override=True)

app = FastAPI(title="Custodian backend", version="0.1.0")
# Called as runner(run_id) in a background task. It opens its own connection: the request's closes with the response.
app.state.pipeline_runner = None  # type: Callable[[UUID], None] | None
if os.getenv("PIPELINE_ENABLED") == "true":  # set in docker-compose.yml; tests and local imports leave it off
    from backend.deps import make_runner

    app.state.pipeline_runner = make_runner()


def get_conn() -> Iterator[psycopg.Connection]:
    """One autocommit connection per request; the store wraps each write in conn.transaction()."""
    with psycopg.connect(
        host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
        port=int(os.getenv("POSTGRES_PORT", "15432")),
        dbname=os.environ["POSTGRES_DB"],
        user="agent_user",
        password=os.environ["AGENT_DB_PASSWORD"],
        autocommit=True,
    ) as conn:
        yield conn


Conn = Annotated[psycopg.Connection, Depends(get_conn)]


class RunIn(BaseModel):
    invoice_object_key: str = Field(min_length=1, max_length=300)

    @field_validator("invoice_object_key")
    @classmethod
    def plain_key(cls, v: str) -> str:
        if v.startswith("/") or ".." in v:
            raise ValueError("object key must be relative and must not contain '..'")
        return v


class StepOut(BaseModel):
    seq: int
    name: str
    status: str
    input: dict
    output: dict | None
    error: str | None
    decision_id: UUID | None
    started_at: datetime
    finished_at: datetime | None


class RunOut(BaseModel):
    run_id: UUID
    status: str
    invoice_object_key: str
    error: str | None
    outcome: str | None
    review_required: bool
    flags: list[str]
    created_at: datetime
    updated_at: datetime


class RunDetail(RunOut):
    steps: list[StepOut]


@app.get("/health")
def health(conn: Conn) -> dict:
    conn.execute("SELECT 1")
    return {"status": "ok"}


@app.post("/runs", response_model=RunOut, status_code=201)
def create_run(body: RunIn, conn: Conn, background: BackgroundTasks):
    run = store.create_run(conn, body.invoice_object_key)
    runner: Callable[[UUID], None] | None = app.state.pipeline_runner
    if runner is not None:
        background.add_task(runner, run["run_id"])
    return run


@app.get("/runs", response_model=list[RunOut])
def list_runs(conn: Conn, limit: Annotated[int, Query(ge=1, le=200)] = 50):
    return store.list_runs(conn, limit)


@app.get("/runs/{run_id}", response_model=RunDetail)
def get_run(run_id: UUID, conn: Conn):
    run = store.get_run(conn, run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    return run
