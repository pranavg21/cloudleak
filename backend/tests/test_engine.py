"""Billing evidence regression tests; sample names are not ground truth about resource state."""

from __future__ import annotations

import csv
import io
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from services.audit_engine import execute_audit_engine, split_usage  # noqa: E402
from services.parsers import UnreadableExportError, parse_and_normalize_csv  # noqa: E402

SAMPLES = pathlib.Path(__file__).resolve().parents[2] / "samples"
AWS_HEADERS = [
    "lineItem/UsageAccountId", "lineItem/ResourceId", "lineItem/ProductCode",
    "lineItem/UsageType", "product/region", "lineItem/UnblendedCost",
    "lineItem/CurrencyCode", "lineItem/LineItemType",
]


def report_for(raw: bytes):
    provider, confidence, df, rejected, currency, capabilities = parse_and_normalize_csv(raw)
    return execute_audit_engine(provider, df, confidence, rejected, currency, capabilities)


def sample(name: str):
    return report_for((SAMPLES / name).read_bytes())


def aws_rows(rows: list[list[str]]) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(AWS_HEADERS)
    writer.writerows(rows)
    return out.getvalue().encode()


def aws_row(resource: str, sku: str, cost: str = "100", *, account: str = "1111", region: str = "us-east-1", service: str = "AmazonEC2", currency: str = "USD", charge: str = "Usage") -> list[str]:
    return [account, resource, service, sku, region, cost, currency, charge]


@pytest.mark.parametrize("filename,provider", [
    ("aws_cur_export.csv", "AWS"),
    ("azure_cost_export.csv", "Azure"),
    ("gcp_billing_export.csv", "GCP"),
])
def test_provider_detection(filename, provider):
    parsed = parse_and_normalize_csv((SAMPLES / filename).read_bytes())
    assert parsed[0] == provider and parsed[1] > 0.5


@pytest.mark.parametrize("filename", ["aws_cur_export.csv", "azure_cost_export.csv", "gcp_billing_export.csv"])
def test_disk_charges_are_review_only(filename):
    report = sample(filename)
    assert report.waste_breakdown.disk_review_spend > 0
    disk = next(f for f in report.rule_findings if f.key == "disk_capacity")
    assert disk.evidence_status == "needs_verification"
    assert "attachment" in disk.verification.lower()
    assert not hasattr(report, "remediation_commands")


def test_aws_idle_ip_is_observed_not_projected_savings():
    report = sample("aws_cur_export.csv")
    idle = next(f for f in report.rule_findings if f.key == "aws_idle_ipv4")
    assert idle.evidence_status == "idle_charge_observed"
    assert report.metrics.observed_idle_spend == pytest.approx(109.50)
    assert report.metrics.idle_charge_ratio_pct == pytest.approx(
        report.metrics.observed_idle_spend / report.metrics.usage_cost * 100, abs=0.01
    )
    assert any("do not establish" in note for note in report.assumptions)


def test_in_use_aws_ipv4_is_never_misclassified_as_idle():
    report = report_for(aws_rows([
        aws_row("eipalloc-live", "USE1-PublicIPv4:InUseAddress", "20", service="AmazonVPC"),
        aws_row("eipalloc-idle", "USE1-PublicIPv4:IdleAddress", "12", service="AmazonVPC"),
    ]))
    assert report.metrics.observed_idle_spend == 12
    assert report.metrics.review_spend == 0
    assert {f.key for f in report.rule_findings} == {"aws_idle_ipv4"}


def test_gcp_static_ip_charge_requires_verification():
    report = sample("gcp_billing_export.csv")
    assert report.metrics.observed_idle_spend == 0
    assert any(f.key == "public_ip_review" for f in report.rule_findings)


def test_attached_disk_sku_is_not_declared_orphaned():
    report = report_for(aws_rows([aws_row("vol-attached", "EBS:VolumeUsage.gp3")]))
    assert report.metrics.observed_idle_spend == 0
    assert report.metrics.review_spend == 100
    assert report.top_leaks[0].finding == "Disk capacity charges"
    assert report.top_leaks[0].observed_cost == 100


def test_awselb_product_code_is_recognized_without_claiming_idle():
    report = report_for(aws_rows([aws_row("arn:aws:elasticloadbalancing:example", "USE1-LoadBalancerUsage", "40", service="AWSELB")]))
    assert {f.key for f in report.rule_findings} == {"load_balancer_spend"}
    assert report.metrics.observed_idle_spend == 0
    assert report.metrics.review_spend == 40


def test_repeated_lines_are_summed_not_repeated_findings():
    report = sample("aws_cur_export.csv")
    disk = [f for f in report.top_leaks if f.resource_id == "vol-0aa11bb22cc33"]
    assert len(disk) == 1
    assert disk[0].observed_cost == pytest.approx(293.60)


def test_resources_in_different_regions_are_not_merged():
    report = report_for(aws_rows([
        aws_row("same-name", "EBS:VolumeUsage.gp3", "12", region="us-east-1"),
        aws_row("same-name", "EBS:VolumeUsage.gp3", "34", region="eu-west-1"),
    ]))
    disk = [f for f in report.top_leaks if f.finding == "Disk capacity charges"]
    assert sorted(f.observed_cost for f in disk) == [12, 34]
    assert len(disk) == 2


