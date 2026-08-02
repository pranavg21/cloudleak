"""Waste rules, defined per cloud provider.

The first version of CloudLeak used one generic rule set matched against every
cloud. That works, but it forces every pattern to be loose enough to catch
three vendors' naming at once, which costs precision and prevents rules that
only make sense for one provider.

Each rule below declares:
  * which providers it applies to
  * regex patterns matched against the normalized service / sub-service fields
  * a reclaim factor, and why that number is what it is
  * the CLI command template for each provider that supports remediation

Reclaim factors are stated per rule and surfaced on the report. Where billing
data cannot prove the resource is idle, the factor is deliberately below 1.0.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

ALL_PROVIDERS = ("Azure", "AWS", "GCP", "Generic-FOCUS", "Generic")


@dataclass(frozen=True)
class WasteRule:
    """One waste heuristic."""

    key: str
    title: str
    providers: tuple[str, ...]
    service_pattern: str
    sub_service_pattern: str
    reclaim_factor: float
    rationale: str
    # provider -> callable(resource_id_quoted, account_quoted) -> command
    commands: Dict[str, Callable[[str, str], str]] = field(default_factory=dict)
    # Optional exclusion: rows matching this are not the thing we're looking for.
    exclude_pattern: Optional[str] = None

    def command_for(self, provider: str, resource_id: str, account: str) -> Optional[str]:
        """Build a shell-safe command, or None if unsupported for this provider."""
        builder = self.commands.get(provider)
        if builder is None:
            return None
        return builder(shlex.quote(resource_id), shlex.quote(account))


# --- Rules --------------------------------------------------------------------

RULES: List[WasteRule] = [
    WasteRule(
        key="orphaned_disk",
        title="Orphaned storage disk",
        providers=ALL_PROVIDERS,
        service_pattern=r"storage|compute|amazonec2|ec2|block",
        sub_service_pattern=(
            r"disk|ebs[:\s]*volume|volumeusage|pd[-\s]|persistentdisk|storage pd|unattached"
        ),
        # Snapshots also match 'disk' language but are a separate rule.
        exclude_pattern=r"snapshot|image",
        reclaim_factor=0.40,
        rationale=(
            "Billing data records provisioned capacity, not attachment state, so a matched "
            "disk may still be in use. A conservative share of matched spend is claimed."
        ),
        commands={
            "Azure": lambda res, acc: f"az disk delete --resource-group {acc} --name {res} --yes",
            "AWS": lambda res, acc: f"aws ec2 delete-volume --volume-id {res}",
            "GCP": lambda res, acc: f"gcloud compute disks delete {res} --project={acc} --quiet",
        },
    ),
    WasteRule(
        key="idle_public_ip",
        title="Idle public IP",
        providers=ALL_PROVIDERS,
        service_pattern=r"network|compute|amazonec2|ec2|virtual network",
        sub_service_pattern=(
            r"elasticip|idleaddress|public ip|static ip|ip address|\beip\b|unused ip"
        ),
        reclaim_factor=1.00,
        rationale=(
            "Idle-address SKUs bill only while the address is unattached, so matched spend "
            "is claimed in full."
        ),
        commands={
            "Azure": lambda res, acc: f"az network public-ip delete --resource-group {acc} --name {res}",
            "AWS": lambda res, acc: f"aws ec2 release-address --allocation-id {res}",
            "GCP": lambda res, acc: f"gcloud compute addresses delete {res} --project={acc} --quiet",
        },
    ),
    WasteRule(
        key="stale_snapshot",
        title="Accumulated snapshots",
        providers=ALL_PROVIDERS,
        service_pattern=r"storage|compute|amazonec2|ec2|backup",
        sub_service_pattern=r"snapshot|image storage|ami storage",
        reclaim_factor=0.25,
        rationale=(
            "Snapshots are legitimate backups; age and retention policy are not visible in "
            "billing data. A small share is claimed to flag review, not deletion."
        ),
        commands={
            "Azure": lambda res, acc: f"az snapshot delete --resource-group {acc} --name {res}",
            "AWS": lambda res, acc: f"aws ec2 delete-snapshot --snapshot-id {res}",
            "GCP": lambda res, acc: f"gcloud compute snapshots delete {res} --project={acc} --quiet",
        },
    ),
    WasteRule(
        key="idle_load_balancer",
        title="Idle load balancer",
        providers=ALL_PROVIDERS,
        service_pattern=r"network|load balancer|elasticloadbalancing|amazonec2|compute",
        sub_service_pattern=r"loadbalancer|load balancer|\blb\b|application gateway|lbcapacityunit",
        reclaim_factor=0.30,
        rationale=(
            "A load balancer with no backend still bills an hourly rate, but billing data "
            "does not expose backend count. Flagged for review at a low claim rate."
        ),
        commands={
            "Azure": lambda res, acc: f"az network lb delete --resource-group {acc} --name {res}",
            "AWS": lambda res, acc: (
                f"aws elbv2 delete-load-balancer --load-balancer-arn {res}"
            ),
            "GCP": lambda res, acc: (
                f"gcloud compute forwarding-rules delete {res} --project={acc} --quiet"
            ),
        },
    ),
    WasteRule(
        key="legacy_storage_tier",
        title="Premium tier on cold storage",
        providers=("Azure", "AWS", "GCP"),
        service_pattern=r"storage|amazons3|blob",
        sub_service_pattern=r"premium|provisioned|ssd|io1|io2|ultra",
        reclaim_factor=0.20,
        rationale=(
            "Premium tiers on infrequently accessed data are often a downgrade opportunity "
            "rather than a deletion. Claimed low; the action is a tier change, not removal."
        ),
        # No delete command: the remediation is a tier change, which is
        # environment-specific and unsafe to auto-generate.
        commands={},
    ),
]

RULES_BY_KEY: Dict[str, WasteRule] = {rule.key: rule for rule in RULES}


def rules_for(provider: str) -> List[WasteRule]:
    """Rules that apply to a given provider."""
    return [rule for rule in RULES if provider in rule.providers]
