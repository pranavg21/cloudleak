"""FOCUS version awareness.

A real deployment does not see one FOCUS version. Vendors adopt the spec at
different paces, and an enterprise merging AWS, Azure and GCP exports will
routinely hold three different versions at once -- plus historical files
written against whatever version was current when they were generated.

So CloudLeak does not assume a version. It detects which one an export was
written against, records what that version can and cannot express, and adapts:
rules that need a column absent from that version are skipped and reported as
skipped, rather than silently returning zero.

Version history (ratification dates):
  0.5   preview               2023
  1.0   GA                    June 2024
  1.1   ratified              7 November 2024
  1.2   ratified              29 May 2025   -- SaaS support, x_ custom columns
  1.3   ratified              2025
  1.4   ratified              4 June 2026   -- invoice/billing-period datasets

Only columns CloudLeak actually consumes are tracked here. This is not a
conformance model of the whole specification.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# Ordered oldest to newest. Order matters: detection walks it in reverse.
KNOWN_VERSIONS: List[str] = ["0.5", "1.0", "1.1", "1.2", "1.3", "1.4"]


def version_key(version: str) -> Tuple[int, ...]:
    """Sortable tuple so '1.10' would order after '1.9' if it ever exists."""
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        return (0,)


@dataclass(frozen=True)
class FocusVersion:
    """What a given FOCUS version guarantees, for the columns we use."""

    version: str
    ratified: str
    # Columns introduced in this version (cumulative set is built below).
    introduces: List[str] = field(default_factory=list)
    notes: str = ""


# Columns CloudLeak reads, and the version each became available.
VERSION_TIMELINE: List[FocusVersion] = [
    FocusVersion(
        version="0.5",
        ratified="2023 (preview)",
        introduces=[
            "BilledCost",
            "ServiceName",
            "ResourceId",
            "SubAccountId",
            "Region",
        ],
        notes="Preview release. Column naming differs from 1.0 in places.",
    ),
    FocusVersion(
        version="1.0",
        ratified="June 2024",
        introduces=[
            "ServiceCategory",
            "ChargeCategory",
            "EffectiveCost",
            "SubAccountName",
            "BillingCurrency",
            "ChargePeriodStart",
            "ChargePeriodEnd",
            "ResourceName",
            "ResourceType",
            "RegionId",
            "RegionName",
        ],
        notes="First GA release. ServiceCategory and ChargeCategory become available.",
    ),
    FocusVersion(
        version="1.1",
        ratified="7 November 2024",
        introduces=["ChargeClass", "CapacityReservationId", "CapacityReservationStatus"],
        notes="Adds billing metadata and finer commitment detail.",
    ),
    FocusVersion(
        version="1.2",
        ratified="29 May 2025",
        introduces=["PricingCurrency", "ContractedCost", "ContractedUnitPrice", "ServiceSubcategory"],
        notes="SaaS support, x_ custom column convention, version-aware schema.",
    ),
    FocusVersion(
        version="1.3",
        ratified="2025",
        introduces=[],
        notes="Incremental refinement.",
    ),
    FocusVersion(
        version="1.4",
        ratified="4 June 2026",
        introduces=["InvoiceId", "BillingPeriodStart", "BillingPeriodEnd", "CommitmentDiscountQuantity"],
        notes="Invoice Detail and Billing Period datasets; expanded commitment columns.",
    ),
]

VERSION_INDEX: Dict[str, FocusVersion] = {v.version: v for v in VERSION_TIMELINE}


def columns_available_in(version: str) -> set[str]:
    """Cumulative set of tracked columns available at or before `version`."""
    target = version_key(version)
    available: set[str] = set()
    for entry in VERSION_TIMELINE:
        if version_key(entry.version) <= target:
            available.update(entry.introduces)
    return available


def detect_focus_version(headers: List[str]) -> Tuple[Optional[str], float, str]:
    """Infer which FOCUS version an export was written against.

    Returns ``(version, confidence, reasoning)``. Version is None when the file
    is not FOCUS-shaped at all -- a native vendor export, for example.

    The heuristic is deliberately simple and stated plainly rather than being
    presented as authoritative: find the newest version whose distinguishing
    columns are present. A file carrying InvoiceId is at least 1.4; one with
    ChargeClass but no InvoiceId is 1.1-1.3; one with ServiceCategory but no
    ChargeClass is 1.0.
    """
    present = {h.strip() for h in headers}

    # A FOCUS export must at minimum carry BilledCost.
    if "BilledCost" not in present and "EffectiveCost" not in present:
        return None, 0.0, "No FOCUS cost column present; treated as a native vendor export."

    # Explicit metadata column wins if the export declares its own version.
    for declared in ("FocusVersion", "SpecVersion", "x_FocusVersion"):
        if declared in present:
            return None, 0.0, (
                f"The export carries a {declared} column. CloudLeak reads only the header "
                "row, so the declared value is not inspected; version was inferred from "
                "columns instead."
            )

    best_version = "0.5"
    reasons: List[str] = []

    for entry in reversed(VERSION_TIMELINE):
        markers = [c for c in entry.introduces if c in present]
        if markers:
            best_version = entry.version
            reasons.append(
                f"Found {', '.join(sorted(markers)[:3])}, introduced in FOCUS {entry.version}."
            )
            break

    # Confidence: share of that version's cumulative tracked columns present.
    expected = columns_available_in(best_version)
    hit = len(expected & present)
    confidence = round(hit / len(expected), 2) if expected else 0.0

    if not reasons:
        reasons.append("Only baseline columns found; assuming the earliest version.")

    return best_version, confidence, " ".join(reasons)


@dataclass(frozen=True)
class VersionCapabilities:
    """What CloudLeak can and cannot do with a given detected version."""

    version: Optional[str]
    confidence: float
    reasoning: str
    available_columns: set[str] = field(default_factory=set)
    missing_for_rules: Dict[str, List[str]] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"FOCUS {self.version}" if self.version else "Native vendor export"


# Columns each CloudLeak rule would ideally have from a FOCUS export.
RULE_COLUMN_REQUIREMENTS: Dict[str, List[str]] = {
    "charge_type_filtering": ["ChargeCategory"],
    "service_categorisation": ["ServiceCategory"],
    "amortised_cost_view": ["EffectiveCost"],
    "commitment_analysis": ["CommitmentDiscountQuantity"],
    "invoice_reconciliation": ["InvoiceId", "BillingPeriodStart"],
}


def assess_capabilities(headers: List[str]) -> VersionCapabilities:
    """Detect the version and work out which rules it can support."""
    version, confidence, reasoning = detect_focus_version(headers)
    present = {h.strip() for h in headers}

    if version is None:
        return VersionCapabilities(None, confidence, reasoning, present, {})

    available = columns_available_in(version) & present
    missing: Dict[str, List[str]] = {}
    for rule, required in RULE_COLUMN_REQUIREMENTS.items():
        absent = [c for c in required if c not in present]
        if absent:
            missing[rule] = absent

    return VersionCapabilities(version, confidence, reasoning, available, missing)
