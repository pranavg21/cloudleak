from __future__ import annotations

from decimal import Decimal

import pytest

from cloudleak import variance as V
from cloudleak.diff import diff
from cloudleak.engine import Workspace
from cloudleak.errors import InputError
from cloudleak.reconcile import Expectation, load_invoices, reconcile
from tests.conftest import AWS_COLS, aws_month, aws_row, write_csv


def test_bridge_closes_exactly(demo_ws):
    b, c = V.choose_periods(demo_ws, None, None)
    assert (b, c) == ("2026-09", "2026-10")
    for br in V.compute(demo_ws, b, c):
        assert sum(br.buckets.values()) == br.current_total - br.base_total
        assert br.narrative()


def test_bridge_buckets(demo_ws):
    billed = next(x for x in V.compute(demo_ws, "2026-09", "2026-10") if x.cost_basis == "billed")
    assert billed.buckets["new"] == Decimal("12.5")           # GCP late usage, invoiced into October
    assert billed.buckets["credits"] == Decimal("-42.92")    # AWS refund -42.17 and GCP credits -0.75
    assert billed.buckets["tax"] > 0
    assert billed.pct_change() is not None and billed.comparable_days


def test_partial_month_is_normalized_per_day(tmp_path):
    full = aws_month([aws_row(line_item_unblended_cost="10")], days=30, month="2026-06")
    part = aws_month([aws_row(line_item_unblended_cost="10")], days=10, month="2026-07", invoice="")
    write_csv(tmp_path / "x.csv", AWS_COLS, full + part)
    with Workspace([tmp_path]) as ws:
        br = V.compute(ws, "2026-06", "2026-07")[0]
    assert br.delta == Decimal(-200)
    assert not br.comparable_days
    assert br.pct_change() == "0.0"  # same daily run rate: never report a fake -66.7% drop
    assert "per day" in br.narrative()


def test_rate_and_volume_split(tmp_path):
    m1 = aws_month([aws_row(line_item_usage_amount="10", line_item_unblended_cost="1.0")], days=30, month="2026-06")
    m2 = aws_month([aws_row(line_item_usage_amount="20", line_item_unblended_cost="1.5")], days=31, month="2026-07")
    write_csv(tmp_path / "x.csv", AWS_COLS, m1 + m2)
    with Workspace([tmp_path]) as ws:
        br = V.compute(ws, "2026-06", "2026-07")[0]
    # base unit cost 0.1; volume 620 - 300 = 320 units -> +32.0; total delta 46.5 - 30 = 16.5 -> rate -15.5
    assert br.buckets["volume"] == Decimal("32")
    assert br.buckets["rate"] == Decimal("-15.5")


def test_variance_two_paths(demo):
    root, _ = demo
    with Workspace([root / "m1", root / "m2"]) as ws:
        ids1 = [s.file.source_id for s in ws.sources if "2026-09" in s.file.path.name]
        ids2 = [s.file.source_id for s in ws.sources if s.file.source_id not in ids1]
        assert V.choose_periods(ws, None, None, ids1, ids2) == ("2026-09", "2026-10")


def test_variance_needs_two_periods(tmp_path):
    p = write_csv(tmp_path / "x.csv", AWS_COLS, [aws_row()])
    with Workspace([p]) as ws, pytest.raises(InputError, match="two billing periods"):
        V.choose_periods(ws, None, None)


INVOICE_COLS = ["InvoiceId", "BillingCurrency", "BilledCost", "EffectiveCost", "ChargeCategory", "ChargePeriodStart",
                "ServiceName", "ResourceId", "ProviderName"]


@pytest.fixture
def invoice_file(tmp_path):
    rows = [["DEMO-1042", "USD", "24.75", "24.75", "Usage", "2026-07-01", "Storage", "disk-17", "Synthetic"],
            ["DEMO-1043", "USD", "90", "90", "Usage", "2026-07-01", "Storage", "disk-51", "Synthetic"],
            ["DEMO-1042", "USD", "-4.75", "-4.75", "Credit", "2026-07-02", "Storage", "disk-17", "Synthetic"],
            ["DEMO-1042", "USD", "2", "2", "Usage", "2026-07-03", "Storage", "snapshot-11", "Synthetic"],
            ["DEMO-1042", "EUR", "1", "1", "Usage", "2026-07-03", "Storage", "x", "Synthetic"]]
    return write_csv(tmp_path / "inv.csv", INVOICE_COLS, rows)


