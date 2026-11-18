"""Canonical API response for offline billing evidence.

The normalized fields borrow FOCUS naming but do not implement its complete
required schema. No finding here establishes the safety of deleting a resource.
"""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

SCHEMA_VERSION = "cloudleak-internal-v2"
UNTAGGED_SENTINEL = "UNTAGGED"
EvidenceStatus = Literal["idle_charge_observed", "needs_verification"]


class AuditMetrics(BaseModel):
    total_billed_cost: float = 0.0
    billing_currency: str = "UNSPECIFIED"
    line_items_ingested: int = 0
    line_items_rejected: int = 0
    usage_cost: float = 0.0
    non_usage_cost: float = 0.0
    non_usage_line_items: int = 0
    observed_idle_spend: float = Field(0.0, description="Charges with explicitly idle billing usage types")
    review_spend: float = Field(0.0, description="Matched charges requiring external verification")
    idle_charge_ratio_pct: float = Field(0.0, description="Observed idle charges as a share of usage spend")


class WasteBreakdown(BaseModel):
    disk_review_spend: float = 0.0
    idle_ipv4_spend: float = 0.0
    untagged_spend: float = 0.0


class TopLeak(BaseModel):
    """One billed resource/SKU combination, ranked by observed cost."""

    resource_id: str
    sub_account_name: str
    service_category: str
    region: str
    finding: str
    evidence_status: EvidenceStatus
    observed_cost: float


class RuleFinding(BaseModel):
    key: str
    title: str
    resource_count: int
    matched_spend: float
    evidence_status: EvidenceStatus
    rationale: str
    verification: str


class FocusVersionInfo(BaseModel):
    detected_version: Optional[str] = None
    label: str = "Native vendor export"
    confidence: float = 0.0
    reasoning: str = ""
    unsupported_rules: List[str] = Field(
        default_factory=list, description="Columns unavailable for possible future analyses, not executed rules"
    )


class CategorySpend(BaseModel):
    label: str
    amount: float
    observed_idle_spend: float = 0.0


class AuditReportResponse(BaseModel):
    schema_version: str = SCHEMA_VERSION
    detected_provider: str
    detection_confidence: float = 0.0
    focus_version: FocusVersionInfo = Field(default_factory=FocusVersionInfo)
    metrics: AuditMetrics
    waste_breakdown: WasteBreakdown = Field(default_factory=WasteBreakdown)
    rule_findings: List[RuleFinding] = Field(default_factory=list)
    spend_by_service: List[CategorySpend] = Field(default_factory=list)
    spend_by_account: List[CategorySpend] = Field(default_factory=list)
    top_leaks: List[TopLeak] = Field(default_factory=list)
    assumptions: List[str] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    detail: str
    hint: Optional[str] = None
