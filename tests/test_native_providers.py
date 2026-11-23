"""Synthetic native export regressions, including exact money and ambiguous export scopes."""

from decimal import Decimal

import duckdb
import pytest

from cloudleak.engine import Workspace
from cloudleak.errors import InputError
from tests.conftest import write_csv

GCP = {"cost": "12", "service.description": "Cloud", "sku.description": "Usage", "billing_account_id": "A",
       "invoice.month": "202502", "usage_start_time": "2025-01-03", "currency": "USD"}
AZURE = {"CostInBillingCurrency": "70", "MeterCategory": "Compute", "ChargeType": "Usage",
         "BillingCurrency": "EUR", "Date": "2025-02-01"}
AWS = {"line_item_unblended_cost": "10", "line_item_usage_account_id": "A", "line_item_currency_code": "USD"}


def export(path, *rows):
    return write_csv(path, list(dict.fromkeys(k for row in rows for k in row)), list(rows))


@pytest.mark.parametrize("detailed", [False, True])
def test_nested_gcp_preserves_credit_digits_and_invoice_month(tmp_path, detailed):
    path = tmp_path / "billing.parquet"
    resource = ", {'name':'vm','global_name':'global-vm'} AS resource" if detailed else ""
    with duckdb.connect() as db:
        db.sql(f"""SELECT 'A' AS billing_account_id, {{'id':'P'}} AS project,
                     {{'description':'Compute'}} AS service, {{'id':'K','description':'CPU'}} AS sku,
                     {{'month':'202502'}} AS invoice, DATE '2025-01-31' AS usage_start_time,
                     'USD' AS currency, 9007199254740993.000000000001::DECIMAL(38,12) AS cost,
                     [{{'name':'discount','amount':-9007199254740992.991234567890::DECIMAL(38,12)}}] AS credits
                     {resource}""").write_parquet(str(path))
    with Workspace([path]) as ws:
        assert ws.sources[0].mapping.dataset == ("gcp_detailed" if detailed else "gcp_standard")
        assert ws.con.execute("""
            SELECT cost, billing_period, month(charge_start), invoice_id, resource_id, sub_account_id FROM items
        """).fetchone() == (Decimal("0.008765432111"), "2025-02", 1, None, "global-vm" if detailed else None, "P")


@pytest.mark.parametrize("credit", ['broken', '{}', 'null', '[{}]', '[null]', '[3]', '[{"amount":null}]',
                                    '[{"amount":""}]', '[{"amount":false}]', '[{"amount":"bad"},{"amount":-1}]',
                                    '[{"amount":1},]', '[{"amount":1,}]', '[{"amount":1,"adj":NaN}]',
                                    '[{"amount":-1,"amount":-2}]', '[{"amount":0.1000000000001}]'])
def test_corrupt_credit_fails_closed(tmp_path, credit):
    path = export(tmp_path / "gcp.csv", dict(GCP, credits=credit))
    with pytest.raises(InputError):
        Workspace([path])


def test_flat_credit_literal_remains_exact(tmp_path):
    credit = r'[{"amount":-9007199254740992.991234567890,"name":"incl. \"amount\": -987"}]'
    path = export(tmp_path / "gcp.csv", dict(GCP, cost="9007199254740993.000000000001", credits=credit),
                  dict(GCP, credits=""), dict(GCP, credits="[]"))
    with Workspace([path]) as ws:
        assert ws.scalar("SELECT sum(cost) FROM items") == Decimal("24.008765432111")


def test_flattened_credit_and_month_validation(tmp_path):
    path = tmp_path / "gcp.csv"
    export(path, {**GCP, "credits.amount": "bad"})
    with pytest.raises(InputError, match="credit"):
        Workspace([path])
    export(path, {k: v for k, v in GCP.items() if k != "invoice.month"})
    with Workspace([path]) as ws:
        assert ws.scalar("SELECT billing_period FROM items") is None
    export(path, {**GCP, "invoice.month": "202513"})
    with pytest.raises(InputError, match=r"invoice\.month"):
        Workspace([path])


def test_reject_nested_and_flat_collision(tmp_path):
    path = tmp_path / "collision.parquet"
    with duckdb.connect() as db:
        db.sql("SELECT {'description':'A'} AS service, 'B' AS service_description, 1 AS cost").write_parquet(str(path))
    with pytest.raises(InputError, match="Ambiguous GCP field"):
        Workspace([path])