def test_reconcile_exact_match(invoice_file):
    with Workspace([invoice_file]) as ws:
        r = reconcile(ws, [Expectation(Decimal("22.00"), "USD", "DEMO-1042")])[0]
    assert r.status == "not_conclusive" and r.difference == 0 and r.rows == 3
    assert r.to_dict()["arithmetic_comparison"] == "equal" and r.finality == "unknown"
    assert r.components == {"usage": Decimal("26.75"), "credit": Decimal("-4.75")}
    assert r.excluded_other_currency[0]["currency"] == "EUR"


def test_reconcile_subcent_difference_is_reported(invoice_file):
    with Workspace([invoice_file]) as ws:
        r = reconcile(ws, [Expectation(Decimal("22.001"), "USD", "DEMO-1042")])[0]
        assert r.status == "difference" and r.difference == Decimal("-0.001")
        r2 = reconcile(ws, [Expectation(Decimal("22.001"), "USD", "DEMO-1042")], Decimal("0.01"))[0]
        assert r2.status == "not_conclusive" and r2.to_dict()["arithmetic_comparison"] == "within_tolerance"


def test_reconcile_preliminary_not_conclusive(tmp_path):
    p = write_csv(tmp_path / "x.csv", AWS_COLS, aws_month([aws_row(line_item_unblended_cost="1")], days=5,
                                                          invoice=""))
    with Workspace([p]) as ws:
        r = reconcile(ws, [Expectation(Decimal("5"), "USD", period="2026-07")])[0]
    assert r.status == "not_conclusive" and r.finality == "preliminary_estimate"


def test_reconcile_requires_provider_for_mixed_period(demo_ws):
    with pytest.raises(InputError, match="one issuer"):
        reconcile(demo_ws, [Expectation(Decimal("1"), "USD", period="2026-09")])
    r = reconcile(demo_ws, [Expectation(Decimal("27206.8066200011"), "USD", period="2026-09", provider="aws")])[0]
    assert r.status == "match"


def test_invoice_file_validation(tmp_path):
    bad = tmp_path / "i.csv"
    bad.write_text("total,currency,invoice_id\n1,250.00,USD,X\n")
    with pytest.raises(InputError):
        load_invoices(bad)
    ok = tmp_path / "ok.csv"
    ok.write_text("invoice_id,currency,total\nA,usd,10.5\n")
    assert load_invoices(ok)[0].currency == "USD"


def test_diff_shows_restatement(tmp_path):
    before = aws_month([aws_row(line_item_unblended_cost="10")], days=31, invoice="")
    after = before + [aws_row(lid="sup", line_item_product_code="AWSSupportBusiness", line_item_unblended_cost="100",
                              bill_invoice_id="INV-1"),
                      aws_row(lid="cr", line_item_line_item_type="Credit", line_item_unblended_cost="-15",
                              bill_invoice_id="INV-1")]
    after = [dict(r, bill_invoice_id="INV-1") for r in after]
    a = write_csv(tmp_path / "a" / "x.csv", AWS_COLS, before)
    b = write_csv(tmp_path / "b" / "x.csv", AWS_COLS, after)
    with Workspace([a]) as wa, Workspace([b]) as wb:
        d = diff(wa, wb)
    s = d["scopes"][0]
    assert s["status"] == "changed" and s["delta"] == "85.00"
    assert s["before_finality"] == "unknown" or s["before_finality"] == "preliminary_estimate"
    assert s["after_finality"] == "finalized" and "finalized" in s["note"]
    cats = {c["item"]: c["delta"] for c in s["by_charge_category"]}
    assert cats == {"usage": "100.00", "credit": "-15.00"}
