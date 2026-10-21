"""The real dependencies of the pipeline (1.4.5): object store, Tesseract, gateway, vendor database, runner.

Everything is read from the environment at call time, so a missing variable fails the run (and is recorded on its
step) rather than stopping the API from starting. The database logins are separate and least-privileged: the
pipeline writes runs and steps as agent_user and reads vendor facts as vendors_ro (SELECT only).
"""
import os
from datetime import date
from uuid import UUID

import boto3
import psycopg
from botocore.config import Config

from backend.facts import VendorRecord, find_vendor_id, load_vendor
from backend.pipeline import Deps, run_pipeline
from backend.readers import ocr_bytes, read_vision
from backend.summary import summarise

BUCKET = "invoices"


def _connect(user: str, password_var: str, autocommit: bool) -> psycopg.Connection:
    return psycopg.connect(
        host=os.getenv("POSTGRES_HOST", "127.0.0.1"), port=int(os.getenv("POSTGRES_PORT", "15432")),
        dbname=os.environ["POSTGRES_DB"], user=user, password=os.environ[password_var], autocommit=autocommit)


def build_deps() -> Deps:
    s3 = boto3.client(
        "s3", endpoint_url=os.environ["OBJECTSTORE_ENDPOINT"],
        aws_access_key_id=os.getenv("OBJECTSTORE_ACCESS_KEY", "dev"),
        aws_secret_access_key=os.getenv("OBJECTSTORE_SECRET_KEY", "dev"),
        region_name="us-east-1", config=Config(retries={"max_attempts": 2}))
    gateway, key = os.environ["LITELLM_BASE_URL"], os.environ["LITELLM_MASTER_KEY"]

    def vendor_by_name(name: str) -> VendorRecord | None:
        with _connect("vendors_ro", "VENDORS_RO_DB_PASSWORD", autocommit=True) as conn:
            vendor_id = find_vendor_id(conn, name)
            return load_vendor(conn, vendor_id) if vendor_id else None

    return Deps(
        load_png=lambda object_key: s3.get_object(Bucket=BUCKET, Key=object_key)["Body"].read(),
        ocr=ocr_bytes,
        vision=lambda png: read_vision(png, gateway, key),
        vendor_by_name=vendor_by_name,
        summarise=lambda data: summarise(data, gateway, key),
        today=date.today,
    )


def make_runner():
    """The function POST /runs starts in the background. It opens its own connection: the request's is closed by then."""
    def runner(run_id: UUID) -> None:
        with _connect("agent_user", "AGENT_DB_PASSWORD", autocommit=True) as conn:
            run_pipeline(conn, run_id, build_deps())
    return runner
