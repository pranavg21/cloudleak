"""Evidence for the README's "Tested export shapes" table.

Each case is a minimal synthetic export in the provider's documented column layout, reconciled
through the CLI against an independently computed invoice total. Ported from the external
pre-release review's native probes so the table is backed by CI, not by a one-off run.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import duckdb
import pytest

from cloudleak.cli import main

IID = ["--invoice-id", "SYN-1"]
FEB = ["--period", "2025-02"]


def write(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = list(dict.fromkeys(k for row in rows for k in row))
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, cols, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    return path


def reconcile(capsys, paths, scope, total, currency="USD", extra=()):
    capsys.readouterr()
    code = main(["reconcile", *map(str, paths), "--total", total, "--currency", currency, "--format", "json",
                 *scope, *extra])
    out = capsys.readouterr().out
    return code, (json.loads(out)["reconciliation"][0] if out.strip() else None)


AWS = dict(line_item_line_item_type="Usage", line_item_usage_start_date="2025-02-04T18:00:00Z",
           bill_billing_period_start_date="2025-02-01", line_item_unblended_cost="10",
           line_item_currency_code="USD", bill_invoice_id="SYN-1", line_item_usage_account_id="SYN-A",
           bill_payer_account_id="SYN-P")


def test_aws_split_allocation_is_not_added_to_billed_cost(tmp_path, capsys):
    rows = [dict(AWS, split_line_item_split_cost="", line_item_resource_id="parent"),
            dict(AWS, line_item_unblended_cost="0", split_line_item_split_cost="7", line_item_resource_id="c1"),
            dict(AWS, line_item_unblended_cost="0", split_line_item_split_cost="3", line_item_resource_id="c2"),
            *(dict(AWS, line_item_line_item_type=t, line_item_unblended_cost=m)
              for t, m in [("Credit", "-1"), ("Refund", "-2.30"), ("Fee", "0.51")])]
    code, r = reconcile(capsys, [write(tmp_path / "split.csv", rows)], IID, "7.21")
    assert r["difference"] == "0.00" and code in (0, 1)  # 10 - 1 - 2.30 + 0.51; split costs never added


def test_aws_ri_only_and_sp_only_exports(tmp_path, capsys):
    ri = dict(AWS, line_item_line_item_type="DiscountedUsage", line_item_unblended_cost="0",
              reservation_effective_cost="3.75")
    code, r = reconcile(capsys, [write(tmp_path / "ri.csv", [ri])], IID, "0")
    assert r["difference"] == "0.00"
    sp = dict(AWS, line_item_line_item_type="SavingsPlanCoveredUsage", savings_plan_savings_plan_effective_cost="5.32")
    rows = [sp, dict(sp, line_item_line_item_type="SavingsPlanNegation", line_item_unblended_cost="-10",
                     savings_plan_savings_plan_effective_cost=""),
            dict(sp, line_item_line_item_type="SavingsPlanRecurringFee", line_item_unblended_cost="6",
                 savings_plan_used_commitment="5.32", savings_plan_total_commitment_to_date="6")]
    code, r = reconcile(capsys, [write(tmp_path / "sp.csv", rows)], IID, "6")
    assert r["difference"] == "0.00"


def test_aws_legacy_slash_named_cur(tmp_path, capsys):
    legacy = {"bill/InvoiceId": "SYN-1", "lineItem/UsageAccountId": "SYN-A", "lineItem/LineItemType": "Usage",
              "lineItem/UnblendedCost": "3.98", "lineItem/CurrencyCode": "USD",
              "lineItem/UsageStartDate": "2025-02-02T17:00:00Z"}
    code, r = reconcile(capsys, [write(tmp_path / "legacy.csv", [legacy])], IID, "3.98")
    assert r["difference"] == "0.00"


AZ = {"CostInBillingCurrency": "70", "BillingCurrency": "USD", "SubscriptionId": "SYN-A", "MeterCategory": "Compute",
      "Date": "02/01/2025", "ChargeType": "Usage", "InvoiceId": "SYN-1"}


@pytest.mark.parametrize("form", ["parent", "directories", "files"])
def test_azure_actual_and_amortized_stay_separate_however_paths_are_given(tmp_path, capsys, form):
    a = write(tmp_path / "azure" / "actual" / "part.csv", [AZ])
    b = write(tmp_path / "azure" / "amortized" / "part.csv", [dict(AZ, CostInBillingCurrency="35")] * 2)
    paths = {"parent": [tmp_path / "azure"], "directories": [a.parent, b.parent], "files": [a, b]}[form]
    code, r = reconcile(capsys, paths, IID, "70")
    assert r["export_total"] == "70.00" and r["difference"] == "0.00"


@pytest.mark.parametrize("name,row,scope", [
    ("ea", {"PreTaxCost": "7.25", "Currency": "USD", "SubscriptionGuid": "SYN-A", "MeterCategory": "Compute",
            "Date": "02/01/2025", "ChargeType": "Usage"}, FEB),
    ("mosp", {"PreTaxCost": "7.25", "Currency": "USD", "SubscriptionGuid": "SYN-A", "MeterCategory": "Compute",
              "Date": "02/01/2025", "ChargeType": "Usage", "InvoiceId": "SYN-1"}, IID),
    ("mpa", {"CustomerName": "Synthetic", "CustomerTenantId": "SYN-T", "CostInBillingCurrency": "7.25",
             "BillingCurrency": "USD", "MeterCategory": "Compute", "ChargeType": "Usage", "Date": "2025-02-01",
             "InvoiceId": "SYN-1", "SubscriptionId": "SYN-S"}, IID),
])
def test_azure_ea_mosp_and_mpa_cost_management_fields(tmp_path, capsys, name, row, scope):
    code, r = reconcile(capsys, [write(tmp_path / name / "actual.csv", [row])], scope, "7.25")
    assert r["difference"] == "0.00"


def test_azure_partner_center_invoice_is_not_read_as_cost_management(tmp_path, capsys):
    partner = {"CustomerId": "SYN-T", "MeterName": "Compute", "BillingPreTaxTotal": "7.50", "BillingCurrency": "USD",
               "UsageDate": "2025-02-01", "SubscriptionId": "SYN-A", "ChargeType": "Usage", "InvoiceNumber": "SYN-1"}
    code, r = reconcile(capsys, [write(tmp_path / "pc" / "actual.csv", [partner])], IID, "7.50")
    assert code == 2 and r is None  # unsupported: refused, never silently reconciled


GCP = {"billing_account_id": "SYN-P", "project.id": "SYN-A", "service.description": "Compute Engine",
       "sku.description": "Synthetic SKU", "currency": "USD", "cost": "10.75",
       "usage_start_time": "2025-01-28T23:00:00Z", "invoice.month": "202502",
       "credits": '[{"amount":-1.25},{"amount":-0.50}]', "cost_type": "regular"}


@pytest.mark.parametrize("extra", [{}, {"resource.global_name": "synthetic-resource"}])
def test_gcp_flat_standard_and_detailed_with_late_usage_and_credits(tmp_path, capsys, extra):
    code, r = reconcile(capsys, [write(tmp_path / "gcp.csv", [dict(GCP, **extra)])], FEB, "9.00")
    assert r["export_total"] == "9.00" and r["difference"] == "0.00"  # January usage, February invoice


@pytest.mark.parametrize("detailed", [False, True])
def test_gcp_nested_parquet(tmp_path, capsys, detailed):
    p = tmp_path / f"nested-{detailed}.parquet"
    extra = ", {'name':'SYN','global_name':'SYN-R'} AS resource" if detailed else ""
    duckdb.connect().execute(
        "COPY (SELECT 'SYN-P' billing_account_id, {'id':'SYN-S','description':'Compute Engine'} AS service, "
        "{'id':'SYN-K','description':'SKU'} AS sku, {'id':'SYN-A','name':'Project'} AS project, "
        "{'month':'202502'} AS invoice, TIMESTAMPTZ '2025-01-28 23:00:00+00' AS usage_start_time, "
        "'USD' AS currency, 10.75::DOUBLE AS cost, [{'amount': -1.25::DOUBLE},{'amount':-0.50::DOUBLE}] AS credits, "
        "'regular' AS cost_type" + extra + ") TO '" + str(p).replace("'", "''") + "' (FORMAT parquet)")
    code, r = reconcile(capsys, [p], FEB, "9.00")
    assert r["export_total"] == "9.00" and r["difference"] == "0.00"


def test_gcp_invalid_credit_stops_the_comparison(tmp_path, capsys):
    code, r = reconcile(capsys, [write(tmp_path / "bad.csv", [dict(GCP, credits='[{"amount":"bad"}]')])], FEB, "10.75")
    assert code == 2 and r is None
