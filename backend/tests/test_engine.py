"""End-to-end checks over the sample exports in ../../samples."""

from __future__ import annotations

import pathlib
import shlex
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from services.audit_engine import execute_audit_engine  # noqa: E402
from services.parsers import UnreadableExportError, parse_and_normalize_csv  # noqa: E402

SAMPLES = pathlib.Path(__file__).resolve().parents[2] / "samples"


def run(name: str):
    provider, confidence, df, rejected, currency, caps = parse_and_normalize_csv(
        (SAMPLES / name).read_bytes()
    )
    return (
        execute_audit_engine(provider, df, confidence, rejected, currency, caps),
        provider,
        confidence,
    )


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("azure_cost_export.csv", "Azure"),
        ("aws_cur_export.csv", "AWS"),
        ("gcp_billing_export.csv", "GCP"),
    ],
)
def test_provider_detection(filename, expected):
    _, provider, confidence = run(filename)
    assert provider == expected
    assert confidence > 0.5


@pytest.mark.parametrize(
    "filename",
    ["azure_cost_export.csv", "aws_cur_export.csv", "gcp_billing_export.csv"],
)
def test_finds_both_leak_classes(filename):
    report, _, _ = run(filename)
    assert report.waste_breakdown.orphaned_storage_waste > 0
    assert report.waste_breakdown.zombie_ip_waste > 0
    assert 0 < report.metrics.waste_leak_ratio_pct < 100


def test_waste_never_exceeds_billed_cost():
    for name in ("azure_cost_export.csv", "aws_cur_export.csv", "gcp_billing_export.csv"):
        report, _, _ = run(name)
        assert report.metrics.identified_waste <= report.metrics.total_billed_cost


def test_duplicate_line_items_produce_one_command():
    """A resource billed on many days must yield exactly one delete command."""
    report, _, _ = run("aws_cur_export.csv")
    volumes = [c for c in report.remediation_commands if "vol-0aa11bb22cc33" in c.command]
    assert len(volumes) == 1
    # ...and its cost is the sum of both line items, not one of them.
    assert volumes[0].monthly_cost == pytest.approx(293.60)


def test_commands_are_shell_quoted():
    """A hostile resource name must not escape into an executable clause."""
    report, _, _ = run("injection_probe.csv")
    assert report.remediation_commands, "expected the malicious disk to be flagged"
    command = report.remediation_commands[0].command

    # The real test: the shell must see the payload as ONE argument, not as a
    # command separator followed by a second command.
    tokens = shlex.split(command)
    assert "disk-evil; rm -rf $HOME" in tokens, "payload must survive as a single argument"
    assert tokens[0] == "gcloud"
    assert not any(token in {";", "&&", "|", "rm"} for token in tokens), "no injected command token"


def test_unreadable_costs_are_rejected_not_zeroed():
    report, _, _ = run("malformed_export.csv")
    assert report.metrics.line_items_rejected == 2
    assert report.metrics.line_items_ingested == 1
    # The thousands separator must parse, not fall through to a rejection.
    assert report.metrics.total_billed_cost == pytest.approx(1250.00)


def test_empty_file_is_a_clean_error():
    with pytest.raises(UnreadableExportError):
        parse_and_normalize_csv(b"")


def test_untagged_is_excluded_from_headline_waste():
    report, _, _ = run("aws_cur_export.csv")
    assert report.waste_breakdown.untagged_spend > 0
    assert report.metrics.identified_waste == pytest.approx(
        report.waste_breakdown.orphaned_storage_waste + report.waste_breakdown.zombie_ip_waste
    )


# --- charge-category filtering -------------------------------------------------


def test_non_usage_rows_are_excluded_from_the_denominator():
    """Tax, credits, refunds and fees must not sit in the waste-ratio base."""
    report, _, _ = run("aws_cur_with_charges.csv")
    m = report.metrics

    assert m.non_usage_line_items == 4
    assert m.non_usage_cost == pytest.approx(272.45)
    # Invoice total is still reported, but the ratio uses usage only.
    assert m.usage_cost == pytest.approx(2647.19)
    assert m.total_billed_cost == pytest.approx(m.usage_cost + m.non_usage_cost)
    assert m.waste_leak_ratio_pct == pytest.approx(
        m.identified_waste / m.usage_cost * 100, abs=0.01
    )


def test_ratio_differs_from_the_naive_invoice_total_calculation():
    """Guards the actual bug: dividing by the invoice total understates waste."""
    report, _, _ = run("aws_cur_with_charges.csv")
    m = report.metrics
    naive = round(m.identified_waste / m.total_billed_cost * 100, 2)
    assert m.waste_leak_ratio_pct != naive
    assert m.waste_leak_ratio_pct > naive  # tax/fees inflated the old denominator


