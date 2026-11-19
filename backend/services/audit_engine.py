"""Evaluate export charges without confusing potential waste with proven savings."""

from __future__ import annotations

from typing import Dict, List

import pandas as pd

from schemas.focus_schema import (
    SCHEMA_VERSION,
    UNTAGGED_SENTINEL,
    AuditMetrics,
    AuditReportResponse,
    CategorySpend,
    FocusVersionInfo,
    RuleFinding,
    TopLeak,
    WasteBreakdown,
)
from services.focus_versions import VersionCapabilities
from services.waste_rules import rules_for

MAX_TOP_LEAKS = 15
# Match charge categories, not arbitrary substrings. AWS DiscountedUsage is
# real instance usage; filtering on "discount" silently discards those charges.
NON_USAGE_PATTERN = (
    r"^(?:tax|credit|refund|discount|bundleddiscount|edpdiscount|"
    r"negation|rifee|savingsplan(?:recurringfee|upfrontfee|negation)|"
    r"adjustment|rounding|support|reserved instance fee|promotion|rebate)$"
)



def split_usage(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Keep vendor credits, taxes and fees out of usage analysis."""
    if df.empty or "charge_category" not in df.columns:
        return df, df.iloc[0:0]
    category = df["charge_category"].fillna("").astype(str).str.lower()
    excluded = category.str.contains(NON_USAGE_PATTERN, na=False, regex=True)
    return df[~excluded], df[excluded]


def _version_info(capabilities: VersionCapabilities | None) -> FocusVersionInfo:
    if capabilities is None:
        return FocusVersionInfo()
    return FocusVersionInfo(
        detected_version=capabilities.version,
        label=capabilities.label,
        confidence=capabilities.confidence,
        reasoning=capabilities.reasoning,
        unsupported_rules=sorted(capabilities.missing_for_rules),
    )


def _empty_report(
    provider: str,
    confidence: float,
    currency: str,
    rejected: int,
    capabilities: VersionCapabilities | None = None,
    *,
    total_billed: float = 0.0,
    non_usage_cost: float = 0.0,
    non_usage_rows: int = 0,
    line_items: int = 0,
) -> AuditReportResponse:
    return AuditReportResponse(
        schema_version=SCHEMA_VERSION,
        detected_provider=provider,
        detection_confidence=confidence,
        focus_version=_version_info(capabilities),
        metrics=AuditMetrics(
            total_billed_cost=round(total_billed, 2),
            billing_currency=currency,
            line_items_ingested=line_items,
            line_items_rejected=rejected,
            usage_cost=0.0,
            non_usage_cost=round(non_usage_cost, 2),
            non_usage_line_items=non_usage_rows,
            observed_idle_spend=0.0,
            review_spend=0.0,
            idle_charge_ratio_pct=0.0,
        ),
        waste_breakdown=WasteBreakdown(),
        assumptions=["No usage charges were available to analyze in this export."],
    )


def execute_audit_engine(
    provider: str,
    df: pd.DataFrame,
    confidence: float = 0.0,
    rejected_rows: int = 0,
    currency: str = "USD",
    capabilities: VersionCapabilities | None = None,
    *,
    evidence_records: dict[str, list[int]] | None = None,
) -> AuditReportResponse:
    """Return evidence for investigation, not speculative savings or deletion scripts."""
    if df.empty:
        return _empty_report(provider, confidence, currency, rejected_rows, capabilities)

    usage_df, excluded_df = split_usage(df)
    invoice_total = float(df["billed_cost"].sum())
    non_usage_cost = float(excluded_df["billed_cost"].sum())
    if usage_df.empty:
        return _empty_report(
            provider, confidence, currency, rejected_rows, capabilities,
            total_billed=invoice_total,
            non_usage_cost=non_usage_cost,
            non_usage_rows=len(excluded_df),
            line_items=len(df),
        )

    usage_cost = float(usage_df["billed_cost"].sum())
    service = usage_df["service_category"].fillna("").astype(str).str.lower()
    sub_service = usage_df["sub_service_category"].fillna("").astype(str).str.lower()
    claimed = pd.Series(False, index=usage_df.index)
    idle_mask = pd.Series(False, index=usage_df.index)

    rule_findings: List[RuleFinding] = []
    top_leaks: List[TopLeak] = []
    review_spend = 0.0
    idle_spend = 0.0
    disk_review_spend = 0.0

    for rule in rules_for(provider):
        mask = (
            service.str.contains(rule.service_pattern, na=False, regex=True)
            & sub_service.str.contains(rule.sub_service_pattern, na=False, regex=True)
            & ~claimed
        )
        if rule.exclude_pattern:
            mask &= ~sub_service.str.contains(rule.exclude_pattern, na=False, regex=True)
        if not mask.any():
            continue
        claimed |= mask
        matched = usage_df.loc[mask]
        if evidence_records is not None:
            evidence_records[rule.key] = matched.index.tolist()
        billed = float(matched["billed_cost"].sum())
        if rule.evidence_status == "idle_charge_observed":
            idle_mask |= mask
            idle_spend += billed
        else:
            review_spend += billed
            if rule.key == "disk_capacity":
                disk_review_spend += billed

        keys = ["sub_account_name", "region", "service_category", "sub_service_category", "resource_id"]
        grouped = matched.groupby(keys, as_index=False, dropna=False)["billed_cost"].sum()
        identities = grouped[["sub_account_name", "region", "resource_id"]].drop_duplicates()
        rule_findings.append(RuleFinding(
            key=rule.key,
            title=rule.title,
            resource_count=int((~identities["resource_id"].astype(str).str.lower().isin(["unknown", "", "nan"])).sum()),
            matched_spend=round(billed, 2),
            evidence_status=rule.evidence_status,
            rationale=rule.rationale,
            verification=rule.verification,
        ))
        for row in grouped.itertuples(index=False):
            top_leaks.append(TopLeak(
                resource_id=str(row.resource_id),
                sub_account_name=str(row.sub_account_name),
                service_category=str(row.service_category),
                region=str(row.region),
                finding=rule.title,
                evidence_status=rule.evidence_status,
                observed_cost=round(float(row.billed_cost), 2),
            ))

    untagged_mask = usage_df["sub_account_name"].isin([UNTAGGED_SENTINEL, "UNKNOWN", "unknown", ""])
    untagged_cost = float(usage_df.loc[untagged_mask, "billed_cost"].sum())
    idle_df = usage_df.loc[idle_mask]

    def breakdown(column: str) -> List[CategorySpend]:
        ranked = usage_df.groupby(column)["billed_cost"].sum().sort_values(ascending=False)
        idle_by_category = idle_df.groupby(column)["billed_cost"].sum()
        return [CategorySpend(
            label=str(name),
            amount=round(float(cost), 2),
            observed_idle_spend=round(float(idle_by_category.get(name, 0.0)), 2),
        ) for name, cost in ranked.head(6).items()]

    assumptions = [
        "Matched disk, snapshot, load balancer and general IP charges are review candidates, not confirmed waste.",
        "Historical idle-address charges do not establish that an address is idle now or that the same cost can be saved next month.",
        "No destructive commands are generated from billing data without live resource and dependency verification.",
        f"Costs use the export's currency ({currency}); no conversion or monthly extrapolation was performed.",
        f"{currency} {untagged_cost:,.2f} of unattributed account spend is a governance signal, not waste.",
    ]
    if len(excluded_df):
        assumptions.append(
            f"{len(excluded_df)} non-usage line item(s) ({currency} {non_usage_cost:,.2f}) "
            "were excluded from the usage denominator."
        )
    if not usage_df["charge_category"].fillna("").astype(str).str.strip().any():
        assumptions.append("Charge categories were not supplied; all parseable rows were treated as usage.")
    if rejected_rows:
        assumptions.append(f"{rejected_rows} row(s) with invalid costs were dropped.")
    if confidence < 0.5:
        assumptions.append("Low provider-detection confidence; verify the export columns before relying on findings.")
    if capabilities and capabilities.version:
        assumptions.append(f"FOCUS version is inferred from headers, not a conformance validation: {capabilities.reasoning}")

    top_leaks.sort(key=lambda item: item.observed_cost, reverse=True)
    return AuditReportResponse(
        schema_version=SCHEMA_VERSION,
        detected_provider=provider,
        detection_confidence=confidence,
        focus_version=_version_info(capabilities),
        metrics=AuditMetrics(
            total_billed_cost=round(invoice_total, 2),
            billing_currency=currency,
            line_items_ingested=len(df),
            line_items_rejected=rejected_rows,
            usage_cost=round(usage_cost, 2),
            non_usage_cost=round(non_usage_cost, 2),
            non_usage_line_items=len(excluded_df),
            observed_idle_spend=round(idle_spend, 2),
            review_spend=round(review_spend, 2),
            idle_charge_ratio_pct=round(100 * idle_spend / usage_cost, 2) if usage_cost > 0 else 0.0,
        ),
        waste_breakdown=WasteBreakdown(
            disk_review_spend=round(disk_review_spend, 2),
            idle_ipv4_spend=round(idle_spend, 2),
            untagged_spend=round(untagged_cost, 2),
        ),
        rule_findings=rule_findings,
        spend_by_service=breakdown("service_category"),
        spend_by_account=breakdown("sub_account_name"),
        top_leaks=top_leaks[:MAX_TOP_LEAKS],
        assumptions=assumptions,
    )
