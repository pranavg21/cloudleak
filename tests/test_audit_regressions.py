"""Regression tests for defects found in the adversarial audit (one test per defect, each a reproduction)."""

from __future__ import annotations

import json
import shutil
import threading
import time
from decimal import Decimal

import duckdb
import pytest

from cloudleak import variance as V
from cloudleak.cli import main
from cloudleak.doctor import run_doctor
from cloudleak.engine import Workspace
from cloudleak.providers import focus
from cloudleak.providers.azure import basis_from_name
from cloudleak.providers.base import Columns
from cloudleak.reconcile import Expectation, reconcile
from cloudleak.redact import classify, load_key, redact
from cloudleak.render.formats import owner_label
from tests.conftest import AWS_COLS, aws_row, write_csv

LEGACY = ["identity/LineItemId", "bill/InvoiceId", "bill/PayerAccountId", "bill/BillingPeriodStartDate",
          "lineItem/UsageAccountId", "lineItem/LineItemType", "lineItem/UsageStartDate", "lineItem/ProductCode",
          "lineItem/UsageType", "lineItem/ResourceId", "lineItem/UnblendedCost", "lineItem/CurrencyCode"]


def legacy_row(lid: str) -> list[str]:
    return [lid, "", "111122223333", "2026-10-01T00:00:00Z", "444455556666", "Usage", "2026-10-02T00:00:00Z",
            "AmazonEC2", "BoxUsage", "i-1", "100", "USD"]


def statuses(ws, check_id):
    return [r.status for r in run_doctor(ws).results if r.check_id == check_id]


def test_d2_stale_cur_assembly_beside_manifest_fails(tmp_path):
    per = tmp_path / "cur" / "20261001-20261101"
    write_csv(per / "asm-OLD" / "rep-00001.csv", LEGACY, [legacy_row("x1")])
    write_csv(per / "asm-NEW" / "rep-00001.csv", LEGACY, [legacy_row("x1b")])
    (per / "rep-Manifest.json").write_text(json.dumps({
        "assemblyId": "asm-NEW", "reportKeys": ["p/rep/20261001-20261101/asm-NEW/rep-00001.csv"],
        "billingPeriod": {"start": "20261001T000000.000Z"}}))
    with Workspace([tmp_path / "cur"]) as ws:
        ing = [r for r in run_doctor(ws).results if r.check_id == "CL-ING-003"]
    assert any(r.status == "FAIL" and "asm-OLD/rep-00001.csv" in r.message for r in ing)
    assert any(r.status == "PASS" for r in ing)  # the current assembly itself is complete


def test_d2_two_azure_runs_with_manifests_fail(tmp_path):
    hdr = ["SubscriptionId", "MeterCategory", "MeterSubCategory", "CostInBillingCurrency", "BillingCurrency", "Date",
           "ChargeType"]
    for run, cost in (("run-a", "10"), ("run-b", "11")):
        d = tmp_path / "exp" / "20261001-20261031" / run
        write_csv(d / "part_0_0001.csv", hdr, [["s", "VM", "D", cost, "USD", "10/01/2026", "Purchase"]])
        (d / "manifest.json").write_text(json.dumps({
            "exportConfig": {"exportName": "daily", "resourceId": "/subscriptions/s",
                             "timePeriod": {"from": "2026-10-01T00:00:00Z"}},
            "blobs": [{"blobName": f"daily/20261001-20261031/{run}/part_0_0001.csv", "dataRowCount": 1}]}))
    with Workspace([tmp_path / "exp"]) as ws:
        msgs = [r.message for r in run_doctor(ws).results if r.check_id == "CL-ING-003" and r.status == "FAIL"]
    assert any("2 deliveries" in m for m in msgs)


def test_d3_missing_provider_is_an_export_gap_not_discontinued_spend(demo, tmp_path):
    root, _ = demo
    m2 = tmp_path / "m2"
    shutil.copytree(root / "m2", m2)
    shutil.rmtree(m2 / "azure-actual")
    shutil.rmtree(m2 / "azure-amortized")
    with Workspace([root / "m1", m2]) as ws:
        br = next(b for b in V.compute(ws, "2026-09", "2026-10") if b.cost_basis == "billed")
        assert statuses(ws, "CL-ACC-008") == ["WARN", "WARN"]  # azure actual and amortized
    assert br.buckets["coverage"] == Decimal("-2687.949999978")
    assert br.buckets["discontinued"] == 0
    assert br.pct_change() is None
    assert br.narrative().startswith("NOT COMPARABLE AS-IS")
    assert sum(br.buckets.values()) == br.delta


def test_d4_unwritable_output_is_actionable(tmp_path, capsys):
    p = write_csv(tmp_path / "x.csv", AWS_COLS, [aws_row()])
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("a regular file cannot hold an output file, on any OS")
    assert main(["audit", str(p), "-o", str(blocker / "x.html"), "-q"]) == 2
    err = capsys.readouterr().err
    assert "cannot access" in err and "bug" not in err


