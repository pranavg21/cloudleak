"""Doctor checks: each defect class is caught, and clean data passes."""

from __future__ import annotations

import json
import shutil

from cloudleak.doctor import CATALOG, run_doctor
from cloudleak.engine import Workspace
from tests.conftest import AWS_COLS, aws_month, aws_row, write_csv


def statuses(ws, check_id):
    return [r.status for r in run_doctor(ws).results if r.check_id == check_id]


def results(ws, check_id):
    return [r for r in run_doctor(ws).results if r.check_id == check_id]


def test_catalog_ids_unique():
    ids = [c.check_id for c in CATALOG]
    assert len(ids) == len(set(ids))


def test_demo_has_no_failures(demo_ws):
    rep = run_doctor(demo_ws)
    assert rep.counts()["FAIL"] == 0
    assert all(r.check_id in {c.check_id for c in CATALOG} for r in rep.results)


def test_preliminary_vs_finalized(demo_ws):
    acc = results(demo_ws, "CL-ACC-002")
    aws = {r.scope.split()[2]: r.status for r in acc if r.scope.startswith("aws")}
    assert aws == {"2026-09": "PASS", "2026-10": "WARN"}


def test_partial_month_warns(tmp_path):
    p = write_csv(tmp_path / "x.csv", AWS_COLS, aws_month([aws_row()], days=12, invoice=""))
    with Workspace([p]) as ws:
        assert "WARN" in statuses(ws, "CL-ACC-003")
        assert any("preliminary" in r.message.lower() for r in results(ws, "CL-ACC-002"))


def test_azure_identical_rows_kept_and_reported(demo, demo_ws):
    root, truth = demo
    info = [r for r in results(demo_ws, "CL-DQ-004") if "amortized" in r.scope]
    assert info and all(r.status == "INFO" for r in info)
    # The identical rows are still counted: totals equal ground truth (see test_ingest).
    n = demo_ws.scalar("SELECT count(*) FROM valid WHERE dataset = 'azure_amortized' AND sku LIKE '%D8s v5'")
    assert n == 2 * (30 + 31)


def test_duplicate_file_fails(tmp_path):
    p = write_csv(tmp_path / "a" / "x.csv", AWS_COLS, [aws_row()])
    (tmp_path / "b").mkdir()
    shutil.copy(p, tmp_path / "b" / "copy.csv")
    with Workspace([tmp_path]) as ws:
        assert statuses(ws, "CL-ING-002") == ["FAIL"]
        assert statuses(ws, "CL-ACC-006") == ["WARN"]


def test_overlapping_deliveries_warn(tmp_path):
    rows = [aws_row(lid="1"), aws_row(lid="2", line_item_resource_id="i-0def")]
    write_csv(tmp_path / "d1.csv", AWS_COLS, rows)
    write_csv(tmp_path / "d2.csv", AWS_COLS, rows + [aws_row(lid="3", line_item_resource_id="i-new")])
    with Workspace([tmp_path]) as ws:
        r = results(ws, "CL-ACC-006")
        assert r[0].status == "WARN" and r[0].metrics["duplicated_line_items"] == 2


def test_mixed_currency_kept_separate(tmp_path):
    rows = [aws_row(lid="1", line_item_unblended_cost="10"),
            aws_row(lid="2", line_item_currency_code="EUR", line_item_unblended_cost="7")]
    p = write_csv(tmp_path / "x.csv", AWS_COLS, rows)
    with Workspace([p]) as ws:
        assert "WARN" in statuses(ws, "CL-ACC-001")
        t = dict(ws.con.execute("SELECT currency, sum(cost) FROM valid GROUP BY 1").fetchall())
        assert t == {"USD": 10, "EUR": 7}


def test_unparseable_cost_fails(tmp_path):
    p = write_csv(tmp_path / "x.csv", AWS_COLS, [aws_row(line_item_unblended_cost="12,00")])
    with Workspace([p]) as ws:
        assert statuses(ws, "CL-ING-005") == ["FAIL"]


def test_missing_resource_id_column(tmp_path):
    cols = [c for c in AWS_COLS if c != "line_item_resource_id"]
    p = write_csv(tmp_path / "x.csv", cols, [aws_row()])
    with Workspace([p]) as ws:
        r = [x for x in results(ws, "CL-DQ-001") if x.status == "WARN"]
        assert r and "INCLUDE_RESOURCES" in r[0].remediation


def test_tag_key_variants(demo_ws):
    r = results(demo_ws, "CL-DQ-003")
    assert any(x.status == "WARN" and "user_Env" in x.message for x in r)


def test_focus_empty_columns(demo_ws):
    r = results(demo_ws, "CL-FOC-003")
    assert any(x.status == "WARN" and "invoice_id" in x.metrics["empty_columns"] for x in r)


def test_manifest_missing_file_and_rowcount(tmp_path):
    write_csv(tmp_path / "part-1.csv", AWS_COLS, [aws_row(lid="1"), aws_row(lid="2")])
    (tmp_path / "manifest.json").write_text(json.dumps({
        "blobs": [{"blobName": "exports/part-1.csv", "dataRowCount": 3},
                  {"blobName": "exports/part-2.csv", "dataRowCount": 1}]}))
    with Workspace([tmp_path]) as ws:
        assert statuses(ws, "CL-ING-003") == ["FAIL"]
        assert statuses(ws, "CL-ING-004") == ["FAIL"]


def test_gcp_late_usage_reported(demo_ws):
    r = results(demo_ws, "CL-DQ-006")
    assert r and all(x.status == "INFO" for x in r)


def test_mixed_basis_warns(demo_ws):
    assert "WARN" in statuses(demo_ws, "CL-ACC-004")
