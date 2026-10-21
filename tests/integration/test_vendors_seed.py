"""Integration tests for the seeded vendors schema. Needs the stack up and `make seed` run."""
import os
from datetime import date

import psycopg
import pytest
from psycopg import errors

pytestmark = pytest.mark.integration


def connect(user: str, password_env: str) -> psycopg.Connection:
    # No autocommit. The `owner` fixture rolls back explicitly at teardown, so probe rows never persist.
    return psycopg.connect(
        host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
        port=int(os.getenv("POSTGRES_PORT", "15432")),
        dbname=os.environ["POSTGRES_DB"],
        user=user,
        password=os.environ[password_env],
    )


@pytest.fixture
def owner():
    conn = connect("vendors_user", "VENDORS_DB_PASSWORD")
    try:
        yield conn
    finally:
        conn.rollback()  # psycopg 3 `with conn:` COMMITS on clean exit, so roll back explicitly
        conn.close()


def scalar(conn, sql):
    return conn.execute(sql).fetchone()[0]


def test_seed_row_counts(owner):
    assert scalar(owner, "SELECT count(*) FROM vendors.vendors") == 40
    assert scalar(owner, "SELECT count(*) FROM vendors.vendor_bank_accounts") == 49
    assert scalar(owner, "SELECT count(*) FROM vendors.vendor_risk_flags") == 10
    assert scalar(owner, "SELECT count(*) FROM vendors.invoices") == 50
    assert scalar(owner, "SELECT count(*) FROM vendors.historical_payments") >= 40 * 6


def test_blocked_vendors(owner):
    rows = owner.execute("SELECT vendor_id FROM vendors.vendors WHERE status='blocked' ORDER BY 1").fetchall()
    assert [r[0] for r in rows] == [38, 39, 40]


def test_vendor_5_account_changed_recently(owner):
    valid_from = scalar(owner, "SELECT valid_from FROM vendors.vendor_bank_accounts WHERE vendor_id=5 AND valid_to IS NULL")
    assert valid_from, "vendor 5 has no current account"
    assert (date.today() - valid_from).days >= 10  # seeded 10 days before the seed date


def test_second_current_account_is_denied(owner):
    """Denied: the partial unique index. Control: a closed account is allowed."""
    owner.execute(
        "INSERT INTO vendors.vendor_bank_accounts VALUES (9001, 1, 'XX00FAKE9999999001', '2000-01-01', '2000-02-01')"
    )  # allowed: closed account
    with pytest.raises(errors.UniqueViolation):
        owner.execute(
            "INSERT INTO vendors.vendor_bank_accounts VALUES (9002, 1, 'XX00FAKE9999999002', '2000-03-01', NULL)"
        )


def test_real_looking_iban_is_denied(owner):
    """Denied: IBAN must start with XX. Control: an XX IBAN is accepted."""
    owner.execute(
        "INSERT INTO vendors.vendor_bank_accounts VALUES (9003, 1, 'XX00FAKE9999999003', '2000-01-01', '2000-02-01')"
    )
    with pytest.raises(errors.CheckViolation):
        owner.execute(
            "INSERT INTO vendors.vendor_bank_accounts VALUES (9004, 1, 'GB00REAL0000000004', '2000-03-01', '2000-04-01')"
        )


def test_duplicate_invoice_number_is_allowed_as_data(owner):
    """Allowed on purpose: spotting duplicates is the agent's job, not the database's."""
    existing = owner.execute("SELECT vendor_id, invoice_number FROM vendors.invoices LIMIT 1").fetchone()
    assert existing and existing[1], "no invoice to duplicate, so this test would prove nothing"
    owner.execute(
        "INSERT INTO vendors.invoices VALUES (9005, %s, %s, 10, '2026-01-01', '2026-01-31', 'XX00FAKE0000000001', NULL)",
        existing,
    )


def test_non_positive_amount_is_denied(owner):
    with pytest.raises(errors.CheckViolation):
        owner.execute(
            "INSERT INTO vendors.invoices VALUES (9006, 1, 'X-1', 0, '2026-01-01', '2026-01-31', 'XX00FAKE0000000001', NULL)"
        )


def test_vendors_ro_reads_seeded_data():
    with connect("vendors_ro", "VENDORS_RO_DB_PASSWORD") as ro:
        assert scalar(ro, "SELECT count(*) FROM vendors.vendors") == 40
        with pytest.raises(errors.InsufficientPrivilege):
            ro.execute("DELETE FROM vendors.vendors")
