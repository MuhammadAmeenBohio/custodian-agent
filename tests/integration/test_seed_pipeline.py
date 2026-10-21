"""The whole seed pipeline on the live stack: after `make seed`, all 8 checks must pass."""
import pytest

from loader import validate as v
from loader.upload import s3_client

pytestmark = pytest.mark.integration


def test_live_data_passes_all_eight_checks():
    results = v.run_checks(v.load_frames(), v.bucket_key_exists(s3_client()))
    assert len(results) == 8
    failed = [name for name, ok in results if not ok]
    assert not failed, failed


def test_every_invoice_has_an_object_key_in_the_expected_form():
    frames = v.load_frames()
    keys = list(frames["invoices"]["object_key"])
    assert len(keys) == 50
    assert all(k == f"synthetic/{i}.png" for k, i in zip(keys, frames["invoices"]["invoice_id"]))
