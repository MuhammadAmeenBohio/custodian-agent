"""What the ledger_api login may and may not do (1.1.3). Needs the stack up and `make ledger-schema` run.

Each denial first creates a valid posted entry in the same transaction (the allowed case), then tries to
change it. Every denied statement must fail with "permission denied" or "must be owner", which proves the
role's privileges stop it, not just the append-only triggers (those would also raise InsufficientPrivilege).
Everything is rolled back at teardown.
"""
import os
import uuid

import psycopg
import pytest
from psycopg import errors

pytestmark = pytest.mark.integration


@pytest.fixture
def api():
    c = psycopg.connect(
        host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
        port=int(os.getenv("POSTGRES_PORT", "15432")),
        dbname=os.environ["POSTGRES_DB"],
        user="ledger_api",
        password=os.environ["LEDGER_API_DB_PASSWORD"],
    )
    try:
        yield c
    finally:
        c.rollback()
        c.close()


def post_valid_entry(c) -> int:
    entry_id = c.execute(
        "INSERT INTO ledger.journal_entries (idempotency_key, description) VALUES (%s, 'role test') RETURNING entry_id",
        (f"role-{uuid.uuid4()}",),
    ).fetchone()[0]
    assert entry_id, "no entry_id, so the denials below would prove nothing"
    c.execute("INSERT INTO ledger.postings (entry_id, account_id, amount) VALUES (%s, 2000, 5), (%s, 1000, -5)",
              (entry_id, entry_id))
    c.execute("SET CONSTRAINTS ALL IMMEDIATE")
    return entry_id


def test_api_role_can_insert_and_read(api):
    """Allowed case: the control for the denials below."""
    entry_id = post_valid_entry(api)
    n = api.execute("SELECT count(*) FROM ledger.postings WHERE entry_id=%s", (entry_id,)).fetchone()[0]
    assert n == 2
    assert api.execute("SELECT count(*) FROM ledger.accounts").fetchone()[0] == 3


DENIED = [
    ("update posting", "UPDATE ledger.postings SET amount = 99", "permission denied"),
    ("delete posting", "DELETE FROM ledger.postings", "permission denied"),
    ("update entry", "UPDATE ledger.journal_entries SET description = 'x'", "permission denied"),
    ("delete entry", "DELETE FROM ledger.journal_entries", "permission denied"),
    ("truncate postings", "TRUNCATE ledger.postings", "permission denied"),
    ("insert account", "INSERT INTO ledger.accounts VALUES (9000, 'Rogue', 'asset')", "permission denied"),
    ("update account", "UPDATE ledger.accounts SET name = 'x'", "permission denied"),
    ("drop append-only trigger", "DROP TRIGGER postings_append_only ON ledger.postings", "must be owner"),
    ("create table", "CREATE TABLE ledger.rogue (id int)", "permission denied"),
    ("read another schema", "SELECT 1 FROM vendors.vendors", "permission denied"),
]


@pytest.mark.parametrize("sql,fragment", [(d[1], d[2]) for d in DENIED], ids=[d[0] for d in DENIED])
def test_api_role_is_denied(api, sql, fragment):
    post_valid_entry(api)
    with pytest.raises(errors.InsufficientPrivilege) as e:
        api.execute(sql)
    assert fragment in str(e.value)
