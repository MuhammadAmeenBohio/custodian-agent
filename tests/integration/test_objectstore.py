import datetime as dt
import os
import uuid

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def s3():
    return boto3.client(
        "s3",
        endpoint_url=f"http://127.0.0.1:{os.getenv('OBJECTSTORE_PORT', '18333')}",
        aws_access_key_id=os.getenv("OBJECTSTORE_ACCESS_KEY", "dev"),
        aws_secret_access_key=os.getenv("OBJECTSTORE_SECRET_KEY", "dev"),
        region_name="us-east-1",
        config=Config(retries={"max_attempts": 1}),
    )


@pytest.mark.parametrize("bucket", ["invoices", "attachments"])
def test_versioned_bucket_allows_permanent_delete(s3, bucket):
    """Allowed case: the control that makes the lock test meaningful."""
    key = f"test/{uuid.uuid4()}.txt"
    version_id = s3.put_object(Bucket=bucket, Key=key, Body=b"x").get("VersionId")
    assert version_id, f"{bucket} returned no VersionId, so versioning is not effective"
    s3.delete_object(Bucket=bucket, Key=key, VersionId=version_id)
    with pytest.raises(ClientError) as e:
        s3.get_object(Bucket=bucket, Key=key, VersionId=version_id)
    assert e.value.response["Error"]["Code"] in ("NoSuchVersion", "NoSuchKey", "404")


def test_locked_version_cannot_be_deleted(s3):
    """Denied case: a COMPLIANCE-locked version must survive a delete attempt."""
    key = f"test/{uuid.uuid4()}.txt"
    retain = dt.datetime.now(dt.UTC) + dt.timedelta(days=1)
    version_id = s3.put_object(
        Bucket="audit-worm", Key=key, Body=b"x",
        ObjectLockMode="COMPLIANCE", ObjectLockRetainUntilDate=retain,
    ).get("VersionId")
    assert version_id, "no VersionId returned, so this test would prove nothing"
    with pytest.raises(ClientError) as e:
        s3.delete_object(Bucket="audit-worm", Key=key, VersionId=version_id)
    assert e.value.response["Error"]["Code"] == "AccessDenied"
    body = s3.get_object(Bucket="audit-worm", Key=key, VersionId=version_id)["Body"].read()
    assert body == b"x"  # still there