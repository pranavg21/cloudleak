"""CloudLeak: offline audit for cloud billing exports.

Library use mirrors the CLI and returns the same ``cloudleak.report.v1`` dict
the JSON output contains::

    from cloudleak import audit_path, doctor_path
    report = audit_path(["exports/2026-10/"])
    for finding in report["findings"]:
        print(finding["rule_id"], finding["financials"]["observed_cost"])
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from decimal import Decimal

from cloudleak._version import __version__

__all__ = ["__version__", "audit_path", "doctor_path"]


def doctor_path(paths: Sequence[str | os.PathLike], *, memory_limit: str = "2GB") -> dict:
    """Run the data-quality checks and return a ``cloudleak.report.v1`` dict."""
    from cloudleak import report
    from cloudleak.doctor import run_doctor
    from cloudleak.engine import Workspace

    with Workspace(list(paths), memory_limit=memory_limit, allow_unknown=True) as ws:
        return report.build(ws, command="doctor", args={}, doctor=run_doctor(ws))


def audit_path(paths: Sequence[str | os.PathLike], *, memory_limit: str = "2GB",
               min_cost: str = "1", variance: bool = True) -> dict:
    """Full audit (doctor, Level-2 findings, month-over-month bridge) as a ``cloudleak.report.v1`` dict."""
    from cloudleak import report
    from cloudleak import variance as var
    from cloudleak.doctor import run_doctor
    from cloudleak.engine import Workspace
    from cloudleak.rules import run_rules

    with Workspace(list(paths), memory_limit=memory_limit) as ws:
        findings = run_rules(ws, min_cost=Decimal(min_cost))
        bridges = None
        if variance and len(var.principal_periods(ws)) >= 2:
            bridges = var.compute(ws, *var.choose_periods(ws, None, None))
            findings += var.variance_findings(bridges)
        return report.build(ws, command="audit", args={"min_cost": min_cost}, doctor=run_doctor(ws),
                            findings=findings, bridges=bridges)
