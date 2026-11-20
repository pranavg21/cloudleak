"""Level-2 billing signatures: charges whose *billing line itself* names a pattern.

A rule here fires only on provider-explicit evidence in the bill: an AWS usage
type that literally says ``IdleAddress``, a meter that says ``Data
Processed`` by a NAT gateway, a charge type that says ``UnusedReservation``.
Billing data never proves a resource is unused, unattached or safe to remove,
so no rule emits a savings figure or a mutating command, and there is
deliberately no rule for "expensive disk", "large GPU" or "idle load
balancer": those cannot be distinguished from legitimate production use in a
bill.

Every verification command is read-only and every interpolated value is
shell-quoted.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal

from cloudleak.analysis import finality_lookup, owner_routing, period_statuses
from cloudleak.engine import Workspace
from cloudleak.findings import Finding, VerificationStep

RULE_PACK_VERSION = "2026.11.1"
Q = shlex.quote


@dataclass(frozen=True)
class Rule:
    rule_id: str
    slug: str
    title: str
    where: str  # SQL predicate over the `valid` view
    group: str  # resource | scope
    rationale: str
    unknowns: tuple[str, ...]
    false_positives: tuple[str, ...]
    verify: Callable[[dict], list[VerificationStep]]
    amount: str = "cost"  # SQL for the observed amount
    severity: str = "medium"
    providers: tuple[str, ...] = ("aws", "azure", "gcp", "focus")
    docs: tuple[str, ...] = field(default_factory=tuple)
    # When one scope has both actual and amortized exports, evaluate this basis only.
    prefer_basis: str = "billed"

    def fingerprint(self) -> dict:
        return {"id": self.rule_id, "where": " ".join(self.where.split()), "group": self.group,
                "amount": self.amount}


def _region(r: dict) -> str:
    return f" --region {Q(r['region'])}" if r.get("region") and re.fullmatch(r"[a-z0-9-]+", r["region"]) else ""


def _console(text: str) -> VerificationStep:
    return VerificationStep(description=text, tool="console")


def _verify_ipv4(r: dict) -> list[VerificationStep]:
    rid = r.get("resource_id") or ""
    if rid.startswith("eipalloc-"):
        sel = f" --allocation-ids {Q(rid)}"
    elif re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", rid):
        sel = f" --public-ips {Q(rid)}"
    else:
        sel = ""
    return [
        VerificationStep("Check whether the address is associated right now (AssociationId empty means idle).",
                         f"aws ec2 describe-addresses{_region(r)}{sel} --query "
                         "'Addresses[].[AllocationId,PublicIp,AssociationId,Tags]' --output table", "aws"),
        _console("Ask the owner whether the address is allowlisted by a partner or held for failover; "
                 "a released Elastic IP cannot be recovered."),
    ]


def _verify_nat(r: dict) -> list[VerificationStep]:
    p, rid = r.get("provider"), r.get("resource_id") or ""
    if p == "aws":
        sel = f" --nat-gateway-ids {Q(rid)}" if rid.startswith("nat-") else ""
        return [
            VerificationStep("Identify the NAT gateway and its VPC.",
                             f"aws ec2 describe-nat-gateways{_region(r)}{sel} --query "
                             "'NatGateways[].[NatGatewayId,VpcId,SubnetId,State]' --output table", "aws"),
            VerificationStep("Check whether S3/DynamoDB traffic already uses gateway endpoints in that VPC.",
                             f"aws ec2 describe-vpc-endpoints{_region(r)} --filters "
                             "Name=vpc-endpoint-type,Values=Gateway --query "
                             "'VpcEndpoints[].[VpcId,ServiceName,State]' --output table", "aws"),
            _console("Use VPC Flow Logs on the NAT gateway ENI to see which destinations drive the bytes."),
        ]
    if p == "azure":
        cmd = (f"az network nat gateway show --ids {Q(rid)} --query '{{name:name,subnets:subnets[].id}}'"
               if rid.startswith("/subscriptions/") else None)
        return [VerificationStep("Inspect the NAT gateway and the subnets routed through it.", cmd, "az"),
                _console("Check whether Azure services are reached via Private Endpoints or Service Endpoints.")]
    if p == "gcp":
        proj = r.get("sub_account_id")
        cmd = f"gcloud compute routers list --project {Q(proj)}" if proj else None
        return [VerificationStep("List Cloud Routers carrying Cloud NAT configurations.", cmd, "gcloud"),
                _console("Check Private Google Access on the subnets that send traffic through Cloud NAT.")]
    return [_console("Identify the NAT resource and the workloads routing through it.")]


def _verify_cross_zone(r: dict) -> list[VerificationStep]:
    if r.get("provider") == "aws":
        return [VerificationStep(
            "List instances by Availability Zone to find chatty services split across zones.",
            f"aws ec2 describe-instances{_region(r)} --query "
            "'Reservations[].Instances[].[InstanceId,Placement.AvailabilityZone,Tags[?Key==`Name`].Value|[0]]' "
            "--output table", "aws"),
            _console("Enable VPC Flow Logs to attribute inter-zone bytes to source/destination pairs.")]
    proj = r.get("sub_account_id")
    if r.get("provider") == "gcp" and proj:
        return [VerificationStep("List instances by zone.",
                                 f"gcloud compute instances list --project {Q(proj)} --format='table(name,zone)'",
                                 "gcloud")]
    return [_console("Map which services talk across zones before changing placement; "
                     "multi-zone traffic is often required for availability.")]


def _verify_logs(r: dict) -> list[VerificationStep]:
    p = r.get("provider")
    if p == "aws":
        return [VerificationStep(
            "Rank log groups by stored bytes and check retention.",
            f"aws logs describe-log-groups{_region(r)} --query "
            "'reverse(sort_by(logGroups,&storedBytes))[:20].[logGroupName,storedBytes,retentionInDays]' "
            "--output table", "aws")]
    if p == "azure":
        rid = r.get("resource_id") or ""
        cmd = f"az monitor log-analytics workspace show --ids {Q(rid)}" if rid.startswith("/subscriptions/") else None
        return [VerificationStep("Inspect the workspace's retention and daily cap.", cmd, "az"),
                _console("Query the Usage table to rank data types by billable ingestion volume.")]
    if p == "gcp":
        proj = r.get("sub_account_id")
        cmd = f"gcloud logging sinks list --project {Q(proj)}" if proj else None
        return [VerificationStep("List log sinks and exclusion filters.", cmd, "gcloud")]
    return [_console("Identify the top log sources by ingested volume.")]


def _verify_snapshot(r: dict) -> list[VerificationStep]:
    p, rid = r.get("provider"), r.get("resource_id") or ""
    if p == "aws":
        sel = f" --snapshot-ids {Q(rid)}" if rid.startswith("snap-") else " --owner-ids self"
        return [VerificationStep("Check snapshot age, source volume and tags.",
                                 f"aws ec2 describe-snapshots{_region(r)}{sel} --query "
                                 "'Snapshots[].[SnapshotId,VolumeId,StartTime,VolumeSize,Tags]' --output table",
                                 "aws"),
                _console("Confirm the retention policy (backup plan, compliance requirement) that covers it.")]
    if p == "azure" and rid.startswith("/subscriptions/"):
        return [VerificationStep("Check the snapshot's creation time and source disk.",
                                 f"az snapshot show --ids {Q(rid)} --query "
                                 "'{created:timeCreated,source:creationData.sourceResourceId,sizeGb:diskSizeGb}'",
                                 "az"),
                _console("Confirm the retention policy that covers it.")]
    if p == "gcp":
        name, proj = rid.rsplit("/", 1)[-1], r.get("sub_account_id")
        cmd = (f"gcloud compute snapshots describe {Q(name)} --project {Q(proj)}"
               if name and proj else None)
        return [VerificationStep("Check the snapshot's creation time and source disk.", cmd, "gcloud"),
                _console("Confirm the snapshot schedule's retention policy.")]
    return [_console("Confirm retention requirements before changing snapshot policy.")]


def _verify_commitment(r: dict) -> list[VerificationStep]:
    p = r.get("provider")
    if p == "aws":
        return [VerificationStep("List active reservations and their remaining term.",
                                 f"aws ec2 describe-reserved-instances{_region(r)} --filters Name=state,Values=active "
                                 "--query 'ReservedInstances[].[ReservedInstancesId,InstanceType,InstanceCount,End]' "
                                 "--output table", "aws"),
                _console("Check whether instance-size flexibility or an exchange could absorb the unused share.")]
    if p == "azure":
        return [VerificationStep("List reservations and utilization.",
                                 "az reservations reservation-order list --query "
                                 "'[].{order:name,display:displayName,term:term}' --output table", "az"),
                _console("Review utilization under Reservations in the Azure portal before exchanging.")]
    return [_console("Review commitment utilization in the provider console before acting.")]


def _verify_gp2(r: dict) -> list[VerificationStep]:
    rid = r.get("resource_id") or ""
    sel = f" --volume-ids {Q(rid)}" if rid.startswith("vol-") else " --filters Name=volume-type,Values=gp2"
    return [VerificationStep("Check size, IOPS, throughput and attachment of the gp2 volume.",
                             f"aws ec2 describe-volumes{_region(r)}{sel} --query "
                             "'Volumes[].[VolumeId,Size,Iops,State,Attachments[0].InstanceId]' --output table",
                             "aws"),
            _console("Compare the volume's peak IOPS/throughput in CloudWatch with gp3 baselines "
                     "before changing type.")]


def _verify_extended_support(r: dict) -> list[VerificationStep]:
    rid, svc = r.get("resource_id") or "", (r.get("service") or "").lower()
    if "rds" in svc:
        ident = rid.rsplit(":", 1)[-1] if rid.startswith("arn:") else rid
        sel = f" --db-instance-identifier {Q(ident)}" if ident else ""
        return [VerificationStep("Confirm the engine version that is incurring extended support.",
                                 f"aws rds describe-db-instances{_region(r)}{sel} --query "
                                 "'DBInstances[].[DBInstanceIdentifier,Engine,EngineVersion]' --output table",
                                 "aws"),
                _console("Plan an engine upgrade with the owning team; extended support exists for workloads "
                         "that cannot upgrade yet.")]
    if "eks" in svc:
        name = rid.rsplit("/", 1)[-1] if rid else ""
        cmd = f"aws eks describe-cluster{_region(r)} --name {Q(name)} --query 'cluster.version'" if name else None
        return [VerificationStep("Confirm the cluster's Kubernetes version.", cmd, "aws")]
    return [_console("Identify the resource running an end-of-standard-support version.")]


RULES: list[Rule] = [
    Rule(
        "CL-SIG-001", "aws-idle-public-ipv4", "Idle public IPv4 address charges",
        "provider = 'aws' AND (sku ILIKE '%PublicIPv4:IdleAddress%' OR sku ILIKE '%ElasticIP:IdleAddress%')",
        "resource",
        "AWS bills idle addresses under an explicit IdleAddress usage type; the charge itself records that "
        "the address was allocated but not associated during the billed hours.",
        ("Whether the address is still idle today", "Whether it is allowlisted by a customer, partner or firewall",
         "Whether it is reserved for disaster-recovery failover"),
        ("Addresses held for DR failover or blue/green cutover",
         "Addresses allowlisted by third parties that cannot be re-issued"),
        _verify_ipv4, severity="low", providers=("aws",),
        docs=("https://docs.aws.amazon.com/vpc/latest/userguide/vpc-billing-usage-reports.html",),
    ),
    Rule(
        "CL-SIG-002", "nat-gateway-data-processing", "NAT gateway data-processing charges",
        """(provider = 'aws' AND sku ILIKE '%NatGateway-Bytes%')
           OR (provider = 'azure' AND service ILIKE '%NAT Gateway%' AND sku ILIKE '%Data Processed%')
           OR (provider = 'gcp' AND sku ILIKE '%NAT%Data Processing%')
           OR (provider NOT IN ('aws','azure','gcp') AND signature_text LIKE '%nat%'
               AND signature_text LIKE '%process%')""",
        "resource",
        "Per-GB NAT processing is billed on every byte routed through the gateway, including traffic to the "
        "provider's own storage and database services that could use private endpoints instead.",
        ("Which workloads and destinations generate the bytes",
         "Whether gateway or private endpoints already exist for the main destinations"),
        ("Internet egress that genuinely requires NAT", "Traffic patterns that were temporary (migrations, backfills)"),
        _verify_nat,
    ),
    Rule(
        "CL-SIG-003", "cross-zone-transfer", "Regional (inter-zone) data transfer charges",
        """(provider = 'aws' AND sku ILIKE '%DataTransfer-Regional-Bytes%')
           OR (provider = 'gcp' AND sku ILIKE '%Inter Zone%')""",
        "scope",
        "AWS bills DataTransfer-Regional-Bytes for traffic between availability zones (in each direction) and "
        "for same-region traffic over public or Elastic IPs; GCP bills inter-zone egress. Chatty services split "
        "across zones generate this cost silently.",
        ("Which services exchange the traffic", "Whether the cross-zone traffic is required for availability"),
        ("Replication or quorum traffic that exists for high availability",),
        _verify_cross_zone, severity="low", providers=("aws", "gcp"),
    ),
    Rule(
        "CL-SIG-004", "log-ingestion", "Log ingestion charges",
        """(provider = 'aws' AND service ILIKE 'AmazonCloudWatch'
                AND (sku ILIKE '%DataProcessing-Bytes%' OR sku ILIKE '%VendedLog-Bytes%'))
           OR (provider = 'azure' AND service ILIKE '%Log Analytics%' AND sku ILIKE '%Ingestion%')
           OR (provider = 'gcp' AND service ILIKE '%Logging%' AND sku ILIKE '%Log Volume%')""",
        "scope",
        "Log ingestion is billed per GB written, independent of whether anyone queries the logs.",
        ("Which log sources produce the volume", "Retention and regulatory requirements for those logs"),
        ("Audit or security logs that must be retained", "Temporary debug logging during an incident"),
        _verify_logs, severity="low",
    ),
    Rule(
        "CL-SIG-005", "snapshot-storage", "Snapshot storage charges",
        """(provider = 'aws' AND sku ILIKE '%SnapshotUsage%')
           OR (provider = 'azure' AND sku ILIKE '%Snapshot%')
           OR (provider = 'gcp' AND sku ILIKE '%Snapshot%')
           OR (provider NOT IN ('aws','azure','gcp') AND signature_text LIKE '%snapshot%')""",
        "resource",
        "Snapshot storage accumulates silently; the bill shows how much is retained, not whether it is needed.",
        ("The retention policy that created the snapshots", "Whether the source volume still exists",
         "Legal, regulatory or backup obligations attached to them"),
        ("Compliance retention (HIPAA, PCI DSS, SOC 2, legal hold)", "Disaster-recovery restore points"),
        _verify_snapshot, severity="low",
    ),
    Rule(
        "CL-SIG-006", "unused-commitment", "Unused commitment charges",
        """(provider = 'aws' AND charge_raw = 'RIFee' AND commitment_status = 'unused')
           OR (provider = 'azure' AND lower(charge_raw) IN ('unusedreservation', 'unusedsavingsplan'))
           OR (provider NOT IN ('aws','azure') AND commitment_status = 'unused')""",
        "resource",
        "The bill explicitly attributes this amount to commitment capacity that no usage consumed.",
        ("Planned usage that will absorb the commitment later in its term",
         "Exchange, instance-size-flexibility or resale options"),
        ("Commitments bought ahead of a planned ramp-up", "Temporary dips in covered usage"),
        _verify_commitment, severity="medium", prefer_basis="amortized",
        amount="CASE WHEN provider = 'aws' THEN effective_cost "
               "WHEN provider NOT IN ('aws','azure') THEN COALESCE(effective_cost, cost) ELSE cost END",
    ),
    Rule(
        "CL-SIG-007", "aws-ebs-gp2", "EBS gp2 volume charges (storage-tier review)",
        "provider = 'aws' AND sku ILIKE '%EBS:VolumeUsage.gp2%'",
        "resource",
        "The usage type records gp2 volumes. gp3 has a lower list price per GB with independently provisioned "
        "IOPS and throughput, so gp2 volumes are worth a tier review.",
        ("The volume's IOPS and throughput profile", "Whether the volume is still attached and in use"),
        ("Volumes whose burst profile depends on gp2 size-based IOPS", "Volumes scheduled for deletion anyway"),
        _verify_gp2, severity="low", providers=("aws",),
    ),
    Rule(
        "CL-SIG-008", "aws-extended-support", "Extended support surcharges",
        "provider = 'aws' AND sku ILIKE '%ExtendedSupport%'",
        "resource",
        "AWS bills an explicit surcharge for running engine or Kubernetes versions past standard support.",
        ("Why the version has not been upgraded", "Upgrade testing effort and risk"),
        ("Workloads intentionally pinned while an upgrade is scheduled",),
        _verify_extended_support, severity="medium", providers=("aws",),
    ),
]


def rule_pack_hash() -> str:
    blob = json.dumps({"version": RULE_PACK_VERSION, "rules": [r.fingerprint() for r in RULES]},
                      sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def run_rules(ws: Workspace, *, min_cost: Decimal = Decimal("1"), max_per_rule: int = 200) -> list[Finding]:
    finality = finality_lookup(period_statuses(ws))
    sha = {s.file.source_id: s.file.sha256 for s in ws.sources}
    out: list[Finding] = []
    for rule in RULES:
        if rule.group == "resource":
            key = "COALESCE(resource_id, '')"
        else:
            key = "''"
        rows = ws.rows(f"""
            SELECT provider, billing_account_id, sub_account_id, any_value(sub_account_name) AS sub_account_name,
                   billing_period, currency, cost_basis, region, any_value(service) AS service,
                   {key} AS rkey, NULLIF({key}, '') AS resource_id,
                   sum({rule.amount}) AS amount, sum(cost) AS billed,
                   sum(effective_cost) AS effective,
                   sum(usage_quantity) AS quantity, any_value(usage_unit) AS unit,
                   count(*) AS n,
                   list(DISTINCT source_id ORDER BY source_id) AS sources,
                   list(DISTINCT sku ORDER BY sku)[1:20] AS skus,
                   (list(struct_pack(s := source_id, r := source_row) ORDER BY source_id, source_row))[1:5]
                       AS samples,
                   arg_max(tags, cost) AS tags
            FROM valid v
            WHERE ({rule.where})
              AND (cost_basis = ? OR NOT EXISTS (
                    SELECT 1 FROM valid o
                    WHERE o.cost_basis = ? AND o.provider = v.provider
                      AND o.sub_account_id IS NOT DISTINCT FROM v.sub_account_id
                      AND o.billing_period IS NOT DISTINCT FROM v.billing_period))
            GROUP BY provider, billing_account_id, sub_account_id, billing_period, currency, cost_basis, region,
                     rkey
            HAVING sum({rule.amount}) >= ?
            ORDER BY amount DESC, rkey, billing_period
        """, [rule.prefer_basis, rule.prefer_basis, min_cost])
        for r in rows[:max_per_rule]:
            fin = finality.get((r["provider"], r["billing_account_id"], r["billing_period"]), "unknown")
            where = r["resource_id"] or ", ".join(x for x in (r["sub_account_id"], r["region"]) if x) or "the export"
            out.append(Finding(
                rule_id=rule.rule_id,
                title=rule.title,
                summary=f"{rule.title} on {where} in {r['billing_period']}.",
                classification="billing_signature_candidate",
                evidence_level="L2",
                severity=rule.severity,
                provider=r["provider"], currency=r["currency"], billing_period=r["billing_period"],
                period_finality=fin,
                observed_cost=r["amount"],
                observed_effective_cost=r["effective"],
                cost_basis=r["cost_basis"],
                billing_account_id=r["billing_account_id"], sub_account_id=r["sub_account_id"],
                resource_id=r["resource_id"],
                resource_id_source="billing_line_item" if r["resource_id"] else None,
                region=r["region"], service=r["service"],
                matched_rows=r["n"],
                source_sha256=[sha[i] for i in r["sources"]],
                sample_rows=[{"source_id": x["s"], "row": x["r"]} for x in r["samples"]],
                matched_values=[x for x in r["skus"] if x],
                unknown_information=list(rule.unknowns),
                potential_false_positives=list(rule.false_positives),
                verification_steps=rule.verify(r),
                owner_routing=owner_routing(r["tags"], r["sub_account_id"], r["sub_account_name"]),
                action_risk="unknown",
                details={"key": r["rkey"], "rationale": rule.rationale, "slug": rule.slug,
                         "usage_quantity": r["quantity"], "usage_unit": r["unit"], "docs": list(rule.docs),
                         "billed_cost_of_matched_rows": r["billed"]},
            ))
        if len(rows) > max_per_rule:
            rest = rows[max_per_rule:]
            out[-1].details["truncated_groups"] = len(rest)
    return out
