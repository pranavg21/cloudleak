"""Deterministic waste heuristics and remediation script generation.

Two design decisions worth stating up front, because both affect whether the
output survives contact with a customer:

1. Findings are aggregated per resource before anything is generated. A
   billing export contains one line item per resource per day (often per hour),
   so a single orphaned disk appears 30-720 times in one file. Emitting a CLI
   command per line item would produce thousands of duplicate deletes and a
   wildly inflated finding count.

2. Every value interpolated into a shell command is passed through
   ``shlex.quote``. Resource IDs are attacker-influenced data: they come from
   an uploaded file, and the whole product promise is that the user pastes the
   output into a terminal. Unquoted interpolation turns a cost report into a
   remote code execution vector against the person running the audit.
"""

from __future__ import annotations

import shlex
from typing import Dict, List, Tuple

import pandas as pd

from schemas.focus_schema import (
    SCHEMA_VERSION,
    UNTAGGED_SENTINEL,
    AuditMetrics,
    AuditReportResponse,
    CategorySpend,
    FocusVersionInfo,
    RemediationCommand,
    RuleFinding,
    TopLeak,
    WasteBreakdown,
)
from services.focus_versions import VersionCapabilities
from services.waste_rules import WasteRule, rules_for

# --- Tunable heuristic assumptions -------------------------------------------
# A detached disk still bills for provisioned capacity. Without utilization
# telemetry we cannot prove a disk is detached from billing data alone, so we
# claim a conservative share of matched disk spend rather than all of it. This
# number is an assumption, is surfaced to the user in the report, and is the
# first thing to calibrate against a real customer's environment.
ORPHANED_DISK_RECLAIM_FACTOR = 0.40

# An idle public IP is billed under a distinct SKU that only exists when the
# address is unattached, so matched spend is claimed in full.
IDLE_IP_RECLAIM_FACTOR = 1.00

# Cap on emitted commands, ranked by cost. Keeps the response payload and the
# reviewer's attention on the money.
MAX_COMMANDS = 25
MAX_TOP_LEAKS = 15

# --- Rule definitions ---------------------------------------------------------
# Matched case-insensitively against the canonical FOCUS categories, so the
# same rule set works for all three vendors.
DISK_SERVICE_PATTERN = r"storage|compute|amazonec2|ec2|block"
DISK_SUBSERVICE_PATTERN = r"disk|ebs[:\s]*volume|volumeusage|pd[-\s]|persistentdisk|storage pd|unattached"

IP_SERVICE_PATTERN = r"network|compute|amazonec2|ec2|virtual network"
IP_SUBSERVICE_PATTERN = r"elasticip|idleaddress|public ip|static ip|ip address|\beip\b|unused ip"

# --- Charge classification -----------------------------------------------------
# A billing export is not only usage. It also carries tax, credits, refunds,
# commitment fees and negations. Summing all of it and calling the result
# "billed cost" makes the waste ratio wrong in both directions: credits and
# refunds are negative and shrink the denominator, tax inflates it.
#
# FOCUS models this as ChargeCategory (Usage / Purchase / Tax / Credit /
# Adjustment). Vendors expose their own version -- AWS lineItem/LineItemType,
# Azure ChargeType, GCP cost_type -- which the parser normalizes into
# `charge_category`.
#
# Only usage and purchases go into the denominator. Everything else is reported
# separately so the number stays defensible.
NON_USAGE_PATTERN = (
    r"tax|credit|refund|discount|negation|rifee|savingsplan(?:recurring|negation)"
    r"|adjustment|rounding|support|reserved instance fee|promotion|rebate"
)

# Rows whose charge type is blank are treated as usage. Absence of the column
# is not evidence of a non-usage charge, and defaulting the other way would
# silently zero the denominator on exports that omit it.
FINDING_ORPHANED_DISK = "Orphaned storage disk"
FINDING_IDLE_IP = "Idle public IP"