def test_alternative_gcp_views_and_same_view_duplicates(tmp_path):
    standard = export(tmp_path / "standard.csv", GCP, GCP)
    detail = export(tmp_path / "detail.csv", {**GCP, "resource.name": "vm"})
    with Workspace([standard]) as ws:
        assert ws.scalar("SELECT sum(cost) FROM items") == 24
    with pytest.raises(InputError, match="Overlapping billing scopes"):
        Workspace([standard, detail])
    with Workspace([standard, detail], independent_inputs=True) as ws:
        assert set(ws.source_ids_for(standard)).isdisjoint(ws.source_ids_for(detail))
        assert len(ws.source_ids_for(standard)) == len(ws.source_ids_for(detail)) == 1
    for disjoint in ({"billing_account_id": "B"}, {"invoice.month": "202503"}):
        export(detail, {**GCP, "resource.name": "vm", **disjoint})
        with Workspace([standard, detail]) as ws:
            assert ws.scalar("SELECT count(*) FROM items") == 3


@pytest.mark.parametrize("provider", ["gcp", "aws"])
def test_mixed_focus_native_ledger_refused(tmp_path, provider):
    row = GCP if provider == "gcp" else {**AWS, "bill_payer_account_id": "A", "bill_invoice_id": "issue",
                                          "bill_invoicing_entity": "AWS", "line_item_usage_start_date": "2025-02-01"}
    native = export(tmp_path / "native.csv", row)
    focus_row = {"BilledCost": "12", "BillingCurrency": "USD", "BillingPeriodStart": "2025-02-01",
                 "ChargePeriodStart": "2025-02-01", "ChargeCategory": "Usage", "BillingAccountId": "A",
                 "ProviderName": provider, "InvoiceId": "issue", "InvoiceIssuerName": provider}
    focus = export(tmp_path / "focus.csv", focus_row)
    with pytest.raises(InputError, match="Overlapping billing scopes"):
        Workspace([native, focus])
    for disjoint in ({"BillingAccountId": "B"}, {"BillingPeriodStart": "2025-03-01"}):
        export(focus, {**focus_row, **disjoint})
        with Workspace([native, focus]) as ws:
            assert ws.scalar("SELECT count(*) FROM items") == 2


def test_azure_basis_all_path_forms(tmp_path):
    root = tmp_path / "exports"
    actual = export(root / "actual" / "rows.csv", AZURE)
    amortized_row = dict(AZURE, CostInBillingCurrency="35")
    amortized = export(root / "amortized" / "rows.csv", amortized_row, amortized_row)
    for paths in ([root], [actual.parent, amortized.parent], [actual, amortized]):
        with Workspace(paths) as ws:
            assert ws.con.execute("""SELECT cost_basis, count(*), sum(cost) FROM items GROUP BY 1 ORDER BY 1"""
                                  ).fetchall() == [("amortized", 2, 70), ("billed", 1, 70)]


def test_azure_conflicting_basis_is_unknown(tmp_path):
    path = export(tmp_path / "actual" / "part.csv", dict(AZURE, ChargeType="UnusedReservation"))
    with Workspace([path]) as ws:
        assert ws.scalar("SELECT cost_basis FROM items") == "unknown"
        assert "conflicting" in ws.sources[0].basis_evidence


def test_azure_list_cost_in_billing_currency(tmp_path):
    row = dict(AZURE, PricingCurrency="USD", PayGPrice="20", Quantity="50", ExchangeRatePricingToBilling="0.9")
    path = export(tmp_path / "actual.csv", dict(row, paygCostInBillingCurrency="899.99"), row,
                  dict(row, ExchangeRatePricingToBilling=""), dict(row, PricingCurrency="EUR"),
                  dict(row, PricingCurrency="USD", ExchangeRatePricingToBilling="bad"))
    with Workspace([path]) as ws:
        assert ws.con.execute("SELECT list_cost FROM items ORDER BY source_row").fetchall() == [
            (Decimal("899.99"),), (900,), (None,), (1000,), (None,)]


def test_aws_effective_cost_uses_required_operands(tmp_path):
    path = tmp_path / "cur.csv"
    export(path, dict(AWS, line_item_line_item_type="DiscountedUsage", reservation_effective_cost="3.75"))
    with Workspace([path]) as ws:
        assert ws.scalar("SELECT effective_cost FROM items") == Decimal("3.75")
        assert ws.sources[0].mapping.capabilities["effective_cost"]
    export(path,
           dict(AWS, line_item_line_item_type="SavingsPlanCoveredUsage",
                savings_plan_savings_plan_effective_cost="5.32"),
           dict(AWS, line_item_line_item_type="SavingsPlanNegation"),
           dict(AWS, line_item_line_item_type="SavingsPlanRecurringFee",
                savings_plan_total_commitment_to_date="6", savings_plan_used_commitment="5.32"),
           dict(AWS, line_item_line_item_type="SavingsPlanRecurringFee", savings_plan_total_commitment_to_date="6"),
           dict(AWS, line_item_line_item_type="RIFee", reservation_unused_recurring_fee="3"))
    with Workspace([path]) as ws:
        assert ws.con.execute("SELECT effective_cost FROM items ORDER BY source_row").fetchall() == [
            (Decimal("5.32"),), (0,), (Decimal("0.68"),), (None,), (None,)]
        assert not ws.sources[0].mapping.capabilities["effective_cost"]
