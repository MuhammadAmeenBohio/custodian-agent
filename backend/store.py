"""Persistence for runs and steps (1.3.1). Plain functions over a psycopg connection.

Each write wraps itself in conn.transaction(): on an autocommit connection that is a real transaction, and inside
a caller's transaction (a test, or a larger unit of work) it is a savepoint, so a refused change rolls back alone.
Rules live in the database (schema.sql); this module turns database errors into named exceptions.
"""
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


class RunNotFound(Exception):
    pass


class InvalidTransition(Exception):
    pass


class StepNotRunning(Exception):
    pass


def _one(conn: psycopg.Connection, sql: str, params: tuple) -> dict | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def create_run(conn: psycopg.Connection, invoice_object_key: str) -> dict:
    with conn.transaction():
        return _one(conn, "INSERT INTO agent.runs (invoice_object_key) VALUES (%s) RETURNING *", (invoice_object_key,))


def get_run(conn: psycopg.Connection, run_id: UUID) -> dict | None:
    """The run with its steps in order, or None."""
    run = _one(conn, "SELECT * FROM agent.runs WHERE run_id = %s", (run_id,))
    if run is None:
        return None
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM agent.steps WHERE run_id = %s ORDER BY seq", (run_id,))
        run["steps"] = cur.fetchall()
    return run


def list_runs(conn: psycopg.Connection, limit: int = 50) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM agent.runs ORDER BY created_at DESC, run_id LIMIT %s", (limit,))
        return cur.fetchall()


def set_run_status(conn: psycopg.Connection, run_id: UUID, status: str, error: str | None = None) -> dict:
    try:
        with conn.transaction():
            row = _one(
                conn,
                "UPDATE agent.runs SET status = %s, error = %s, updated_at = now() WHERE run_id = %s RETURNING *",
                (status, error, run_id),
            )
    except psycopg.errors.CheckViolation as e:  # the state machine trigger, or an unknown status value
        raise InvalidTransition(e.diag.message_primary) from None
    if row is None:
        raise RunNotFound(str(run_id))
    return row


def finish_run(
    conn: psycopg.Connection,
    run_id: UUID,
    status: str,
    outcome: str,
    review_required: bool,
    flags: list[str] | None = None,
) -> dict:
    """End a run in one write: status (through the state machine), outcome, review flag and soft flags."""
    try:
        with conn.transaction():
            row = _one(
                conn,
                "UPDATE agent.runs SET status = %s, outcome = %s, review_required = %s, flags = %s, updated_at = now()"
                " WHERE run_id = %s RETURNING *",
                (status, outcome, review_required, Jsonb(flags or []), run_id),
            )
    except psycopg.errors.CheckViolation as e:  # the state machine trigger, or an unknown status or outcome
        raise InvalidTransition(e.diag.message_primary) from None
    if row is None:
        raise RunNotFound(str(run_id))
    return row


def set_trace_id(conn: psycopg.Connection, run_id: UUID, trace_id: str) -> None:
    """Record the tracer's id for this run (backend/tracing.py), so a run can be followed to its trace."""
    with conn.transaction():
        conn.execute("UPDATE agent.runs SET trace_id = %s WHERE run_id = %s", (trace_id, run_id))


def start_step(conn: psycopg.Connection, run_id: UUID, name: str, input: dict | None = None) -> dict:
    with conn.transaction():
        # Lock the run row so two writers cannot pick the same sequence number.
        if _one(conn, "SELECT run_id FROM agent.runs WHERE run_id = %s FOR UPDATE", (run_id,)) is None:
            raise RunNotFound(str(run_id))
        return _one(
            conn,
            "INSERT INTO agent.steps (run_id, seq, name, status, input)"
            " VALUES (%s, (SELECT COALESCE(max(seq), 0) + 1 FROM agent.steps WHERE run_id = %s), %s, 'running', %s)"
            " RETURNING *",
            (run_id, run_id, name, Jsonb(input or {})),
        )


def finish_step(
    conn: psycopg.Connection,
    step_id: int,
    output: dict | None = None,
    error: str | None = None,
    decision_id: UUID | str | None = None,
    input: dict | None = None,
) -> dict:
    """Mark a running step completed (or failed, when error is given). A finished step cannot be changed.

    decision_id is the id of the authorize() decision made in this step, kept so a step can be tied to its decision.
    input, when given, replaces the input recorded at start (a node only knows what it read once it has run).
    """
    with conn.transaction():
        row = _one(
            conn,
            "UPDATE agent.steps SET status = %s, output = %s, error = %s, decision_id = %s, finished_at = now(),"
            " input = COALESCE(%s, input) WHERE step_id = %s AND status = 'running' RETURNING *",
            ("failed" if error else "completed", Jsonb(output) if output is not None else None, error,
             decision_id, Jsonb(input) if input is not None else None, step_id),
        )
    if row is None:
        raise StepNotRunning(str(step_id))
    return row
