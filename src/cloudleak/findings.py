"""The ``cloudleak.finding.v1`` contract and its invariants.

Evidence ladder (CloudLeak's billing-only ceiling is Level 2):

    L1  billing observation       validated line items; spend, reconciliation, variance
    L2  billing-supported hypothesis  a provider-explicit billing signature
    L3  corroborated by an offline inventory snapshot   (not produced from billing alone)
    L4  owner-confirmed recommendation                  (recorded by people, not inferred)
    L5  validated outcome on a later finalized bill     (recorded by people, not inferred)

Invariants enforced at construction time, so no code path can emit a
finding that breaks them:

* ``verified_savings`` is always null at L1 and L2.
* L2 findings must list unknown information, potential false positives and
  at least one read-only verification step.
* Verification steps are read-only; CloudLeak never emits a command that
  changes infrastructure.
* Billing data alone can never produce L3-L5.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any

from cloudleak.money import fmt

SCHEMA_VERSION = "cloudleak.finding.v1"
LEVELS = ("L1", "L2", "L3", "L4", "L5")
CLASSIFICATIONS = ("qa_defect", "reconciliation_delta", "variance_driver", "billing_signature_candidate")
FINALITY = ("finalized", "preliminary_estimate", "unknown")
REVIEW_STATUS = ("unreviewed", "investigating", "accepted", "dismissed", "resolved")
ACTION_RISK = ("none_readonly", "low", "medium", "high", "unknown")
SEVERITY = ("info", "low", "medium", "high")


class ContractViolation(AssertionError):
    """A finding was constructed that breaks the evidence contract. Always a bug."""


@dataclass(frozen=True)
class VerificationStep:
    description: str
    command: str | None = None  # read-only; shell-quoted
    tool: str | None = None  # aws | az | gcloud | console | sql
    action_risk: str = "none_readonly"

    def __post_init__(self) -> None:
        if self.action_risk != "none_readonly":
            raise ContractViolation("verification steps must be read-only")
        if self.command:
            # Quoted arguments are data (resource IDs from the export), not verbs.
            unquoted = re.sub(r"'[^']*'", "''", self.command.lower())
            lowered = f" {unquoted} "
            for verb in (" delete", " terminate", " release-address", " remove", " destroy", " modify-",
                         " update ", " stop-", " deallocate", " purge", " rm "):
                if verb in lowered:
                    raise ContractViolation(f"verification command looks mutating: {self.command!r}")


@dataclass
class Finding:
    rule_id: str
    title: str
    summary: str
    classification: str
    evidence_level: str
    severity: str
    provider: str | None
    currency: str | None
    billing_period: str | None
    period_finality: str
    observed_cost: Decimal | None = None
    observed_effective_cost: Decimal | None = None
    cost_basis: str | None = None
    billing_account_id: str | None = None
    sub_account_id: str | None = None
    resource_id: str | None = None
    resource_id_source: str | None = None
    region: str | None = None
    service: str | None = None
    matched_rows: int = 0
    source_sha256: list[str] = field(default_factory=list)
    sample_rows: list[dict[str, int]] = field(default_factory=list)
    matched_values: list[str] = field(default_factory=list)
    unknown_information: list[str] = field(default_factory=list)
    potential_false_positives: list[str] = field(default_factory=list)
    verification_steps: list[VerificationStep] = field(default_factory=list)
    owner_routing: dict[str, Any] = field(default_factory=lambda: {"status": "unattributed"})
    details: dict[str, Any] = field(default_factory=dict)
    verified_savings: Decimal | None = None
    review_status: str = "unreviewed"
    action_risk: str = "unknown"
    approval_history: list[dict] = field(default_factory=list)
    verified_outcome: dict | None = None
    observed_billed_cost: Decimal | None = None

    def __post_init__(self) -> None:
        self.check()

    def check(self) -> None:
        if self.evidence_level not in LEVELS:
            raise ContractViolation(f"bad evidence level {self.evidence_level}")
        if self.evidence_level in ("L3", "L4", "L5"):
            raise ContractViolation("billing exports alone cannot produce evidence above L2")
        if self.classification not in CLASSIFICATIONS:
            raise ContractViolation(f"bad classification {self.classification}")
        if self.period_finality not in FINALITY:
            raise ContractViolation(f"bad period finality {self.period_finality}")
        if self.severity not in SEVERITY or self.review_status not in REVIEW_STATUS:
            raise ContractViolation("bad severity or review status")
        if self.action_risk not in ACTION_RISK:
            raise ContractViolation("bad action risk")
        if self.evidence_level in ("L1", "L2") and self.verified_savings is not None:
            raise ContractViolation("verified_savings must be null at L1 and L2")
        if self.evidence_level == "L2":
            if not self.unknown_information or not self.potential_false_positives:
                raise ContractViolation("L2 findings must disclose unknowns and false positives")
            if not self.verification_steps:
                raise ContractViolation("L2 findings need at least one read-only verification step")
        if self.verified_outcome is not None:
            raise ContractViolation("verified outcomes are recorded by people at L5, never inferred")

    def identity(self) -> dict[str, Any]:
        """The fields that make a finding *this* finding (stable across runs)."""
        return {
            "rule_id": self.rule_id,
            "provider": self.provider,
            "billing_account_id": self.billing_account_id,
            "sub_account_id": self.sub_account_id,
            "resource_id": self.resource_id,
            "region": self.region,
            "billing_period": self.billing_period,
            "currency": self.currency,
            "cost_basis": self.cost_basis,
            "key": self.details.get("key"),
        }

    @property
    def finding_id(self) -> str:
        blob = json.dumps(self.identity(), sort_keys=True, separators=(",", ":"))
        return "clf_" + hashlib.sha256(blob.encode()).hexdigest()[:20]

    def to_dict(self, *, report_run_id: str, reproducibility: dict) -> dict[str, Any]:
        start, end = _period_bounds(self.billing_period)
        return {
            "schema_version": SCHEMA_VERSION,
            "finding_id": self.finding_id,
            "report_run_id": report_run_id,
            "rule_id": self.rule_id,
            "title": self.title,
            "summary": self.summary,
            "classification": self.classification,
            "evidence_level": self.evidence_level,
            "severity": self.severity,
            "provider": self.provider,
            "billing_account_id": self.billing_account_id,
            "sub_account_id": self.sub_account_id,
            "resource_id": self.resource_id,
            "resource_id_source": self.resource_id_source,
            "region": self.region,
            "service": self.service,
            "billing_period": self.billing_period,
            "billing_period_start": start,
            "billing_period_end": end,
            "period_finality": self.period_finality,
            "currency": self.currency,
            "financials": {
                "cost_basis": self.cost_basis,
                "observed_cost": fmt(self.observed_cost),
                "observed_billed_cost": fmt(self.observed_billed_cost),
                "observed_effective_cost": fmt(self.observed_effective_cost),
                "verified_savings": None,
            },
            "exact_evidence": {
                "rule_id": self.rule_id,
                "matched_rows": self.matched_rows,
                "matched_values": sorted(self.matched_values)[:20],
                "source_sha256": sorted(self.source_sha256),
                "sample_rows": self.sample_rows[:10],
            },
            "unknown_information": list(self.unknown_information),
            "potential_false_positives": list(self.potential_false_positives),
            "required_verification_steps": [asdict(v) for v in self.verification_steps],
            "owner_routing": self.owner_routing,
            "details": _jsonable(self.details),
            "review_status": self.review_status,
            "action_risk": self.action_risk,
            "approval_history": list(self.approval_history),
            "verified_outcome": None,
            "reproducibility": reproducibility,
        }


def _period_bounds(period: str | None) -> tuple[str | None, str | None]:
    if not period or len(period) != 7:
        return None, None
    year, month = int(period[:4]), int(period[5:])
    ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
    return f"{year:04d}-{month:02d}-01T00:00:00Z", f"{ny:04d}-{nm:02d}-01T00:00:00Z"


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return fmt(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def sort_key(f: Finding) -> tuple:
    sev = {"high": 0, "medium": 1, "low": 2, "info": 3}[f.severity]
    cost = -(f.observed_cost or f.observed_effective_cost or Decimal(0))
    return (sev, cost, f.rule_id, f.finding_id)
