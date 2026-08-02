"""Canonical internal data contract for CloudLeak.

This schema is MODELLED ON the FinOps FOCUS specification -- it borrows the
field naming and the idea of a vendor-neutral cost record -- but it is NOT
FOCUS-conformant. FOCUS defines a large mandatory column set and controlled
enumerations (ServiceCategory, ChargeCategory and others); this uses seven
fields and carries the vendor's own category strings through unchanged.
Treat the naming as a nod to FOCUS, not a compliance claim.

Every vendor billing export is normalized into the column set defined by
``FOCUS_COLUMNS`` before any heuristic runs. The audit engine never sees a
vendor-specific header, which keeps the waste rules provider-agnostic.

FocusRecord exists as the documented contract for a single normalized line
item. Ingestion is deliberately vectorized (see services/parsers.py) and does
NOT instantiate one model per row: a real AWS Cost and Usage Report can carry
millions of line items, and per-row model construction would dominate request
latency. Pydantic is used where it earns its cost -- validating the API
response contract.
"""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

SCHEMA_VERSION = "cloudleak-internal-v1"

# Internal column names, shown against the FOCUS field each is modelled on.
FOCUS_COLUMNS: dict[str, str] = {
    "provider_name": "ProviderName",
    "billed_cost": "BilledCost",
    "service_category": "ServiceCategory",
    "sub_service_category": "SubServiceCategory",
    "sub_account_name": "SubAccountName",
    "resource_id": "ResourceId",
    "region": "Region",
}

UNTAGGED_SENTINEL = "UNTAGGED"

ProviderName = Literal["Azure", "AWS", "GCP", "Generic-FOCUS"]


class FocusRecord(BaseModel):
    """A single billing line item in CloudLeak's internal schema."""

    provider_name: str = Field(..., description="Azure, AWS, GCP or Generic-FOCUS")
    billed_cost: float = Field(0.0, description="Billed cost in the export's billing currency")
    service_category: str = Field("Other", description="High-level category, e.g. Storage, Networking")
    sub_service_category: str = Field("", description="Sub-category, e.g. Managed Disks, Unused IP")
    sub_account_name: str = Field(
        UNTAGGED_SENTINEL,
        description="Resource Group (Azure), Usage Account ID (AWS), Project ID (GCP)",
    )
    resource_id: str = Field("unknown", description="Resource name or fully qualified URI")
    region: Optional[str] = Field("global", description="Deployment region")
    charge_category: str = Field(
        "", description="Raw vendor charge type, e.g. Usage, Tax, Credit, Refund"
    )


class AuditMetrics(BaseModel):
    """Headline financials for the executive summary."""

    total_billed_cost: float = Field(..., description="Sum of BilledCost across all ingested line items")
    identified_waste: float = Field(..., description="Sum of high-conviction waste findings")
    waste_leak_ratio_pct: float = Field(..., description="identified_waste / total_billed_cost * 100")
    billing_currency: str = Field("USD", description="Currency reported by the export; not converted")
    line_items_ingested: int = Field(0, description="Rows successfully normalized")
    line_items_rejected: int = Field(0, description="Rows dropped due to unparseable cost values")
    usage_cost: float = Field(
        0.0, description="Spend on actual usage and purchases -- the waste-ratio denominator"
    )
    non_usage_cost: float = Field(
        0.0, description="Tax, credits, refunds and adjustments, excluded from the ratio"
    )
    non_usage_line_items: int = Field(0, description="Rows classified as non-usage")


class WasteBreakdown(BaseModel):
    """Waste attributed to each heuristic rule.

    ``untagged_spend`` is a governance signal, not a leak: the money is being
    spent on something, it just has no owner. It is reported separately and
    deliberately excluded from ``identified_waste`` so the headline number
    stays defensible in front of a CFO.
    """

    orphaned_storage_waste: float = 0.0
    zombie_ip_waste: float = 0.0
    untagged_spend: float = 0.0


class RemediationCommand(BaseModel):
    """One executable cleanup command with the finding that produced it."""

    resource_id: str
    sub_account_name: str
    monthly_cost: float
    finding: str = Field(..., description="Which heuristic flagged this resource")
    command: str = Field(..., description="Shell-quoted CLI command, safe to review then run")


class TopLeak(BaseModel):
    """A single expensive finding, for the dashboard's ranked table."""

    resource_id: str
    sub_account_name: str
    service_category: str
    region: str
    finding: str
    estimated_waste: float


class RuleFinding(BaseModel):
    """Per-rule totals, so the breakdown is no longer fixed at two rules."""

    key: str
    title: str
    resource_count: int
    matched_spend: float
    estimated_waste: float
    reclaim_factor: float
    rationale: str


class FocusVersionInfo(BaseModel):
    """What FOCUS version the export appears to use, and what that allows."""

    detected_version: Optional[str] = Field(
        None, description="e.g. '1.0', '1.4'. Null for a native vendor export."
    )
    label: str = "Native vendor export"
    confidence: float = 0.0
    reasoning: str = ""
    unsupported_rules: List[str] = Field(
        default_factory=list,
        description="Analyses this version's columns cannot support",
    )


class AuditReportResponse(BaseModel):
    """The complete audit result returned by POST /api/v1/audit/upload."""

    schema_version: str = SCHEMA_VERSION
    detected_provider: str
    detection_confidence: float = Field(
        0.0, description="Share of expected vendor headers found, 0.0-1.0"
    )
    focus_version: FocusVersionInfo = Field(default_factory=FocusVersionInfo)
    metrics: AuditMetrics
    waste_breakdown: WasteBreakdown
    rule_findings: List[RuleFinding] = Field(
        default_factory=list, description="Per-rule totals for charting"
    )
    spend_by_service: List["CategorySpend"] = Field(
        default_factory=list, description="Top service categories by spend"
    )
    spend_by_account: List["CategorySpend"] = Field(
        default_factory=list, description="Top accounts/projects by spend"
    )
    top_leaks: List[TopLeak] = Field(default_factory=list)
    remediation_commands: List[RemediationCommand] = Field(default_factory=list)
    assumptions: List[str] = Field(
        default_factory=list,
        description="Every estimate the engine made, stated plainly for the reader",
    )


class CategorySpend(BaseModel):
    """One slice of spend, for charts."""

    label: str
    amount: float
    waste: float = 0.0


class ErrorResponse(BaseModel):
    """Structured error body returned for all 4xx responses."""

    detail: str
    hint: Optional[str] = None


AuditReportResponse.model_rebuild()