def test_d5_header_only_input_fails(tmp_path):
    p = write_csv(tmp_path / "x.csv", AWS_COLS, [])
    with Workspace([p]) as ws:
        assert "FAIL" in statuses(ws, "CL-ING-001")


def test_d6_bigquery_parquet_with_nested_structs(tmp_path):
    pq = tmp_path / "gcp.parquet"
    duckdb.sql(f"""COPY (SELECT '01A-B' AS billing_account_id,
      {{'id': 'svc', 'description': 'Compute Engine'}} AS service, {{'id': 'sku', 'description': 'N2'}} AS sku,
      TIMESTAMP '2026-10-05 00:00:00' AS usage_start_time, {{'id': 'proj-1', 'name': 'proj-1'}} AS project,
      [{{'key': 'team', 'value': 'data'}}] AS labels, {{'region': 'us-central1'}} AS location,
      10.25::DOUBLE AS cost, 'USD' AS currency,
      [{{'name': 'SUD', 'amount': -1.25::DOUBLE}}, {{'name': 'Promo', 'amount': -0.5::DOUBLE}}] AS credits,
      {{'month': '202610'}} AS invoice) TO '{pq}' (FORMAT parquet)""")
    with Workspace([pq]) as ws:
        r = ws.rows("SELECT * FROM valid")[0]
    assert ws.sources[0].mapping.provider == "gcp"
    assert r["cost"] == Decimal("8.5") and r["gross_cost"] == Decimal("10.25")
    assert r["billing_period"] == "2026-10" and r["sub_account_id"] == "proj-1" and json.loads(r["tags"]) == {
        "team": "data"}


def test_d7_arn_resource_names_and_tag_values_do_not_leak(tmp_path):
    rows = [aws_row(lid="1", line_item_resource_id="arn:aws:ec2:us-east-1:111122223333:volume/Vol-AcmeProd-01"),
            aws_row(lid="2", line_item_line_item_description=(
                "moved from arn:aws:ec2:us-east-1:111122223333:volume/vol-acmeprod-01 earlier")),
            aws_row(lid="3", line_item_line_item_description="owner team ACME-PAYMENTS-7 approved"),
            aws_row(lid="4", resource_tags='{"user_team": "acme-payments-7"}')]
    p = write_csv(tmp_path / "in" / "x.csv", AWS_COLS, rows)
    redact([str(p)], str(tmp_path / "out"), key_file=str(tmp_path / "k"))
    text = "".join(x.read_text() for x in (tmp_path / "out").iterdir()).lower()
    for secret in ("acmeprod", "acme-payments-7", "111122223333", "arn:aws"):
        assert secret not in text, secret


@pytest.mark.parametrize("col,expected", [
    ("PartNumber", "SAFE"), ("project.ancestors", "DROP"), ("adjustment_info.mode", "SAFE"),
    ("x_CostCategories", "TAGS"), ("x_Discounts", "TEXT"), ("AccountOwnerId", "PSEUDO"),
    ("CustomerTenantId", "PSEUDO"), ("InvoiceSectionName", "PSEUDO"),
])
def test_d7_registry_covers_real_columns(col, expected):
    assert classify(col) == expected


def test_d8_redaction_scales_with_many_identifiers(tmp_path):
    """10k rows of the 35-column benchmark shape: ~36 s before the token index, ~2 s after."""
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "cl_bench", Path(__file__).resolve().parents[1] / "scripts" / "benchmark.py")
    bench = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bench)
    data = tmp_path / "in" / "cur.csv.gz"
    data.parent.mkdir()
    bench.generate(data, 10_000)
    start = time.perf_counter()
    redact([str(data)], str(tmp_path / "out"), key_file=str(tmp_path / "k"))
    assert time.perf_counter() - start < 15


def test_d9_azure_basis_matches_whole_words():
    assert basis_from_name("contractual-exports/usage.csv") is None
    assert basis_from_name("exports/ActualCost_202610.csv") == "billed"
    assert basis_from_name("azure-amortized/part_1.csv") == "amortized"


def test_d10_focus_charge_class_is_1_0():
    assert focus.detect_version(Columns.from_names(["BilledCost", "EffectiveCost", "ChargePeriodStart",
                                                    "ChargeClass"])) == "1.0"


def test_d11_period_reconcile_names_every_invoice(tmp_path):
    rows = [aws_row(lid="1", bill_invoice_id="AWS-1", line_item_unblended_cost="100"),
            aws_row(lid="2", bill_invoice_id="MKT-1", line_item_unblended_cost="40")]
    p = write_csv(tmp_path / "x.csv", AWS_COLS, rows)
    with Workspace([p]) as ws:
        r = reconcile(ws, [Expectation(Decimal("100"), "USD", period="2026-07", provider="aws")])[0]
    assert r.difference == Decimal(40)
    assert any("spans 2 invoices" in n for n in r.notes)
    assert not any("no tax lines" in n for n in r.notes)  # tax cannot explain an export that is higher