def test_idle_spend_attribution_is_account_scoped():
    report = report_for(aws_rows([
        aws_row("same-id", "USE1-PublicIPv4:IdleAddress", "10", account="a", service="AmazonVPC"),
        aws_row("same-id", "USE1-PublicIPv4:InUseAddress", "20", account="b", service="AmazonVPC"),
    ]))
    by_account = {x.label: x for x in report.spend_by_account}
    assert by_account["a"].observed_idle_spend == 10
    assert by_account["b"].observed_idle_spend == 0


def test_malicious_identifier_is_just_a_displayed_identifier():
    report = report_for(aws_rows([aws_row("vol-123; rm -rf $HOME", "EBS:VolumeUsage.gp3")]))
    assert "rm -rf" in report.top_leaks[0].resource_id
    assert "rm -rf" not in str(report.assumptions)
    assert not hasattr(report, "remediation_commands")


def test_mixed_currency_export_is_rejected():
    raw = aws_rows([
        aws_row("v1", "EBS:VolumeUsage.gp3", currency="USD"),
        aws_row("v2", "EBS:VolumeUsage.gp3", currency="EUR"),
    ])
    with pytest.raises(UnreadableExportError, match="multiple billing currencies"):
        parse_and_normalize_csv(raw)


def test_bad_currency_code_is_rejected():
    with pytest.raises(UnreadableExportError, match="three-letter"):
        parse_and_normalize_csv(aws_rows([aws_row("v1", "EBS:VolumeUsage.gp3", currency="2026-07-01")]))


def test_missing_currency_is_not_assumed_to_be_usd():
    report = report_for(b"lineItem/UnblendedCost,lineItem/ProductCode,lineItem/UsageType\n20,AmazonEC2,EBS:VolumeUsage.gp3\n")
    assert report.metrics.billing_currency == "UNSPECIFIED"


def test_empty_usage_report_does_not_crash():
    report = report_for(aws_rows([aws_row("v1", "EBS:VolumeUsage.gp3", "invalid")]))
    assert report.metrics.line_items_rejected == 1
    assert report.metrics.usage_cost == 0
    assert report.rule_findings == []


def test_credit_only_export_preserves_total_and_count():
    report = report_for(aws_rows([aws_row("v1", "EBS:VolumeUsage.gp3", "-15", charge="Credit")]))
    assert report.metrics.total_billed_cost == -15
    assert report.metrics.non_usage_cost == -15
    assert report.metrics.non_usage_line_items == 1
    assert report.metrics.observed_idle_spend == 0


def test_unreadable_costs_are_rejected():
    report = sample("malformed_export.csv")
    assert report.metrics.line_items_rejected == 2
    assert report.metrics.line_items_ingested == 1
    assert report.metrics.total_billed_cost == pytest.approx(1250)


def test_empty_file_reports_readable_error():
    with pytest.raises(UnreadableExportError, match="empty"):
        parse_and_normalize_csv(b"")


def test_charge_category_filtering():
    import pandas as pd
    df = pd.DataFrame({"charge_category": ["Usage", "Credit", "Tax", "RIFee", "Purchase"], "billed_cost": [1, 2, 3, 4, 5]})
    usage, excluded = split_usage(df)
    assert len(usage) == 2
    assert len(excluded) == 3


def test_tax_and_credit_are_excluded_from_usage_spend():
    report = sample("aws_cur_with_charges.csv")
    assert report.metrics.non_usage_line_items == 4
    assert report.metrics.non_usage_cost == pytest.approx(272.45)
    assert report.metrics.usage_cost == pytest.approx(2647.19)


def test_non_usage_charges_never_become_findings():
    report = report_for(aws_rows([aws_row("v1", "EBS:VolumeUsage.gp3", "100", charge="Tax")]))
    assert not report.rule_findings


def test_unattributed_spend_is_not_inferred_to_be_waste():
    report = sample("aws_cur_export.csv")
    assert report.waste_breakdown.untagged_spend > 0
    assert report.metrics.observed_idle_spend <= report.metrics.usage_cost


def test_focus_version_is_inferred_not_verified():
    report = sample("focus_1_0_export.csv")
    assert report.focus_version.detected_version == "1.0"
    assert "invoice_reconciliation" in report.focus_version.unsupported_rules
    assert report.metrics.billing_currency == "USD"
    assert report.metrics.observed_idle_spend == 0
    assert report.metrics.review_spend > 0


def test_focus_version_1_4_produces_review_findings():
    report = sample("focus_1_4_export.csv")
    assert report.focus_version.detected_version == "1.4"
    assert {f.key for f in report.rule_findings} >= {"disk_capacity", "load_balancer_spend", "snapshot_storage"}
    assert all(f.evidence_status == "needs_verification" for f in report.rule_findings)


def test_charts_are_sorted_and_sums_are_accurate():
    report = sample("focus_1_4_export.csv")
    assert [x.amount for x in report.spend_by_service] == sorted([x.amount for x in report.spend_by_service], reverse=True)
    assert sum(x.amount for x in report.spend_by_service) == pytest.approx(report.metrics.usage_cost, abs=0.01)


def test_rule_matched_spend_is_never_counted_twice():
    report = sample("focus_1_4_export.csv")
    assert sum(f.matched_spend for f in report.rule_findings) <= report.metrics.usage_cost
    assert report.metrics.review_spend == pytest.approx(sum(f.matched_spend for f in report.rule_findings))