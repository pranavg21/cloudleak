"""Invoice identity, input precision and dataset-grain regressions on synthetic rows."""
from decimal import Decimal

import duckdb
import pytest

from cloudleak.cli import main
from cloudleak.engine import Workspace
from cloudleak.errors import InputError
from cloudleak.providers.base import Columns
from cloudleak.providers.focus import detect_version
from cloudleak.reconcile import Expectation, reconcile
from tests.conftest import write_csv

ROW = dict(BilledCost="10", InvoiceId="I", InvoiceIssuerName="Issuer A", ProviderName="Amazon",
           BillingAccountId="A", BillingPeriodStart="2025-01-01", ChargePeriodStart="2025-01-03",
           BillingCurrency="USD", ChargeCategory="Usage")


def charges(tmp_path, rows):
    return write_csv(tmp_path / "charges.csv", list(dict.fromkeys(k for r in rows for k in r)), rows)


def invoice(total="10", **scope):
    return Expectation(Decimal(total), "USD", "I", **scope)


@pytest.mark.parametrize("override,diagnostic", [
    ({"BilledCost": ""}, "amount"), ({"BilledCost": "1,25"}, "amount"),
    ({"BilledCost": "1.00000000000049"}, "amount"), ({"BilledCost": "1e-13"}, "amount"),
    ({"BillingCurrency": ""}, "currency"), ({"BillingCurrency": "?"}, "currency"),
    ({"InvoiceId": ""}, "invoice_id")])
def test_scoped_invalid_row_prevents_matching_subset(tmp_path, capsys, override, diagnostic):
    p = charges(tmp_path, [ROW, ROW | override])
    assert main(["reconcile", str(p), "--invoice-id", "I", "--total", "10", "--currency", "USD"]) == 2
    assert diagnostic in capsys.readouterr().err


def test_missing_invoice_scope_in_unrelated_month_or_known_currency(tmp_path):
    p = charges(tmp_path, [ROW, ROW | {"BillingPeriodStart": "2025-02-01", "InvoiceId": ""},
                           ROW | {"BillingCurrency": "EUR", "BilledCost": "?"}])
    with Workspace([p]) as ws:
        result, = reconcile(ws, [invoice()])
    assert result.rows == 1 and result.difference == 0
    assert result.excluded_other_currency == [{"currency": "EUR", "rows": 1, "amount": None}]


def test_issuer_and_account_scope(tmp_path):
    p = charges(tmp_path, [ROW, ROW | {"BilledCost": "3", "InvoiceIssuerName": "Issuer B"},
                           ROW | {"BilledCost": "2", "BillingAccountId": "B"}])
    with Workspace([p]) as ws:
        with pytest.raises(InputError, match="one issuer"):
            reconcile(ws, [invoice("15")])
        with pytest.raises(InputError, match="billing accounts"):
            reconcile(ws, [invoice("12", issuer="Issuer A")])
        result, = reconcile(ws, [invoice(issuer="Issuer A", billing_account="A")])
        assert result.rows == 1 and result.difference == 0


def test_one_issuer_can_invoice_several_service_providers(tmp_path):
    p = charges(tmp_path, [ROW, ROW | {"ProviderName": "Google", "BilledCost": "2"}])
    with Workspace([p]) as ws:
        result, = reconcile(ws, [invoice("12")])
    assert result.difference == 0 and result.providers == ["aws", "gcp"]


def test_period_and_duplicate_expectations(tmp_path):
    distinct = charges(tmp_path, [ROW, ROW | {"InvoiceId": "J"}])
    month = Expectation(Decimal("20"), "USD", period="2025-01")
    with Workspace([distinct]) as ws, pytest.raises(InputError, match=r"multiple.*invoices"):
        reconcile(ws, [month])
    one = charges(tmp_path, [ROW])
    with Workspace([one]) as ws:
        with pytest.raises(InputError, match="Duplicate"):
            reconcile(ws, [invoice(), invoice("99")])
        with pytest.raises(InputError, match="overlap"):
            reconcile(ws, [month, invoice()])


def test_invoice_detail_and_usage_markers(tmp_path):
    p = charges(tmp_path, [ROW | {"InvoiceDetailId": "ID"}])
    detail = write_csv(tmp_path / "detail.csv", [*ROW, "InvoiceIssueStatus"], [ROW | {"InvoiceIssueStatus": "Issued"}])
    with Workspace([p]) as ws:
        assert ws.sources[0].mapping.focus_version == "1.4"
        assert reconcile(ws, [invoice()])[0].export_total == 10
    with pytest.raises(InputError, match="invoice-detail"):
        Workspace([p, detail])


def test_colliding_cost_column_names_cannot_choose_the_amount(tmp_path):
    p = charges(tmp_path, [ROW | {"billed_cost": "9"}])
    with Workspace([p]) as ws, pytest.raises(InputError, match="Ambiguous invoice columns"):
        reconcile(ws, [invoice()])


def test_full_precision_arithmetic_and_formatting(tmp_path):
    p = charges(tmp_path, [ROW | {"BilledCost": "12345678901234567890123456789012e-12"},
                           ROW | {"BilledCost": "0.0000000000010000", "ChargeCategory": "Tax"}])
    with Workspace([p]) as ws:
        result, = reconcile(ws, [invoice("12345678901234567890.12345679")])
    assert result.to_dict()["export_total"] == "12345678901234567890.123456789013"
    assert result.to_dict()["difference"] == "-0.000000000987"


@pytest.mark.parametrize("storage,amount,cent_loss", [("DOUBLE", "9007199254740992.01", True),
                                                      ("FLOAT", "262144.01", True),
                                                      ("DOUBLE", "9.00", False),
                                                      ("DECIMAL(38,12)", "9007199254740992.01", False)])
def test_parquet_billed_source_precision(tmp_path, storage, amount, cent_loss):
    path = tmp_path / "billed.parquet"
    with duckdb.connect() as db:
        db.sql(f"""SELECT '{amount}'::{storage} AS BilledCost, 'I' AS InvoiceId,
                          'USD' AS BillingCurrency, 'Usage' AS ChargeCategory""").write_parquet(str(path))
    with Workspace([path]) as ws:
        if cent_loss:
            with pytest.raises(InputError, match="resolve individual cents"):
                reconcile(ws, [invoice(amount)])
        else:
            result, = reconcile(ws, [invoice(amount)])
            assert result.difference == 0
            assert any("floating-point" in s for s in result.notes) == (storage == "DOUBLE")


def test_azure_extension_and_header_lower_bounds(tmp_path):
    row = {k: v for k, v in ROW.items() if k != "InvoiceId"}
    p = charges(tmp_path, [row | {"ProviderName": "Microsoft", "x_InvoiceId": "I",
                                  "ChargeClass": "", "BillingAccountType": "", "SubAccountType": ""}])
    with Workspace([p]) as ws:
        assert ws.sources[0].to_dict()["focus_version"] == "1.0"
        assert reconcile(ws, [invoice()])[0].difference == 0
    for marker, minimum in [("SkuMeter", "1.1"), ("InvoiceId", "1.2"), ("ContractApplied", "1.3"),
                            ("CommitmentProgramEligibilityDetails", "1.4")]:
        assert detect_version(Columns.from_names(["BilledCost", marker])) == minimum
