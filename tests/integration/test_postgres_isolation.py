import os

import psycopg
import pytest
from psycopg import errors

pytestmark = pytest.mark.integration

SERVICES = ["agent", "ledger", "vendors", "policy", "audit"]
PROBE_TABLE = "isolation_probe"


def connect_as(user: str, password_env: str) -> psycopg.Connection:
    """Open a connection as any database user, reading the password from .env."""
    return psycopg.connect(
        host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
        port=int(os.getenv("POSTGRES_PORT", "15432")),
        dbname=os.environ["POSTGRES_DB"],
        user=user,
        password=os.environ[password_env],
        autocommit=True,
    )


def connect(service: str) -> psycopg.Connection:
    """Open a connection as one service's own database user."""
    return connect_as(f"{service}_user", f"{service.upper()}_DB_PASSWORD")


@pytest.mark.parametrize("service", SERVICES)
def test_service_can_use_its_own_schema(service):
    """Allowed case: proves the login works, so the denials below mean something."""
    with connect(service) as conn:
        conn.execute(f"CREATE TABLE {service}.{PROBE_TABLE} (id int)")
        conn.execute(f"DROP TABLE {service}.{PROBE_TABLE}")


@pytest.mark.parametrize(
    "owner,other",
    [(a, b) for a in SERVICES for b in SERVICES if a != b],
)
def test_service_cannot_write_to_another_schema(owner, other):
    """Denied case: every service against every other service's schema."""
    with connect(owner) as conn:
        try:
            conn.execute(f"CREATE TABLE {other}.{PROBE_TABLE} (id int)")
        except errors.InsufficientPrivilege:
            return  # correct: access was denied
        # Isolation is broken. Clean up the table, then fail loudly.
        conn.execute(f"DROP TABLE {other}.{PROBE_TABLE}")
        pytest.fail(f"{owner}_user was able to create a table in schema {other}")

def test_vendors_ro_can_read_but_not_write():
    with connect_as("vendors_user", "VENDORS_DB_PASSWORD") as owner:
        owner.execute("CREATE TABLE vendors.ro_probe (id int)")
        try:
            with connect_as("vendors_ro", "VENDORS_RO_DB_PASSWORD") as ro:
                ro.execute("SELECT * FROM vendors.ro_probe")  # must work
                with pytest.raises(errors.InsufficientPrivilege):
                    ro.execute("INSERT INTO vendors.ro_probe VALUES (1)")  # must be denied
        finally:
            owner.execute("DROP TABLE vendors.ro_probe")