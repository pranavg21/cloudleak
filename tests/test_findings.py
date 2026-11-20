"""Evidence contract and the adversarial false-positive suite."""

from __future__ import annotations

import json
from decimal import Decimal
from importlib import resources

import jsonschema
import pytest

from cloudleak import report as rpt
from cloudleak.engine import Workspace
from cloudleak.findings import ContractViolation, Finding, VerificationStep
from cloudleak.rules import RULES, run_rules
from tests.conftest import AWS_COLS, aws_month, aws_row, write_csv


@pytest.fixture(scope="module")
def demo_findings(demo_ws):
    return run_rules(demo_ws)


def by_rule(findings, rid):
    return [f for f in findings if f.rule_id == rid]


def test_every_l2_invariant(demo_findings):
    assert demo_findings
    for f in demo_findings:
        assert f.evidence_level == "L2"
        assert f.verified_savings is None
        assert f.unknown_information and f.potential_false_positives and f.verification_steps
        assert all(s.action_risk == "none_readonly" for s in f.verification_steps)
        assert f.observed_cost > 0


def test_idle_ipv4_exact_and_in_use_never_flagged(demo_findings):
    idle = by_rule(demo_findings, "CL-SIG-001")
    assert {f.resource_id for f in idle} == {"eipalloc-0idle00001"}
    assert sorted(f.observed_cost for f in idle) == [Decimal("3.60"), Decimal("3.72")]


def test_expensive_gpu_and_attached_disks_are_not_findings(demo_findings):
    for f in demo_findings:
        assert "p4d" not in " ".join(f.matched_values)
        assert not (f.resource_id or "").startswith("vol-0prod")  # gp3 production volumes
        assert "i-0gputrain" not in (f.resource_id or "")


def test_unused_commitment_amounts(demo_findings):
    u = by_rule(demo_findings, "CL-SIG-006")
    amounts = {(f.provider, f.billing_period): f.observed_cost for f in u}
    assert amounts[("aws", "2026-09")] == Decimal("21.60")
    assert amounts[("azure", "2026-09")] == Decimal("87.50")
    assert amounts[("synthetic cloud", "2026-10")] == Decimal("139.50")  # FOCUS: EffectiveCost of Unused rows


def test_no_duplicate_findings_across_azure_bases(demo_findings):
    ids = [f.finding_id for f in demo_findings]
    assert len(ids) == len(set(ids))
    nat = [f for f in by_rule(demo_findings, "CL-SIG-002") if f.provider == "azure"]
    assert {f.cost_basis for f in nat} == {"billed"}


def test_finality_attached(demo_findings):
    aws = {f.billing_period: f.period_finality for f in by_rule(demo_findings, "CL-SIG-001")}
    assert aws == {"2026-09": "finalized", "2026-10": "preliminary_estimate"}


def test_commands_are_shell_quoted(tmp_path):
    hostile = "eipalloc-1; rm -rf $HOME"
    p = write_csv(tmp_path / "x.csv", AWS_COLS, aws_month([aws_row(
        line_item_product_code="AmazonVPC", line_item_usage_type="USE1-PublicIPv4:IdleAddress",
        line_item_resource_id=hostile, line_item_unblended_cost="0.12")]))
    with Workspace([p]) as ws:
        f = run_rules(ws)[0]
    cmd = f.verification_steps[0].command
    assert "'eipalloc-1; rm -rf $HOME'" in cmd


def test_unattributed_owner(tmp_path):
    p = write_csv(tmp_path / "x.csv", AWS_COLS, aws_month([aws_row(
        line_item_usage_account_id="", line_item_usage_type="EBS:SnapshotUsage", line_item_resource_id="snap-1",
        line_item_unblended_cost="5")]))
    with Workspace([p]) as ws:
        f = run_rules(ws)[0]
    assert f.owner_routing["status"] == "unattributed"


def test_owner_from_tag(tmp_path):
    p = write_csv(tmp_path / "x.csv", AWS_COLS, aws_month([aws_row(
        line_item_usage_type="EBS:SnapshotUsage", line_item_resource_id="snap-1", line_item_unblended_cost="5",
        resource_tags='{"user_Owner": "team-data"}')]))
    with Workspace([p]) as ws:
        f = run_rules(ws)[0]
    assert f.owner_routing == {"status": "tag", "key": "user_Owner", "value": "team-data"}


def _f(**kw):
    base = dict(rule_id="CL-SIG-001", title="t", summary="s", classification="billing_signature_candidate",
                evidence_level="L2", severity="low", provider="aws", currency="USD", billing_period="2026-07",
                period_finality="unknown", unknown_information=["u"], potential_false_positives=["p"],
                verification_steps=[VerificationStep("look")])
    base.update(kw)
    return Finding(**base)


@pytest.mark.parametrize("kw", [
    {"verified_savings": Decimal(1)},
    {"evidence_level": "L3"},
    {"unknown_information": []},
    {"potential_false_positives": []},
    {"verification_steps": []},
    {"classification": "waste"},
])
def test_contract_violations_raise(kw):
    with pytest.raises(ContractViolation):
        _f(**kw)


@pytest.mark.parametrize("cmd", ["aws ec2 release-address --allocation-id x", "aws ec2 delete-volume --volume-id x",
                                 "gcloud compute snapshots delete x", "az vm deallocate --ids x"])
def test_mutating_commands_rejected(cmd):
    with pytest.raises(ContractViolation):
        VerificationStep("x", cmd)


def test_every_rule_has_docs_quality():
    for r in RULES:
        assert r.unknowns and r.false_positives and r.rationale
        assert r.rule_id.startswith("CL-SIG-")


def test_report_validates_against_schema(demo_ws, demo_findings):
    report = json.loads(rpt.dumps(rpt.build(demo_ws, command="audit", args={}, findings=demo_findings)))
    schema = json.loads(resources.files("cloudleak.schemas").joinpath("finding.v1.schema.json").read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    v = jsonschema.Draft202012Validator(schema)
    for f in report["findings"]:
        v.validate(f)
    for f in report["findings"]:
        assert f["financials"]["verified_savings"] is None
        assert isinstance(f["financials"]["observed_cost"], str)  # money is never a float
