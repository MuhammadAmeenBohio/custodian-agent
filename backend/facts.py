"""Facts about an invoice, for authorize(). Plain data in, plain data out (1.4.2).

build_facts is pure: it takes records and returns the dict authorize() reads. load_vendor and find_vendor_id are the
SQL shell, run as vendors_ro (read-only). Nothing here decides anything; deciding is authorize()'s job.
"""
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

import psycopg


@dataclass
class VendorRecord:
    vendor_id: int
    name: str
    status: str
    accounts: list[tuple[str, date, date | None]]  # (iban, valid_from, valid_to); valid_to None = current
    paid_invoice_numbers: set[str] = field(default_factory=set)
    risk_flags: list[str] = field(default_factory=list)  # for the reviewer summary; no rule reads them

    def current_account(self) -> tuple[str, date, date | None] | None:
        current = [a for a in self.accounts if a[2] is None]
        return current[0] if len(current) == 1 else None  # zero or several current accounts: unknown


def build_facts(vendor: VendorRecord | None, invoice_number: str, amount: Decimal, iban: str, today: date) -> dict:
    if vendor is None:
        return {"vendor_exists": False}
    current = vendor.current_account()
    facts = {
        "vendor_exists": True,
        "vendor_id": vendor.vendor_id,
        "vendor_status": vendor.status,
        "amount": amount,
        "duplicate": invoice_number in vendor.paid_invoice_numbers,
        "risk_flags": list(vendor.risk_flags),
    }
    if current is not None:
        facts["iban_matches_current"] = iban == current[0]
        facts["days_since_bank_change"] = (today - current[1]).days
    return facts  # without a single current account those two facts are absent, and authorize() denies


def normalise_name(name: str) -> str:
    """Case and spacing only. Not fuzzy: a misspelt name must not match a different vendor."""
    return " ".join(name.lower().split())


def unique_match(vendors: dict[int, str], query: str) -> int | None:
    """The one vendor whose normalised name equals the query's, else None (unknown, empty or ambiguous)."""
    q = normalise_name(query)
    hits = [vid for vid, name in vendors.items() if q and normalise_name(name) == q]
    return hits[0] if len(hits) == 1 else None


def find_vendor_id(conn: psycopg.Connection, name: str) -> int | None:
    vendors = dict(conn.execute("SELECT vendor_id, name FROM vendors.vendors").fetchall())
    return unique_match(vendors, name)


def load_vendor(conn: psycopg.Connection, vendor_id: int) -> VendorRecord | None:
    row = conn.execute("SELECT vendor_id, name, status FROM vendors.vendors WHERE vendor_id = %s", (vendor_id,)).fetchone()
    if row is None:
        return None
    accounts = conn.execute(
        "SELECT iban, valid_from, valid_to FROM vendors.vendor_bank_accounts WHERE vendor_id = %s ORDER BY valid_from",
        (vendor_id,),
    ).fetchall()
    paid = conn.execute(
        "SELECT invoice_number FROM vendors.historical_payments WHERE vendor_id = %s", (vendor_id,)
    ).fetchall()
    flags = conn.execute("SELECT flag_type FROM vendors.vendor_risk_flags WHERE vendor_id = %s", (vendor_id,)).fetchall()
    return VendorRecord(row[0], row[1], row[2], [tuple(a) for a in accounts], {p[0] for p in paid}, [f[0] for f in flags])
