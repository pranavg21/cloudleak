"""Provider-aware billing signatures used to route charges for investigation.

A billing SKU establishes what was charged, not whether a resource can be
removed. Only an explicitly idle AWS IPv4 usage type is treated as evidence of
historical idle charges. Every other match requires live validation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

ALL_PROVIDERS = ("Azure", "AWS", "GCP", "Generic-FOCUS")


@dataclass(frozen=True)
class WasteRule:
    key: str
    title: str
    providers: tuple[str, ...]
    service_pattern: str
    sub_service_pattern: str
    evidence_status: str
    rationale: str
    verification: str
    exclude_pattern: Optional[str] = None


RULES: list[WasteRule] = [
    WasteRule(
        key="aws_idle_ipv4",
        title="AWS idle IPv4 charges",
        providers=("AWS",),
        service_pattern=r"amazonvpc|amazonec2|ec2|network",
        sub_service_pattern=r"(?:publicipv4:idleaddress|elasticip:idleaddress)",
        evidence_status="idle_charge_observed",
        rationale="The AWS usage type explicitly denotes idle address-hours during the billing period.",
        verification="Confirm the address is still idle, identify the owner, and check dependencies before release.",
    ),
    WasteRule(
        key="disk_capacity",
        title="Disk capacity charges",
        providers=ALL_PROVIDERS,
        service_pattern=r"storage|compute|amazonec2|ec2|block",
        sub_service_pattern=r"disk|ebs[:\s]*volume|volumeusage|pd[-\s]|persistentdisk|storage pd",
        exclude_pattern=r"snapshot|image",
        evidence_status="needs_verification",
        rationale="Provisioned disk capacity does not establish attachment or workload activity.",
        verification="Check attachment state, retention, owner and dependent workloads in the provider console.",
    ),
    WasteRule(
        key="public_ip_review",
        title="Public IP address charges",
        providers=("Azure", "GCP", "Generic-FOCUS", "AWS"),
        service_pattern=r"network|compute|amazonec2|ec2|amazonvpc|virtual network",
        sub_service_pattern=r"elasticip|idleaddress|public\s*ip|static\s*ip|ip addresses|ipv4",
        exclude_pattern=r"inuseaddress|idleaddress",  # Only explicit AWS idle usage is classified above.
        evidence_status="needs_verification",
        rationale="A charge for a public IP does not establish that the address is unattached.",
        verification="Verify association, actual traffic, ownership and whether release is safe.",
    ),
    WasteRule(
        key="snapshot_storage",
        title="Snapshot storage charges",
        providers=ALL_PROVIDERS,
        service_pattern=r"storage|compute|amazonec2|ec2|backup",
        sub_service_pattern=r"snapshot|image storage|ami storage",
        evidence_status="needs_verification",
        rationale="Snapshot charges do not establish age, retention policy or backup dependencies.",
        verification="Inspect retention rules, restore dependencies and owner approvals.",
    ),
    WasteRule(
        key="load_balancer_spend",
        title="Load balancer charges",
        providers=ALL_PROVIDERS,
        service_pattern=r"network|load balancer|elasticloadbalancing|awselb|amazonec2|compute",
        sub_service_pattern=r"loadbalancer|load balancer|\blb\b|application gateway|lbcapacityunit|lcuusage",
        evidence_status="needs_verification",
        rationale="Billed hours or capacity units do not prove that the load balancer has no useful traffic.",
        verification="Check listeners, targets, request/byte metrics, topology and owner approval.",
    ),
    WasteRule(
        key="premium_storage",
        title="Premium storage tier charges",
        providers=("Azure", "AWS", "GCP"),
        service_pattern=r"storage|amazons3|blob",
        sub_service_pattern=r"premium|provisioned|ssd|io1|io2|ultra",
        evidence_status="needs_verification",
        rationale="Premium storage may be required for latency or throughput; billing does not show access patterns.",
        verification="Compare measured performance requirements and access patterns before a tier change.",
    ),
]


def rules_for(provider: str) -> list[WasteRule]:
    return [rule for rule in RULES if provider in rule.providers]