def split_usage(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split a normalized frame into (usage rows, non-usage rows).

    Non-usage rows are tax, credits, refunds, commitment fees and adjustments.
    They are excluded from the waste-ratio denominator and from waste matching:
    a credit line referencing a disk SKU is not an orphaned disk.
    """
    if df.empty or "charge_category" not in df.columns:
        return df, df.iloc[0:0]

    category = df["charge_category"].fillna("").astype(str).str.lower()
    non_usage_mask = category.str.contains(NON_USAGE_PATTERN, na=False, regex=True)
    return df[~non_usage_mask], df[non_usage_mask]


def _aggregate(df: pd.DataFrame) -> pd.DataFrame:
    """Collapse line items to one row per resource, summing cost."""
    grouped = (
        df.groupby(["resource_id", "sub_account_name"], as_index=False, dropna=False)
        .agg(
            billed_cost=("billed_cost", "sum"),
            service_category=("service_category", "first"),
            sub_service_category=("sub_service_category", "first"),
            region=("region", "first"),
        )
    )
    return grouped.sort_values("billed_cost", ascending=False)


def _build_command(provider: str, finding: str, resource_id: str, account: str) -> str | None:
    """Return a shell-quoted cleanup command, or None if unsupported.

    Every interpolated value is quoted. Deletes are non-reversible, so no
    command here is auto-run; the frontend presents them for review.
    """
    res = shlex.quote(resource_id)
    acc = shlex.quote(account)

    if finding == FINDING_ORPHANED_DISK:
        if provider == "Azure":
            return f"az disk delete --resource-group {acc} --name {res} --yes"
        if provider == "AWS":
            return f"aws ec2 delete-volume --volume-id {res}"
        if provider == "GCP":
            return f"gcloud compute disks delete {res} --project={acc} --quiet"
    elif finding == FINDING_IDLE_IP:
        if provider == "Azure":
            return f"az network public-ip delete --resource-group {acc} --name {res}"
        if provider == "AWS":
            return f"aws ec2 release-address --allocation-id {res}"
        if provider == "GCP":
            return f"gcloud compute addresses delete {res} --project={acc} --quiet"
    return None


def _dedupe_preserving_order(commands: List[RemediationCommand]) -> List[RemediationCommand]:
    """Order-preserving dedup. ``set()`` would reshuffle identical uploads."""
    seen: set[str] = set()
    out: List[RemediationCommand] = []
    for cmd in commands:
        if cmd.command not in seen:
            seen.add(cmd.command)
            out.append(cmd)
    return out


def _empty_report(provider: str, confidence: float, currency: str, rejected: int) -> AuditReportResponse:
    version_info = FocusVersionInfo()
    if capabilities is not None:
        version_info = FocusVersionInfo(
            detected_version=capabilities.version,
            label=capabilities.label,
            confidence=capabilities.confidence,
            reasoning=capabilities.reasoning,
            unsupported_rules=sorted(capabilities.missing_for_rules.keys()),
        )
        if capabilities.version:
            assumptions.append(
                f"This export appears to follow {capabilities.label}. {capabilities.reasoning}"
            )
        if capabilities.missing_for_rules:
            readable = ", ".join(
                rule.replace("_", " ") for rule in sorted(capabilities.missing_for_rules)
            )
            assumptions.append(
                f"Columns for the following analyses are absent from this export, so they were "
                f"skipped rather than reported as zero: {readable}."
            )

    for finding in rule_findings:
        assumptions.append(
            f"{finding.title}: {int(finding.reclaim_factor * 100)}% of "
            f"{currency} {finding.matched_spend:,.2f} matched spend claimed. {finding.rationale}"
        )

    return AuditReportResponse(
        schema_version=SCHEMA_VERSION,
        detected_provider=provider,
        detection_confidence=confidence,
        focus_version=version_info,
        rule_findings=rule_findings,
        spend_by_service=spend_by_service,
        spend_by_account=spend_by_account,
        metrics=AuditMetrics(
            total_billed_cost=0.0,
            identified_waste=0.0,
            waste_leak_ratio_pct=0.0,
            billing_currency=currency,
            line_items_ingested=0,
            line_items_rejected=rejected,
        ),
        waste_breakdown=WasteBreakdown(),
        assumptions=["No priceable line items were found in this export."],
    )


def execute_audit_engine(
    provider: str,
    df: pd.DataFrame,
    confidence: float = 0.0,
    rejected_rows: int = 0,
    currency: str = "USD",
    capabilities: "VersionCapabilities | None" = None,
) -> AuditReportResponse:
    """Run every waste rule over a normalized FOCUS frame."""
    if df.empty:
        return _empty_report(provider, confidence, currency, rejected_rows)

    # Separate real usage from tax, credits, refunds and adjustments. Only usage
    # forms the denominator, and only usage is searched for waste.
    usage_df, non_usage_df = split_usage(df)

    total_billed = float(df["billed_cost"].sum())
    usage_cost = float(usage_df["billed_cost"].sum())
    non_usage_cost = float(non_usage_df["billed_cost"].sum())
    line_items = int(len(df))

    if usage_df.empty:
        return _empty_report(provider, confidence, currency, rejected_rows)

    service = usage_df["service_category"].str.lower()
    sub_service = usage_df["sub_service_category"].str.lower()

    # --- Apply every rule registered for this provider -------------------------
    # Rules are evaluated in registry order and a row is claimed by the first
    # rule that matches, so a resource is never counted twice.
    claimed = pd.Series(False, index=usage_df.index)
    matched_frames: List[Tuple[WasteRule, pd.DataFrame]] = []

    for rule in rules_for(provider):
        mask = service.str.contains(rule.service_pattern, na=False, regex=True) & sub_service.str.contains(
            rule.sub_service_pattern, na=False, regex=True
        )
        if rule.exclude_pattern:
            mask &= ~sub_service.str.contains(rule.exclude_pattern, na=False, regex=True)
        mask &= ~claimed
        if not mask.any():
            continue
        claimed |= mask
        matched_frames.append((rule, _aggregate(usage_df[mask])))

    rule_findings: List[RuleFinding] = []
    top_leaks: List[TopLeak] = []
    commands: List[RemediationCommand] = []
    total_waste = 0.0
    waste_by_key: Dict[str, float] = {}

    for rule, frame in matched_frames:
        if frame.empty:
            continue
        matched_spend = float(frame["billed_cost"].sum())
        rule_waste = matched_spend * rule.reclaim_factor
        total_waste += rule_waste
        waste_by_key[rule.key] = rule_waste

        rule_findings.append(
            RuleFinding(
                key=rule.key,
                title=rule.title,
                resource_count=int(len(frame)),
                matched_spend=round(matched_spend, 2),
                estimated_waste=round(rule_waste, 2),
                reclaim_factor=rule.reclaim_factor,
                rationale=rule.rationale,
            )
        )

        for row in frame.itertuples(index=False):
            waste = float(row.billed_cost) * rule.reclaim_factor
            resource_id = str(row.resource_id)
            account = str(row.sub_account_name)

            top_leaks.append(
                TopLeak(
                    resource_id=resource_id,
                    sub_account_name=account,
                    service_category=str(row.service_category),
                    region=str(row.region),
                    finding=rule.title,
                    estimated_waste=round(waste, 2),
                )
            )

            # Unnamed resources cannot be safely targeted by a CLI command.
            if resource_id in {"unknown", ""} or resource_id.startswith("unnamed"):
                continue
            command = rule.command_for(provider, resource_id, account)
            if command:
                commands.append(
                    RemediationCommand(
                        resource_id=resource_id,
                        sub_account_name=account,
                        monthly_cost=round(float(row.billed_cost), 2),
                        finding=rule.title,
                        command=command,
                    )
                )

    disk_waste = waste_by_key.get("orphaned_disk", 0.0)
    ip_waste = waste_by_key.get("idle_public_ip", 0.0)

    untagged_mask = usage_df["sub_account_name"].isin([UNTAGGED_SENTINEL, "UNKNOWN", "unknown", ""])
    untagged_spend = float(usage_df.loc[untagged_mask, "billed_cost"].sum())

    # Denominator is usage cost, not everything on the invoice.
    waste_ratio = round(total_waste / usage_cost * 100.0, 2) if usage_cost > 0 else 0.0

    # --- Chart data -----------------------------------------------------------
    waste_lookup = (
        pd.Series({leak.resource_id: leak.estimated_waste for leak in top_leaks})
        if top_leaks
        else pd.Series(dtype=float)
    )

    def _breakdown(column: str, limit: int = 6) -> List[CategorySpend]:
        grouped = usage_df.groupby(column)["billed_cost"].sum().sort_values(ascending=False)
        out: List[CategorySpend] = []
        for label, amount in grouped.head(limit).items():
            subset = usage_df[usage_df[column] == label]["resource_id"].unique()
            waste = float(waste_lookup.reindex(subset).fillna(0).sum()) if len(waste_lookup) else 0.0
            out.append(
                CategorySpend(label=str(label), amount=round(float(amount), 2), waste=round(waste, 2))
            )
        return out

    spend_by_service = _breakdown("service_category")
    spend_by_account = _breakdown("sub_account_name")

    top_leaks.sort(key=lambda leak: leak.estimated_waste, reverse=True)
    commands.sort(key=lambda cmd: cmd.monthly_cost, reverse=True)

    assumptions = [
        f"Untagged spend ({currency} {untagged_spend:,.2f}) is reported as a governance gap and is "
        "excluded from the headline waste figure.",
        f"Costs are reported in {currency} as they appear in the export. No currency conversion is applied.",
    ]

    if len(non_usage_df):
        assumptions.insert(
            0,
            f"{len(non_usage_df):,} line item(s) worth {currency} {non_usage_cost:,.2f} were "
            "classified as tax, credits, refunds or adjustments rather than usage. The waste "
            f"ratio is measured against usage cost ({currency} {usage_cost:,.2f}), not the "
            "invoice total.",
        )
    else:
        assumptions.insert(
            0,
            "No charge-type column was found in this export, so every line item is treated as "
            "usage. If the bill contains tax or credits, the ratio's denominator is overstated.",
        )
    if rejected_rows:
        assumptions.append(
            f"{rejected_rows:,} row(s) had an unreadable cost value and were excluded from all totals."
        )
    if confidence < 0.5:
        assumptions.append(
            "Vendor detection confidence was low. Check the detected provider before running any command."
        )

    version_info = FocusVersionInfo()
    if capabilities is not None:
        version_info = FocusVersionInfo(
            detected_version=capabilities.version,
            label=capabilities.label,
            confidence=capabilities.confidence,
            reasoning=capabilities.reasoning,
            unsupported_rules=sorted(capabilities.missing_for_rules.keys()),
        )
        if capabilities.version:
            assumptions.append(
                f"This export appears to follow {capabilities.label}. {capabilities.reasoning}"
            )
        if capabilities.missing_for_rules:
            readable = ", ".join(
                rule.replace("_", " ") for rule in sorted(capabilities.missing_for_rules)
            )
            assumptions.append(
                f"Columns for the following analyses are absent from this export, so they were "
                f"skipped rather than reported as zero: {readable}."
            )

    for finding in rule_findings:
        assumptions.append(
            f"{finding.title}: {int(finding.reclaim_factor * 100)}% of "
            f"{currency} {finding.matched_spend:,.2f} matched spend claimed. {finding.rationale}"
        )

    return AuditReportResponse(
        schema_version=SCHEMA_VERSION,
        detected_provider=provider,
        detection_confidence=confidence,
        focus_version=version_info,
        rule_findings=rule_findings,
        spend_by_service=spend_by_service,
        spend_by_account=spend_by_account,
        metrics=AuditMetrics(
            total_billed_cost=round(total_billed, 2),
            identified_waste=round(total_waste, 2),
            waste_leak_ratio_pct=waste_ratio,
            billing_currency=currency,
            line_items_ingested=line_items,
            line_items_rejected=rejected_rows,
            usage_cost=round(usage_cost, 2),
            non_usage_cost=round(non_usage_cost, 2),
            non_usage_line_items=int(len(non_usage_df)),
        ),
        waste_breakdown=WasteBreakdown(
            orphaned_storage_waste=round(disk_waste, 2),
            zombie_ip_waste=round(ip_waste, 2),
            untagged_spend=round(untagged_spend, 2),
        ),
        top_leaks=top_leaks[:MAX_TOP_LEAKS],
        remediation_commands=_dedupe_preserving_order(commands)[:MAX_COMMANDS],
        assumptions=assumptions,
    )
