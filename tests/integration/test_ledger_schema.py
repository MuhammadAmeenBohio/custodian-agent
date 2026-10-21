"""Ledger schema rules (1.1.1). Needs the stack up and `make ledger-schema` run.

Every test works inside a transaction that is rolled back at teardown, so no probe entry persists.
Balance is checked at COMMIT, so tests call `SET CONSTRAINTS ALL IMMEDIATE` to run that check now.
"""
import os

import psycopg
import pytest
from psycopg import errors

pytestmark = pytest.mark.integration


@pytest.fixture
def conn():
    c = psycopg.connect(
        host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
        port=int(os.getenv("POSTGRES_PORT", "15432")),
        dbname=os.environ["POSTGRES_DB"],
        user="ledger_user",
        password=os.environ["LEDGER_DB_PASSWORD"],
    )
    try:
        yield c
    finally:
        c.rollback()
        c.close()


def new_entry(conn, key: str) -> int:
    entry_id = conn.execute(
        "INSERT INTO ledger.journal_entries (idempotency_key, description) VALUES (%s, 'test') RETURNING entry_id",
        (key,),
    ).fetchone()[0]
    assert entry_id, "no entry_id returned, so the tests below would prove nothing"
    return entry_id


def post(conn, entry_id: int, account_id: int, amount) -> None:
    conn.execute(
        "INSERT INTO ledger.postings (entry_id, account_id, amount) VALUES (%s, %s, %s)",
        (entry_id, account_id, amount),
    )


def check_now(conn) -> None:
    conn.execute("SET CONSTRAINTS ALL IMMEDIATE")


def test_chart_of_accounts_is_seeded(conn):
    rows = conn.execute("SELECT account_id, type FROM ledger.accounts ORDER BY 1").fetchall()
    assert rows == [(1000, "asset"), (2000, "liability"), (5000, "expense")]


def test_balanced_entry_is_allowed(conn):
    """Allowed case: the control that makes the denials below meaningful."""
    e = new_entry(conn, "t-balanced")
    post(conn, e, 2000, "250.00")   # debit Accounts Payable
    post(conn, e, 1000, "-250.00")  # credit Cash
    check_now(conn)
    total = conn.execute("SELECT sum(amount) FROM ledger.postings WHERE entry_id=%s", (e,)).fetchone()[0]
    assert total == 0


def test_three_posting_entry_that_balances_is_allowed(conn):
    e = new_entry(conn, "t-three")
    post(conn, e, 5000, "100.00")
    post(conn, e, 5000, "50.00")
    post(conn, e, 2000, "-150.00")
    check_now(conn)


def test_unbalanced_entry_is_denied(conn):
    e = new_entry(conn, "t-unbalanced")
    post(conn, e, 2000, "250.00")
    post(conn, e, 1000, "-249.99")
    with pytest.raises(errors.CheckViolation):
        check_now(conn)


def test_single_posting_entry_is_denied(conn):
    e = new_entry(conn, "t-single")
    post(conn, e, 2000, "250.00")
    with pytest.raises(errors.CheckViolation):
        check_now(conn)


def test_entry_without_postings_is_denied(conn):
    new_entry(conn, "t-empty")
    with pytest.raises(errors.CheckViolation):
        check_now(conn)


def posted_entry(conn, key: str) -> int:
    """A balanced entry, checked now. Control for the append-only tests below."""
    e = new_entry(conn, key)
    post(conn, e, 2000, "10.00")
    post(conn, e, 1000, "-10.00")
    check_now(conn)
    return e


def test_posted_posting_cannot_be_updated(conn):
    e = posted_entry(conn, "t-update")
    with pytest.raises(errors.InsufficientPrivilege):
        conn.execute("UPDATE ledger.postings SET amount = 99 WHERE entry_id=%s AND amount > 0", (e,))


def test_posted_posting_cannot_be_deleted(conn):
    e = posted_entry(conn, "t-delete")
    with pytest.raises(errors.InsufficientPrivilege):
        conn.execute("DELETE FROM ledger.postings WHERE entry_id=%s AND amount > 0", (e,))


def test_posted_entry_cannot_be_changed_or_deleted(conn):
    e = posted_entry(conn, "t-entry")
    with pytest.raises(errors.InsufficientPrivilege):
        conn.execute("UPDATE ledger.journal_entries SET description='edited' WHERE entry_id=%s", (e,))
    conn.rollback()
    e = posted_entry(conn, "t-entry-2")
    with pytest.raises(errors.InsufficientPrivilege):
        conn.execute("DELETE FROM ledger.journal_entries WHERE entry_id=%s", (e,))


@pytest.mark.parametrize("table", ["postings", "journal_entries"])
def test_truncate_is_denied(conn, table):
    posted_entry(conn, f"t-truncate-{table}")
    with pytest.raises(errors.InsufficientPrivilege):
        conn.execute(f"TRUNCATE ledger.{table} CASCADE")


def test_zero_amount_posting_is_denied(conn):
    e = new_entry(conn, "t-zero")
    with pytest.raises(errors.CheckViolation):
        post(conn, e, 2000, "0.00")


def test_unknown_account_is_denied(conn):
    e = new_entry(conn, "t-unknown-account")
    with pytest.raises(errors.ForeignKeyViolation):
        post(conn, e, 9999, "10.00")
        check_now(conn)


def test_duplicate_idempotency_key_is_denied(conn):
    new_entry(conn, "t-dup")  # first use of the key is allowed
    with pytest.raises(errors.UniqueViolation):
        new_entry(conn, "t-dup")