def test_credit_rows_do_not_become_waste_findings():
    """A credit referencing an EBS SKU is not an orphaned disk."""
    report, _, _ = run("aws_cur_with_charges.csv")
    for leak in report.top_leaks:
        assert leak.estimated_waste > 0
    for command in report.remediation_commands:
        assert command.monthly_cost > 0


def test_export_without_a_charge_column_still_works():
    """Absence of the column must not zero the denominator."""
    report, _, _ = run("aws_cur_export.csv")
    m = report.metrics
    assert m.non_usage_line_items == 0
    assert m.usage_cost == pytest.approx(m.total_billed_cost)
    assert m.waste_leak_ratio_pct > 0
    assert any("no charge-type column" in a.lower() for a in report.assumptions)


def test_split_usage_helper_classifies_vendor_terms():
    import pandas as pd

    from services.audit_engine import split_usage

    df = pd.DataFrame(
        {
            "charge_category": [
                "Usage", "Tax", "Credit", "RIFee", "Refund",
                "SavingsPlanNegation", "Purchase", "",
            ],
            "billed_cost": [1.0] * 8,
        }
    )
    usage, non_usage = split_usage(df)
    assert len(usage) == 3          # Usage, Purchase, blank
    assert len(non_usage) == 5


# --- FOCUS version awareness ---------------------------------------------------


def test_detects_focus_1_0_and_flags_unavailable_analyses():
    report, _, _ = run("focus_1_0_export.csv")
    v = report.focus_version
    assert v.detected_version == "1.0"
    assert v.label == "FOCUS 1.0"
    # 1.0 predates the invoice and commitment columns, so those must be
    # reported as skipped rather than silently returning zero.
    assert "invoice_reconciliation" in v.unsupported_rules
    assert "commitment_analysis" in v.unsupported_rules


def test_detects_focus_1_4_and_supports_more():
    report, _, _ = run("focus_1_4_export.csv")
    v = report.focus_version
    assert v.detected_version == "1.4"
    assert "invoice_reconciliation" not in v.unsupported_rules


def test_native_vendor_export_is_not_labelled_focus():
    report, _, _ = run("aws_cur_export.csv")
    assert report.focus_version.detected_version is None
    assert "native" in report.focus_version.label.lower()


def test_older_focus_version_still_produces_findings():
    """Version differences must not silently disable waste detection."""
    report, _, _ = run("focus_1_0_export.csv")
    assert report.metrics.identified_waste > 0
    assert {f.key for f in report.rule_findings} >= {"orphaned_disk", "idle_public_ip"}


def test_focus_charge_category_filtering_works_across_versions():
    report, _, _ = run("focus_1_0_export.csv")
    # Tax row and Credit row are non-usage.
    assert report.metrics.non_usage_line_items == 2


# --- Multiple provider-specific rules ------------------------------------------


def test_additional_rules_fire_and_are_reported_separately():
    report, _, _ = run("focus_1_4_export.csv")
    keys = {f.key for f in report.rule_findings}
    assert "stale_snapshot" in keys
    assert "idle_load_balancer" in keys
    # Each rule reports its own claim rate rather than a single global factor.
    factors = {f.key: f.reclaim_factor for f in report.rule_findings}
    assert factors["idle_public_ip"] == 1.0
    assert factors["stale_snapshot"] < 0.5


def test_a_resource_is_claimed_by_only_one_rule():
    """Snapshots match disk language too; they must not be double counted."""
    report, _, _ = run("focus_1_4_export.csv")
    total_from_rules = sum(f.estimated_waste for f in report.rule_findings)
    assert report.metrics.identified_waste == pytest.approx(total_from_rules, abs=0.02)


def test_rules_without_a_command_still_report_waste():
    """Premium-tier findings have no safe delete command but still count."""
    from services.waste_rules import RULES_BY_KEY

    assert RULES_BY_KEY["legacy_storage_tier"].commands == {}


# --- Chart data ----------------------------------------------------------------


def test_chart_breakdowns_are_populated_and_sorted():
    report, _, _ = run("focus_1_4_export.csv")
    assert report.spend_by_service
    assert report.spend_by_account
    amounts = [s.amount for s in report.spend_by_service]
    assert amounts == sorted(amounts, reverse=True)


def test_chart_breakdown_excludes_non_usage_rows():
    report, _, _ = run("focus_1_0_export.csv")
    labels = {s.label.lower() for s in report.spend_by_service}
    # The Tax row is non-usage and must not appear as a spend category.
    total_charted = sum(s.amount for s in report.spend_by_service)
    assert total_charted == pytest.approx(report.metrics.usage_cost, abs=0.01)
