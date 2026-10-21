"""Upload rendered PNGs to the invoices bucket as synthetic/<invoice_id>.png and record object_key."""
import os
from pathlib import Path

import boto3
import psycopg
from botocore.config import Config
from dotenv import load_dotenv

OUT_DIR = Path(__file__).resolve().parent.parent / "generated"
BUCKET = "invoices"


def s3_client():
    return boto3.client(
        "s3",
        endpoint_url=f"http://127.0.0.1:{os.getenv('OBJECTSTORE_PORT', '18333')}",
        aws_access_key_id=os.getenv("OBJECTSTORE_ACCESS_KEY", "dev"),
        aws_secret_access_key=os.getenv("OBJECTSTORE_SECRET_KEY", "dev"),
        region_name="us-east-1",
        config=Config(retries={"max_attempts": 2}),
    )


def main() -> None:
    load_dotenv(override=True)
    s3 = s3_client()
    files = sorted(OUT_DIR.glob("*.png"), key=lambda p: int(p.stem))
    assert files, "no PNGs in generated/: run loader/render.py first"
    with psycopg.connect(
        host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
        port=int(os.getenv("POSTGRES_PORT", "15432")),
        dbname=os.environ["POSTGRES_DB"],
        user="vendors_user",
        password=os.environ["VENDORS_DB_PASSWORD"],
    ) as conn:
        for f in files:
            key = f"synthetic/{f.stem}.png"
            version_id = s3.put_object(Bucket=BUCKET, Key=key, Body=f.read_bytes(), ContentType="image/png").get("VersionId")
            assert version_id, f"{BUCKET} returned no VersionId for {key}"  # the lesson from the empty-VersionId bug
            conn.execute("UPDATE vendors.invoices SET object_key=%s WHERE invoice_id=%s", (key, int(f.stem)))
    print(f"uploaded {len(files)} invoices to s3://{BUCKET}/synthetic/")


if __name__ == "__main__":
    main()
