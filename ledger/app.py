"""Ledger API (1.1.2b): post balanced journal entries, read entries and balances.

Run locally: uv run uvicorn ledger.app:app --port 18001   (docs page: /docs)
Amounts are Decimal and travel as JSON strings, never floats.
"""
import os
from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

import psycopg
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Response
from pydantic import BaseModel, Field, field_validator, model_validator

load_dotenv(override=True)

app = FastAPI(title="Custodian ledger", version="0.1.0")


def get_conn() -> Iterator[psycopg.Connection]:
    """One autocommit connection per request; each write wraps itself in conn.transaction()."""
    with psycopg.connect(
        host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
        port=int(os.getenv("POSTGRES_PORT", "15432")),
        dbname=os.environ["POSTGRES_DB"],
        user="ledger_api",  # INSERT and SELECT only; the owner (ledger_user) is used just to apply the schema
        password=os.environ["LEDGER_API_DB_PASSWORD"],
        autocommit=True,
    ) as conn:
        yield conn


Conn = Annotated[psycopg.Connection, Depends(get_conn)]


class PostingIn(BaseModel):
    account_id: int
    amount: Decimal = Field(max_digits=12, decimal_places=2, description="debit positive, credit negative")

    @field_validator("amount")
    @classmethod
    def not_zero(cls, v: Decimal) -> Decimal:
        if v == 0:
            raise ValueError("amount must not be zero")
        return v


class EntryIn(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=500)
    postings: list[PostingIn] = Field(min_length=2)

    @model_validator(mode="after")
    def balanced(self) -> "EntryIn":
        total = sum(p.amount for p in self.postings)
        if total != 0:
            raise ValueError(f"postings must sum to zero, got {total}")
        return self


class PostingOut(BaseModel):
    account_id: int
    amount: Decimal


class EntryOut(BaseModel):
    entry_id: int
    idempotency_key: str
    description: str
    created_at: datetime
    postings: list[PostingOut]


class AccountOut(BaseModel):
    account_id: int
    name: str
    type: str


class BalanceOut(AccountOut):
    balance: Decimal


def fetch_entry(conn: psycopg.Connection, by: Literal["entry_id", "idempotency_key"], value) -> EntryOut | None:
    head = conn.execute(
        f"SELECT entry_id, idempotency_key, description, created_at FROM ledger.journal_entries WHERE {by} = %s",
        (value,),
    ).fetchone()
    if head is None:
        return None
    rows = conn.execute(
        "SELECT account_id, amount FROM ledger.postings WHERE entry_id = %s ORDER BY posting_id", (head[0],)
    ).fetchall()
    return EntryOut(
        entry_id=head[0], idempotency_key=head[1], description=head[2], created_at=head[3],
        postings=[PostingOut(account_id=a, amount=m) for a, m in rows],
    )


@app.get("/health")
def health(conn: Conn) -> dict:
    conn.execute("SELECT 1")
    return {"status": "ok"}


@app.post("/entries", response_model=EntryOut, status_code=201)
def post_entry(entry: EntryIn, response: Response, conn: Conn):
    try:
        with conn.transaction():
            # Balance is checked when constraints fire. Defer while inserting, then check inside the block, so
            # a failure is raised here (and rolled back) instead of at some later COMMIT.
            conn.execute("SET CONSTRAINTS ALL DEFERRED")
            row = conn.execute(
                "INSERT INTO ledger.journal_entries (idempotency_key, description) VALUES (%s, %s)"
                " ON CONFLICT (idempotency_key) DO NOTHING RETURNING entry_id",
                (entry.idempotency_key, entry.description),
            ).fetchone()
            if row is None:  # this key was used before
                existing = fetch_entry(conn, "idempotency_key", entry.idempotency_key)
                wanted = sorted((p.account_id, p.amount) for p in entry.postings)
                if existing is None or wanted != sorted((p.account_id, p.amount) for p in existing.postings):
                    raise HTTPException(409, "idempotency_key already used for a different entry")
                response.status_code = 200
                return existing
            with conn.cursor() as cur:  # executemany lives on the cursor, not the connection
                cur.executemany(
                    "INSERT INTO ledger.postings (entry_id, account_id, amount) VALUES (%s, %s, %s)",
                    [(row[0], p.account_id, p.amount) for p in entry.postings],
                )
            conn.execute("SET CONSTRAINTS ALL IMMEDIATE")
            return fetch_entry(conn, "entry_id", row[0])
    except psycopg.errors.ForeignKeyViolation:
        raise HTTPException(422, "unknown account_id") from None
    except psycopg.errors.CheckViolation as e:
        raise HTTPException(422, e.diag.message_primary) from None


@app.get("/entries/{entry_id}", response_model=EntryOut)
def get_entry(entry_id: int, conn: Conn):
    found = fetch_entry(conn, "entry_id", entry_id)
    if found is None:
        raise HTTPException(404, "entry not found")
    return found


@app.get("/accounts", response_model=list[AccountOut])
def list_accounts(conn: Conn):
    rows = conn.execute("SELECT account_id, name, type FROM ledger.accounts ORDER BY account_id").fetchall()
    return [AccountOut(account_id=a, name=n, type=t) for a, n, t in rows]


@app.get("/accounts/{account_id}/balance", response_model=BalanceOut)
def get_balance(account_id: int, conn: Conn):
    row = conn.execute(
        "SELECT a.account_id, a.name, a.type, COALESCE(sum(p.amount), 0) FROM ledger.accounts a"
        " LEFT JOIN ledger.postings p USING (account_id) WHERE a.account_id = %s GROUP BY a.account_id",
        (account_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "account not found")
    return BalanceOut(account_id=row[0], name=row[1], type=row[2], balance=row[3])
