"""Great Expectations checks on the seeded data (0.3.6). Run: python loader/validate.py

Eight checks. Each one builds a small DataFrame and asks Great Expectations to validate it.
Every check also refuses to pass on an empty source, so an empty table can never look "valid".
"""
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("TQDM_DISABLE", "1")  # Great Expectations prints a progress bar per check otherwise

import great_expectations as gx
import great_expectations.expectations as gxe
import pandas as pd
import psycopg
from botocore.exceptions import ClientError
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
ANSWER_KEY = REPO_ROOT / "eval" / "answer_key.jsonl"
EXPECTED_COUNTS = {"clean": 43, "over_threshold": 1, "bank_changed": 1, "duplicate": 1,
                   "blocked_vendor": 3, "injection": 1}
FAR_FUTURE = pd.Timestamp("2100-01-01")


def gx_passes(context, name: str, df: pd.DataFrame, expectation) -> bool:
    """Validate one DataFrame against one expectation with Great Expectations."""
    source = context.data_sources.add_pandas(name)
    asset = source.add_dataframe_asset(name)
    batch = asset.add_batch_definition_whole_dataframe("whole").get_batch(batch_parameters={"dataframe": df})
    return bool(batch.validate(expectation).success)


def check_vendor_references(f):
    ids = set(f["vendors"]["vendor_id"])
    refs = pd.concat([f["payments"][["vendor_id"]], f["invoices"][["vendor_id"]]], ignore_index=True)
    refs["vendor_exists"] = refs["vendor_id"].isin(ids)
    return refs, gxe.ExpectColumnValuesToBeInSet(column="vendor_exists", value_set=[True])


def check_positive_amounts(f):
    amounts = pd.concat([f["payments"][["amount"]], f["invoices"][["amount"]]], ignore_index=True)
    amounts["amount"] = amounts["amount"].astype(float)
    return amounts, gxe.ExpectColumnValuesToBeBetween(column="amount", min_value=0, strict_min=True)


def check_one_current_account(f):
    current = f["accounts"][f["accounts"]["valid_to"].isna()].groupby("vendor_id").size()
    per_vendor = f["vendors"][["vendor_id"]].copy()
    per_vendor["n_current"] = per_vendor["vendor_id"].map(current).fillna(0).astype(int)
    return per_vendor, gxe.ExpectColumnValuesToBeInSet(column="n_current", value_set=[1])


def check_no_overlapping_accounts(f):
    a = f["accounts"].copy()
    a["valid_from"] = pd.to_datetime(a["valid_from"])
    a["valid_to"] = pd.to_datetime(a["valid_to"]).fillna(FAR_FUTURE)
    a = a.sort_values(["vendor_id", "valid_from"])
    prev_end = a.groupby("vendor_id")["valid_to"].shift(1)
    a["overlaps"] = (a["valid_from"] < prev_end).fillna(False).astype(bool)  # first row has no previous
    return a, gxe.ExpectColumnValuesToBeInSet(column="overlaps", value_set=[False])


def check_fake_ibans(f):
    ibans = pd.concat([f["accounts"]["iban"], f["invoices"]["iban_on_invoice"].rename("iban")], ignore_index=True)
    return ibans.to_frame("iban"), gxe.ExpectColumnValuesToMatchRegex(column="iban", regex=r"^XX")


def check_scenario_counts(f):
    actual = f["key"]["scenario"].value_counts()
    rows = pd.DataFrame({"scenario": sorted(set(EXPECTED_COUNTS) | set(actual.index))})
    rows["actual"] = rows["scenario"].map(actual).fillna(0).astype(int)
    rows["expected"] = rows["scenario"].map(EXPECTED_COUNTS).fillna(0).astype(int)
    rows["matches"] = rows["actual"] == rows["expected"]
    return rows, gxe.ExpectColumnValuesToBeInSet(column="matches", value_set=[True])


def check_answer_key_one_to_one(f):
    inv = f["invoices"][["invoice_id", "invoice_number", "vendor_id", "iban_on_invoice"]]
    key = f["key"][["invoice_id", "invoice_number", "vendor_id", "iban"]]
    m = inv.merge(key, on="invoice_id", how="outer", suffixes=("_db", "_key"), indicator=True)
    m["matched"] = (
        (m["_merge"] == "both")
        & (m["invoice_number_db"] == m["invoice_number_key"])
        & (m["vendor_id_db"] == m["vendor_id_key"])
        & (m["iban_on_invoice"] == m["iban"])
    ).astype(bool)
    return m, gxe.ExpectColumnValuesToBeInSet(column="matched", value_set=[True])


def make_object_key_check(key_exists):
    def check_object_keys(f):
        inv = f["invoices"][["invoice_id", "object_key"]].copy()
        inv["key_exists"] = [isinstance(k, str) and bool(k) and key_exists(k) for k in inv["object_key"]]
        return inv, gxe.ExpectColumnValuesToBeInSet(column="key_exists", value_set=[True])
    return check_object_keys


def run_checks(frames: dict, key_exists) -> list[tuple[str, bool]]:
    """frames: vendors, accounts, payments, invoices, key (DataFrames). key_exists: object_key -> bool."""
    context = gx.get_context(mode="ephemeral")
    checks = [
        ("1 payments and invoices reference existing vendors", check_vendor_references),
        ("2 no non-positive amounts", check_positive_amounts),
        ("3 exactly one current account per vendor", check_one_current_account),
        ("4 no overlapping account date ranges", check_no_overlapping_accounts),
        ("5 all IBANs start with XX", check_fake_ibans),
        ("6 every object_key exists in the bucket", make_object_key_check(key_exists)),
        ("7 scenario counts match the plan", check_scenario_counts),
        ("8 invoices and answer key match one to one", check_answer_key_one_to_one),
    ]
    results = []
    for i, (name, fn) in enumerate(checks):
        df, expectation = fn(frames)
        ok = len(df) > 0 and gx_passes(context, f"check{i}", df, expectation)  # empty source never passes
        results.append((name, ok))
    return results


def _query(conn, sql: str) -> pd.DataFrame:
    cur = conn.execute(sql)
    return pd.DataFrame(cur.fetchall(), columns=[c.name for c in cur.description])


def load_frames() -> dict:
    with psycopg.connect(
        host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
        port=int(os.getenv("POSTGRES_PORT", "15432")),
        dbname=os.environ["POSTGRES_DB"],
        user="vendors_ro",
        password=os.environ["VENDORS_RO_DB_PASSWORD"],
    ) as conn:
        frames = {t: _query(conn, f"SELECT * FROM vendors.{table}") for t, table in [
            ("vendors", "vendors"), ("accounts", "vendor_bank_accounts"),
            ("payments", "historical_payments"), ("invoices", "invoices")]}
    lines = ANSWER_KEY.read_text(encoding="utf-8").splitlines()
    frames["key"] = pd.DataFrame([json.loads(x) for x in lines])
    return frames


def bucket_key_exists(s3, bucket: str = "invoices"):
    def exists(key: str) -> bool:
        try:
            s3.head_object(Bucket=bucket, Key=key)  # head_object, not listing: listing was unreliable
            return True
        except ClientError:
            return False
    return exists


def main() -> int:
    load_dotenv(override=True)
    from loader.upload import s3_client  # imported late so unit tests need no S3 settings
    results = run_checks(load_frames(), bucket_key_exists(s3_client()))
    for name, ok in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.path.insert(0, str(REPO_ROOT))  # so `python loader/validate.py` can import loader.upload
    sys.exit(main())