def test_concurrent_key_creation_yields_one_key(tmp_path):
    path = tmp_path / "keys" / "redaction.key"
    keys: list[bytes] = []
    threads = [threading.Thread(target=lambda: keys.append(load_key(path, create=True)[0])) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(keys)) == 1 and len(keys) == 8


def test_owner_label_never_calls_an_account_an_owner():
    assert owner_label({"status": "account", "value": "prod"}).startswith("unattributed")
    assert owner_label({"status": "tag", "key": "team", "value": "web"}) == "team=web"


# ---- coverage evidence for the schema matrix (docs/validation.md) ---------------------------------
CUR2_RI_SP = ["identity_line_item_id", "bill_invoice_id", "bill_payer_account_id", "bill_billing_period_start_date",
              "line_item_usage_account_id", "line_item_line_item_type", "line_item_usage_start_date",
              "line_item_product_code", "line_item_usage_type", "line_item_resource_id", "line_item_unblended_cost",
              "line_item_currency_code", "reservation_reservation_arn", "reservation_effective_cost",
              "reservation_unused_recurring_fee", "reservation_unused_amortized_upfront_fee_for_billing_period",
              "savings_plan_savings_plan_effective_cost", "savings_plan_total_commitment_to_date",
              "savings_plan_used_commitment"]


def test_aws_reserved_instances_and_savings_plans_billed_and_amortized(tmp_path):
    def r(i, t, cost, **kw):
        d = dict(identity_line_item_id=str(i), bill_invoice_id="I", bill_payer_account_id="111122223333",
                 bill_billing_period_start_date="2026-10-01T00:00:00Z", line_item_usage_account_id="444455556666",
                 line_item_line_item_type=t, line_item_usage_start_date="2026-10-01T00:00:00Z",
                 line_item_product_code="AmazonEC2", line_item_usage_type="BoxUsage", line_item_resource_id="i-1",
                 line_item_unblended_cost=cost, line_item_currency_code="USD")
        d.update(kw)
        return d
    rows = [
        r(1, "RIFee", "100", reservation_reservation_arn="arn:ri", reservation_unused_recurring_fee="20",
          reservation_unused_amortized_upfront_fee_for_billing_period="5"),
        r(2, "Fee", "1200", reservation_reservation_arn="arn:ri"),
        r(3, "DiscountedUsage", "0", reservation_effective_cost="80"),
        r(4, "SavingsPlanCoveredUsage", "50", savings_plan_savings_plan_effective_cost="30"),
        r(5, "SavingsPlanNegation", "-50"),
        r(6, "SavingsPlanRecurringFee", "40", savings_plan_total_commitment_to_date="40",
          savings_plan_used_commitment="30"),
        r(7, "Usage", "10"), r(8, "Tax", "5"), r(9, "Credit", "-3"),
    ]
    p = write_csv(tmp_path / "x.csv", CUR2_RI_SP, rows)
    with Workspace([p]) as ws:
        billed, amortized = ws.con.execute("SELECT sum(cost), sum(effective_cost) FROM valid").fetchone()
    assert billed == Decimal(1352)  # what the invoice charges
    assert amortized == Decimal(157)  # 25 unused RI + 80 RI usage + 30 SP usage + 10 unused SP + 10 + 5 - 3


def test_azure_mca_camel_case_headers(tmp_path):
    hdr = ["billingAccountId", "subscriptionId", "subscriptionName", "date", "meterCategory", "meterSubCategory",
           "meterName", "quantity", "costInBillingCurrency", "billingCurrency", "chargeType", "pricingModel",
           "invoiceId", "resourceId", "tags"]
    rows = [["ba", "sub", "s", "10/01/2026", "Virtual Machines", "D", "D4", "24", "4.608", "EUR", "Usage",
             "OnDemand", "G001", "/subscriptions/sub/resourceGroups/rg/providers/x/vm1", '"env": "prod"']]
    p = write_csv(tmp_path / "actualcost.csv", hdr, rows)
    with Workspace([p]) as ws:
        r = ws.rows("SELECT * FROM valid")[0]
    assert (r["provider"], r["cost"], r["currency"], r["billing_period"], r["invoice_id"]) == (
        "azure", Decimal("4.608"), "EUR", "2026-10", "G001")
    assert json.loads(r["tags"]) == {"env": "prod"}


def test_cur2_parquet_map_tags(tmp_path):
    pq = tmp_path / "cur.parquet"
    duckdb.sql(f"""COPY (SELECT '444455556666' AS line_item_usage_account_id, 'Usage' AS line_item_line_item_type,
                 'AmazonEC2' AS line_item_product_code, 'USD' AS line_item_currency_code,
                 TIMESTAMP '2026-10-01' AS line_item_usage_start_date, 1.25::DOUBLE AS line_item_unblended_cost,
                 MAP {{'user_team': 'web'}} AS resource_tags) TO '{pq}' (FORMAT parquet)""")
    with Workspace([pq]) as ws:
        r = ws.rows("SELECT cost, tags FROM valid")[0]
    assert r["cost"] == Decimal("1.25") and json.loads(r["tags"]) == {"user_team": "web"}
