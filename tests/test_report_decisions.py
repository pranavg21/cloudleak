"""Decision regressions from synthetic partial and cumulative billing deliveries."""

import json
from decimal import Decimal

import pytest

from cloudleak.cli import main
from cloudleak.engine import Workspace
from cloudleak.rules import run_rules
from tests.conftest import AWS_COLS, aws_row, write_csv

COLS = ["InvoiceId", "BillingCurrency", "BilledCost", "EffectiveCost", "ChargeCategory", "ChargePeriodStart",
        "BillingPeriodStart", "ServiceName", "SubAccountId", "BillingAccountId", "ProviderName",
        "ConsumedQuantity", "ConsumedUnit", "SkuId", "CommitmentDiscountStatus"]


def charge(period="2026-09", **values):
    return {"InvoiceId": "I", "BillingCurrency": "USD", "BilledCost": "10", "EffectiveCost": "10",
            "ChargeCategory": "Usage", "ChargePeriodStart": period + "-01", "BillingPeriodStart": period + "-01",
            "ServiceName": "Storage", "SubAccountId": "sub", "BillingAccountId": "acct",
            "ProviderName": "Synthetic", "ConsumedQuantity": "1", "ConsumedUnit": "unit", "SkuId": "s", **values}


def test_variance_filters_both_snapshots_throughout_report(tmp_path, capsys):
    base = write_csv(tmp_path / "base.csv", COLS, [charge()])
    current = write_csv(tmp_path / "current.csv", COLS, [
        charge(ChargePeriodStart="2026-09-02"), charge("2026-10", BilledCost="13")])
    assert main(["variance", str(base), str(current), "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert [v["total"] for v in report["totals"]] == ["10.00", "13.00"]
    assert [v["days_covered"] for v in report["periods"]] == [1, 1]
    assert report["breakdowns"]["service"][0]["cost"] == "23.00"
    bridge, = report["variance"]
    assert (bridge["base_total"], bridge["current_total"], bridge["delta"]) == ("10.00", "13.00", "3.00")
    assert main(["variance", str(current), str(base), "--base", "2026-09", "--current", "2026-10"]) == 2


@pytest.mark.parametrize("period", ["2026-09", "2026-11", "2026-13"])
def test_variance_rejects_invalid_pair(tmp_path, period, capsys):
    path = write_csv(tmp_path / "export.csv", COLS, [charge(), charge("2026-10")])
    assert main(["variance", str(path), "--base", "2026-09", "--current", period, "--format", "json"]) == 2
    assert not capsys.readouterr().out


def test_variance_preserves_offsetting_drivers_and_absent_currency_coverage(tmp_path, capsys):
    path = write_csv(tmp_path / "export.csv", COLS, [
        charge(), charge("2026-10", ConsumedQuantity="2"), charge(BillingCurrency="EUR")])
    assert main(["variance", str(path), "--format", "json"]) == 0
    eur, usd = json.loads(capsys.readouterr().out)["variance"]
    assert eur["current_days_covered"] is None and eur["percent_change"] is None
    assert "No rows" in eur["narrative"]
    assert usd["delta"] == "0.00"
    effects = {e["driver"]: e["amount"] for e in usd["bridge"]}
    assert effects["volume"] == "10.00" and effects["rate"] == "-10.00"
    assert "usage volume" in usd["narrative"] and "unit rate" in usd["narrative"]


def test_diff_aggregate_reallocation_and_partial_invoice_ids(tmp_path, capsys):
    before = write_csv(tmp_path / "old.csv", COLS, [
        charge(ServiceName="A", BilledCost="7", InvoiceId=""), charge(ServiceName="B", BilledCost="3", InvoiceId="")])
    after = write_csv(tmp_path / "new.csv", COLS, [
        charge(ServiceName="A", BilledCost="3", InvoiceId=""), charge(ServiceName="B", BilledCost="7")])
    cmd = ["diff", str(before), str(after), "--fail-on-change"]
    assert main(cmd) == 1
    text = capsys.readouterr().out
    assert "service A" in text and "service B" in text and "finalized" not in text
    assert main([*cmd, "--format", "json"]) == 1
    result = json.loads(capsys.readouterr().out)
    scope, = result["scopes"]
    assert result["changed_scopes"] == 1 and scope["status"] == "changed" and scope["delta"] == "0.00"
    assert [r["delta"] for r in scope["by_service"]] == ["-4.00", "4.00"]
    assert scope["before_invoiced_rows"] == 0 and scope["after_invoiced_rows"] == 1
    assert scope["after_finality"] == "unknown"


def test_invoice_coverage_only_changes_diff(tmp_path, capsys):
    before = write_csv(tmp_path / "old.csv", COLS, [charge(InvoiceId="")])
    after = write_csv(tmp_path / "new.csv", COLS, [charge()])
    assert main(["diff", str(before), str(after), "--fail-on-change", "--format", "json"]) == 1
    scope, = json.loads(capsys.readouterr().out)["scopes"]
    assert scope["before_finality"] == scope["after_finality"] == "unknown"
    assert "finalized" not in scope["note"]


def test_run_id_includes_normalized_expectations(tmp_path, capsys):
    path = write_csv(tmp_path / "cur.csv", AWS_COLS, [aws_row(line_item_unblended_cost="10")])
    cmd = ["reconcile", str(path), "--invoice-id", "INV-1", "--format", "json"]
    ids = []
    for total, currency, status in (("10", "USD", 0), ("10.0", "usd", 0), ("11", "USD", 1)):
        assert main([*cmd, "--total", total, "--currency", currency]) == status
        run = json.loads(capsys.readouterr().out)["run"]
        assert run["arguments"]["expectations"][0]["currency"] == "USD"
        ids.append(run["report_run_id"])
    assert ids[0] == ids[1] and ids[0] != ids[2]


@pytest.mark.parametrize("provider", ["Amazon Web Services", "Microsoft Azure", "Google Cloud", "Synthetic"])
def test_unused_focus_selects_effective_cost_for_provider_aliases(tmp_path, provider):
    path = write_csv(tmp_path / "focus.csv", COLS, [
        charge(ProviderName=provider, BilledCost="0", EffectiveCost="4.5", CommitmentDiscountStatus="Unused")])
    with Workspace([path]) as ws:
        finding, = run_rules(ws)
    assert finding.rule_id == "CL-SIG-006"
    assert finding.cost_basis == "effective"
    assert finding.observed_cost == finding.observed_effective_cost == Decimal("4.5")
    assert finding.observed_billed_cost == 0


def test_unused_cost_basis_in_html_and_markdown(tmp_path):
    path = write_csv(tmp_path / "focus.csv", COLS, [
        charge(BilledCost="0", EffectiveCost="4.5", CommitmentDiscountStatus="Unused"),
        charge(BilledCost="3", EffectiveCost="", CommitmentDiscountStatus="Unused")])
    html, md = tmp_path / "r.html", tmp_path / "r.md"
    assert main(["audit", str(path), "-o", str(html), "-o", str(md), "--fail-on", "never", "-q"]) == 0
    for text in (html.read_text(), md.read_text()):
        assert "4.50 USD effective" in text and "3.00 USD billed" in text
        assert "7.50 USD" not in text and "Observed cost is what was billed" not in text


def test_variance_exposes_different_billing_accounts(tmp_path, capsys):
    path = write_csv(tmp_path / "focus.csv", COLS, [charge(), charge("2026-10", BillingAccountId="other")])
    assert main(["variance", str(path), "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    bridge, = report["variance"]
    assert bridge["percent_change"] is None and "different billing accounts" in bridge["narrative"]
    assert {row["billing_account_id"] for row in bridge["top_line_changes"]} == {"acct", "other"}


def test_subcent_change_at_full_decimal_precision(tmp_path, capsys):
    high = "123456789012345678901234.00000000000"
    base = write_csv(tmp_path / "old.csv", COLS, [charge(BilledCost=high + "1")])
    after = write_csv(tmp_path / "new.csv", COLS, [charge(BilledCost=high + "2")])
    current = write_csv(tmp_path / "next.csv", COLS, [charge("2026-10", BilledCost=high + "2")])
    assert main(["diff", str(base), str(after), "--format", "json", "--fail-on-change"]) == 1
    diff_result = json.loads(capsys.readouterr().out)
    assert diff_result["scopes"][0]["delta"] == "0.000000000001"
    assert main(["variance", str(base), str(current), "--format", "json"]) == 0
    br, = json.loads(capsys.readouterr().out)["variance"]
    assert br["delta"] == "0.000000000001"


@pytest.mark.parametrize(("column", "items", "dimension"), [
    ("ServiceName", ("A", "B"), "by_service"),
    ("SubAccountId", ("sub A", "sub B"), "by_account"),
    ("ChargeCategory", ("Usage", "Purchase"), "by_charge_category"),
])
def test_diff_uses_each_aggregate_for_change_status(tmp_path, capsys, column, items, dimension):
    def snapshot(name, costs):
        return write_csv(tmp_path / name, COLS, [
            charge(**{column: item, "BilledCost": cost}) for item, cost in zip(items, costs, strict=True)])

    old, new = snapshot("before.csv", ("7", "3")), snapshot("after.csv", ("3", "7"))
    assert main(["diff", str(old), str(new), "--format", "json", "--fail-on-change"]) == 1
    scope, = json.loads(capsys.readouterr().out)["scopes"]
    assert scope["delta"] == "0.00" and scope["before_rows"] == scope["after_rows"] == 2
    assert scope["before_finality"] == scope["after_finality"] and scope["status"] == "changed"
    assert {r["delta"] for r in scope[dimension]} == {"-4.00", "4.00"}
