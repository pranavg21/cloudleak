"""Invoice equality must not conceal a known delivery error."""
import json

import pytest

from cloudleak.cli import main
from cloudleak.doctor import run_doctor
from cloudleak.engine import Workspace
from tests.conftest import write_csv

HEADER = ["InvoiceId", "BilledCost", "BillingCurrency", "BillingAccountId", "ChargeCategory", "ChargePeriodStart"]
ROW = ["INV", "10", "USD", "account", "Usage", "2026-03-01"]


@pytest.mark.parametrize("case", ["duplicate", "missing", "row_count", "duplicate_basename"])
def test_delivery_errors_block_invoice_output(tmp_path, case):
    delivery = tmp_path / "delivery"
    part = write_csv(delivery / "part.csv", HEADER, [ROW])
    manifest = {}
    if case == "duplicate":
        (delivery / "copy.csv").write_bytes(part.read_bytes())
    elif case == "missing":
        manifest = {"reportKeys": ["part.csv", "other.csv"]}
    elif case == "row_count":
        manifest = {"blobs": [{"blobName": "part.csv"}], "dataRowCount": 2}
    else:
        manifest = {"reportKeys": ["one/part.csv", "two/part.csv"]}
    if manifest:
        (delivery / "manifest.json").write_text(json.dumps(manifest))
    with Workspace([delivery]) as ws:
        assert run_doctor(ws).worst == "FAIL"
    output = tmp_path / "result.json"
    assert main(["reconcile", str(delivery), "--invoice-id", "INV", "--currency", "USD",
                 "--total", "20" if case == "duplicate" else "10", "-o", str(output)]) == 2
    assert not output.exists()


def test_same_basename_in_distinct_manifest_partitions(tmp_path):
    write_csv(tmp_path / "one" / "part.csv", HEADER, [ROW])
    write_csv(tmp_path / "two" / "part.csv", HEADER, [ROW, ROW])
    (tmp_path / "manifest.json").write_text(json.dumps({"dataRowCount": 3, "blobs": [
        {"blobName": "export/one/part.csv", "dataRowCount": 1},
        {"blobName": "export/two/part.csv", "dataRowCount": 2}]}))
    assert main(["reconcile", str(tmp_path), "--invoice-id", "INV", "--currency", "USD", "--total", "30"]) == 0


def test_saved_reconciliation_includes_accepted_residual(tmp_path):
    source = write_csv(tmp_path / "part.csv", HEADER, [ROW])
    outputs = [tmp_path / f"result.{ext}" for ext in ("md", "html", "csv")]
    args = [arg for path in outputs for arg in ("-o", str(path))]
    assert main(["reconcile", str(source), "--invoice-id", "INV", "--currency", "USD",
                 "--total", "10.001", "--tolerance", "0.01", *args]) == 0
    for path in outputs:
        text = path.read_text(encoding="utf-8")
        for token in ("10.001", "-0.001", "within_tolerance", "not_conclusive"):
            assert token in text

@pytest.mark.parametrize("bad_count", [True, "two", -1])
def test_malformed_manifest_count_is_an_input_error(tmp_path, bad_count, capsys):
    write_csv(tmp_path / "part.csv", HEADER, [ROW])
    manifest = {"blobs": [{"blobName": "part.csv", "dataRowCount": bad_count}]}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    assert main(["reconcile", str(tmp_path), "--invoice-id", "INV", "--currency", "USD", "--total", "10"]) == 2
    assert "dataRowCount" in capsys.readouterr().err
