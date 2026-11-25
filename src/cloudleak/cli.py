"""Command-line interface.

Exit codes:  0 success · 1 completed, and the result meets the --fail-on policy
(failed checks, a reconciliation difference, findings) · 2 input cannot be
processed safely (fail closed) or usage error.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import traceback
from decimal import Decimal
from pathlib import Path

from cloudleak._version import __version__
from cloudleak.errors import EXIT_FINDINGS, EXIT_INPUT, EXIT_OK, CloudLeakError, InputError

EPILOG = ("Runs entirely offline. No cloud credentials, no telemetry, no network access.\n"
          "Report a provider schema quirk using synthetic headers: "
          "https://github.com/pranavg21/cloudleak/issues")


# ---------------------------------------------------------------- helpers
def _workspace(args, paths, **kw):
    from cloudleak.engine import Workspace
    return Workspace(paths, memory_limit=args.memory_limit, threads=args.threads, temp_dir=args.temp_dir,
                     max_decompressed=int(args.max_decompressed_gb * (1 << 30)), **kw)


def _kind(path: str) -> str:
    low = path.lower()
    if low.endswith((".sarif", ".sarif.json")):
        return "sarif"
    for ext, kind in ((".html", "html"), (".htm", "html"), (".json", "json"), (".md", "md"),
                      (".markdown", "md"), (".csv", "csv"), (".xml", "junit")):
        if low.endswith(ext):
            return kind
    raise InputError(f"cannot tell the output format of {path!r}",
                     hint="Use .html, .json, .md, .csv, .sarif or .xml (JUnit, doctor only).")


def _render(kind: str, report: dict) -> str:
    from cloudleak.render import formats
    from cloudleak.report import dumps
    if kind == "html":
        from cloudleak.render.html import render
        return render(report)
    if kind == "json":
        return dumps(report)
    if kind == "md":
        return formats.markdown(report)
    if kind == "csv":
        return formats.reconciliation_csv(report) if "reconciliation" in report else formats.findings_csv(report)
    if kind == "sarif":
        return json.dumps(formats.sarif(report), indent=2, sort_keys=True) + "\n"
    if kind == "junit":
        if not report.get("doctor"):
            raise InputError("JUnit output is available for doctor results only")
        return formats.junit(report)
    raise InputError(f"unknown format {kind}")


def _emit_outputs(args, report: dict, *workspaces) -> None:
    from cloudleak.publication import publish_reports

    paths = args.output or []
    inputs = [s.file.path for ws in workspaces for s in ws.sources]
    inputs.extend(m.path for ws in workspaces for m in ws.discovery.manifests)
    if getattr(args, "invoices", None):
        inputs.append(Path(args.invoices))

    def render(path: str) -> str:
        kind = _kind(path)
        if args.command == "diff" and kind != "json":
            raise InputError("diff writes JSON output only")
        if kind == "junit" and args.command != "doctor":
            raise InputError("JUnit output is available for doctor results only")
        return _render(kind, report)

    publish_reports(paths, inputs, render)
    for out in paths:
        _say(args, f"wrote {out}")


def _say(args, msg: str) -> None:
    if not getattr(args, "quiet", False):
        print(msg, file=sys.stderr)


def _color(args) -> bool:
    return not args.no_color and sys.stdout.isatty() and "NO_COLOR" not in os.environ


def _stdout(args, report: dict, default_text) -> None:
    fmt = getattr(args, "format", "text")
    if fmt == "text":
        if not args.quiet:
            sys.stdout.write(default_text(report))
    else:
        sys.stdout.write(_render(fmt, report))


# ---------------------------------------------------------------- commands
def cmd_doctor(args) -> int:
    from cloudleak import report as rpt
    from cloudleak.doctor import run_doctor
    from cloudleak.render.formats import doctor_text
    with _workspace(args, args.paths, allow_unknown=True) as ws:
        doc = run_doctor(ws)
        report = rpt.build(ws, command="doctor", args={"fail_on": args.fail_on}, doctor=doc)
        _emit_outputs(args, report, ws)
        _stdout(args, report, lambda r: doctor_text(r, verbose=args.verbose, color=_color(args)))
    return _policy(args.fail_on, doc.worst)


def _policy(fail_on: str, worst: str, findings: int = 0) -> int:
    if fail_on == "never":
        return EXIT_OK
    if fail_on == "findings":
        return EXIT_FINDINGS if findings or worst == "FAIL" else EXIT_OK
    if fail_on == "warn":
        return EXIT_FINDINGS if worst in ("WARN", "FAIL") else EXIT_OK
    return EXIT_FINDINGS if worst == "FAIL" else EXIT_OK


def cmd_audit(args) -> int:
    from cloudleak import report as rpt
    from cloudleak import variance as var
    from cloudleak.doctor import run_doctor
    from cloudleak.render.formats import audit_text
    from cloudleak.rules import run_rules
    with _workspace(args, args.paths) as ws:
        doc = run_doctor(ws)
        findings = run_rules(ws, min_cost=Decimal(args.min_cost))
        bridges = None
        if len(var.principal_periods(ws)) >= 2 and not args.no_variance:
            base, cur = var.choose_periods(ws, None, None)
            bridges = var.compute(ws, base, cur)
            findings += var.variance_findings(bridges)
        report = rpt.build(ws, command="audit", args={"min_cost": Decimal(args.min_cost),
                           "no_variance": args.no_variance, "fail_on": args.fail_on}, doctor=doc,
                           findings=findings, bridges=bridges)
        _emit_outputs(args, report, ws)
        _stdout(args, report, audit_text)
    sig = sum(1 for f in findings if f.evidence_level == "L2")
    return _policy(args.fail_on, doc.worst, sig)


def cmd_variance(args) -> int:
    from cloudleak import report as rpt
    from cloudleak import variance as var
    paths = [args.base_path] + ([args.current_path] if args.current_path else [])
    if args.top < 1:
        raise InputError("--top must be positive")
    with _workspace(args, paths, independent_inputs=bool(args.current_path)) as ws:
        base_ids = ws.source_ids_for(args.base_path)
        cur_ids = ws.source_ids_for(args.current_path) if args.current_path else base_ids
        base, cur = var.choose_periods(ws, args.base, args.current,
                                       base_ids if args.current_path else None,
                                       cur_ids if args.current_path else None)
        where = var.comparison_filter(base, cur, base_ids, cur_ids)
        bridges = var.compute(ws, base, cur, top=args.top, base_sources=base_ids, current_sources=cur_ids)
        findings = var.variance_findings(bridges)
        arguments = {"base": base, "current": cur, "base_source_ids": base_ids, "current_source_ids": cur_ids,
                     "top": args.top}
        report = rpt.build(ws, command="variance", args=arguments, findings=findings, bridges=bridges, where=where)
        _emit_outputs(args, report, ws)
        _stdout(args, report, lambda r: "\n\n".join(b["narrative"] for b in r["variance"]) + "\n")
    return EXIT_OK


def cmd_reconcile(args) -> int:
    from dataclasses import asdict

    from cloudleak import report as rpt
    from cloudleak.reconcile import (
        Expectation,
        load_invoices,
        parse_amount,
        reconcile,
        reconciliation_findings,
    )

    if args.invoices:
        if any((args.total is not None, args.currency, args.invoice_id, args.period, args.provider,
                args.issuer, args.billing_account)):
            raise InputError("With --invoices, put the invoice totals and scope selectors in the CSV.")
        exps = load_invoices(args.invoices)
    else:
        if args.total is None or not args.currency:
            raise InputError("give --total and --currency (with --invoice-id or --period), or --invoices FILE")
        exps = [Expectation(parse_amount(args.total, "--total"), args.currency, args.invoice_id, args.period,
                            args.provider, issuer=args.issuer, billing_account=args.billing_account)]
    tol = parse_amount(args.tolerance, "--tolerance")
    if tol < 0:
        raise InputError("--tolerance cannot be negative")
    with _workspace(args, args.paths) as ws:
        results = reconcile(ws, exps, tol)
        report = rpt.build(ws, command="reconcile", args={"tolerance": tol, "cost_basis": "billed",
                           "expectations": [asdict(e) for e in exps]},
                           findings=reconciliation_findings(results),
                           extra={"reconciliation": [r.to_dict() for r in results]})
        _emit_outputs(args, report, ws)

        def text(rep):
            lines = []
            for r in rep["reconciliation"]:
                inv = r["invoice"]
                name = inv["invoice_id"] or f"{inv['provider'] or ''} {inv['period']}".strip()
                lines.append(f"{r['status'].upper():<14} {name}: export {r['export_total']} vs invoice "
                             f"{inv['total']} {inv['currency']} -> difference {r['difference']} "
                             f"(export minus invoice; {r['rows']:,} rows; period {r['period_finality']})")
                for k, v in r["components"].items():
                    lines.append(f"    {k:<28} {v:>20}")
                for n in r["notes"]:
                    lines.append(f"    note: {n}")
            return "\n".join(lines) + "\n"
        _stdout(args, report, text)
    return EXIT_FINDINGS if any(r.status in ("difference", "no_rows") for r in results) else EXIT_OK


def cmd_diff(args) -> int:
    from cloudleak import diff as d
    from cloudleak.report import dumps, generated_at, run_id, tool_info
    with _workspace(args, [args.before]) as before, _workspace(args, [args.after]) as after:
        result = d.diff(before, after)
        arguments = {"fail_on_change": args.fail_on_change}
        report = {"schema_version": "cloudleak.diff.v1", "tool": tool_info(),
                  "run": {"command": "diff", "generated_at": generated_at(), "arguments": arguments,
                          "report_run_id": run_id(before, "diff", arguments, after=after)},
                  "before": [s.to_dict() for s in before.sources], "after": [s.to_dict() for s in after.sources],
                  **result}
        _emit_outputs(args, report, before, after)
        if args.format == "json":
            sys.stdout.write(dumps(report))
        elif not args.quiet:
            for s in result["scopes"]:
                if s["status"] == "unchanged":
                    continue
                print(f"{s['status'].upper():<9} {s['provider']} {s['billing_period']} {s['currency']} "
                      f"({s['cost_basis']}): {s['before_total']} -> {s['after_total']} (delta {s['delta']}; rows "
                      f"{s['before_rows']:,} -> {s['after_rows']:,}; finality {s['before_finality']} -> "
                      f"{s['after_finality']})")
                for dim in ("by_charge_category", "by_service", "by_account"):
                    for change in s[dim][:6]:
                        print(f"    {dim[3:]} {change['item']}: {change['before']} -> {change['after']} "
                              f"({change['delta']})")
                if s.get("note"):
                    print(f"    note: {s['note']}")
            if not result["changed_scopes"]:
                print("No changes to the compared aggregates or invoice coverage.")
    return EXIT_FINDINGS if result["changed_scopes"] and args.fail_on_change else EXIT_OK


def cmd_redact(args) -> int:
    from cloudleak.redact import redact
    rep = redact(args.paths, args.output_path, key_file=args.key_file, drop_unclassified=args.drop_unclassified,
                 force=args.force, memory_limit=args.memory_limit, temp_dir=args.temp_dir, threads=args.threads,
                 max_decompressed=int(args.max_decompressed_gb * (1 << 30)))
    _say(args, f"wrote {rep.output}: {len(rep.files)} file(s), {rep.aliased_values:,} distinct identifiers "
               f"aliased, {rep.scanned_cells:,} cells passed the residual-identifier scan.")
    _say(args, f"key fingerprint {rep.key_fingerprint}. Keep the key private; `cloudleak resolve` needs it.")
    return EXIT_OK


def cmd_resolve(args) -> int:
    from cloudleak.redact import build_resolver, resolve_text
    table = build_resolver(args.source, args.key_file, memory_limit=args.memory_limit, temp_dir=args.temp_dir,
                           threads=args.threads, max_decompressed=int(args.max_decompressed_gb * (1 << 30)))
    if args.input:
        text = Path(args.input).read_text(encoding="utf-8") if args.input != "-" else sys.stdin.read()
    elif args.aliases:
        text = "\n".join(args.aliases) + "\n"
    else:
        raise InputError("give aliases to resolve or --in FILE")
    out, found, missing = resolve_text(text, table)
    sys.stdout.write(out)
    _say(args, f"resolved {found} alias(es)" + (f"; {missing} not found in the given sources" if missing else ""))
    return EXIT_OK if not missing else EXIT_FINDINGS


def cmd_demo(args) -> int:
    from cloudleak.fixtures import generate
    from cloudleak.publication import staged_demo
    from cloudleak.render.formats import audit_text

    target = Path(args.directory).expanduser()
    summary = ""
    with staged_demo(target, args.force) as stage:
        generate(stage, scale=args.scale)
        audit = argparse.Namespace(**vars(args))
        audit.paths = [str(stage / "m1"), str(stage / "m2")]
        audit.output = [str(stage / "report.html"), str(stage / "report.json")]
        audit.min_cost, audit.no_variance, audit.fail_on = "1", False, "never"
        audit.quiet, audit.format = True, "text"
        cmd_audit(audit)
        if not args.quiet or args.format != "text":
            report = json.loads((stage / "report.json").read_text(encoding="utf-8"))
            summary = audit_text(report) if args.format == "text" else _render(args.format, report)
    _say(args, f"wrote synthetic exports to {target}/m1 and {target}/m2 and invoices to {target}/invoices.csv")
    sys.stdout.write(summary)
    _say(args, f"\nOpen {target / 'report.html'} for the audit or {target / 'README.md'} for the invoice comparison.")
    return EXIT_OK


def cmd_checks(args) -> int:
    from cloudleak.doctor import CATALOG
    for c in CATALOG:
        print(f"{c.check_id:<11} {c.category:<13} {c.title}\n{'':<25}{c.description}")
    return EXIT_OK


def cmd_rules(args) -> int:
    from cloudleak.rules import RULE_PACK_VERSION, RULES, rule_pack_hash
    print(f"rule pack {RULE_PACK_VERSION} ({rule_pack_hash()}); every rule is Level 2 and read-only\n")
    for r in RULES:
        print(f"{r.rule_id}  {r.title}  [{', '.join(r.providers)}]\n{'':<12}{r.rationale}\n")
    return EXIT_OK


def cmd_schema(args) -> int:
    from importlib import resources
    name = {"finding": "finding.v1.schema.json", "report": "report.v1.schema.json"}[args.which]
    sys.stdout.write(resources.files("cloudleak.schemas").joinpath(name).read_text(encoding="utf-8"))
    return EXIT_OK


# ---------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    g = common.add_argument_group("engine")
    g.add_argument("--memory-limit", default=os.environ.get("CLOUDLEAK_MEMORY_LIMIT", "2GB"),
                   help="DuckDB managed-buffer limit (default 2GB). Process RSS can be higher.")
    g.add_argument("--threads", type=int, default=None, help="worker threads (default: all cores)")
    g.add_argument("--temp-dir", default=os.environ.get("CLOUDLEAK_TEMP_DIR"),
                   help="spill directory; point at an encrypted or RAM-backed path for sensitive data")
    g.add_argument("--max-decompressed-gb", type=float, default=64.0,
                   help="refuse .gz inputs that expand beyond this (default 64)")
    g.add_argument("-q", "--quiet", action="store_true", help="no progress or summary on the terminal")
    g.add_argument("--no-color", action="store_true", help="disable colored output (also: NO_COLOR)")

    p = argparse.ArgumentParser(
        prog="cloudleak",
        description="Offline audit for cloud billing exports (AWS CUR, Azure, GCP, FOCUS): data-quality doctor, "
                    "invoice reconciliation, month-over-month variance, evidence-graded findings and fail-closed "
                    "redaction.",
        epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"cloudleak {__version__}")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")

    def out_opt(sp, kinds: str):
        sp.add_argument("-o", "--output", action="append", metavar="FILE",
                        help=f"write a report; format from extension ({kinds}); repeatable")

    sp = sub.add_parser("doctor", parents=[common], help="check exports for completeness, finality and defects",
                        description="Deterministic data-quality, finality and FOCUS conformance-gap checks.")
    sp.add_argument("paths", nargs="+", help="export files or directories")
    sp.add_argument("--format", choices=["text", "json", "junit", "sarif", "md"], default="text")
    sp.add_argument("--fail-on", choices=["fail", "warn", "never"], default="fail",
                    help="exit 1 when any check reaches this status (default: fail)")
    sp.add_argument("-v", "--verbose", action="store_true", help="also list passing checks")
    out_opt(sp, ".json .xml .sarif .md .html")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("audit", parents=[common], help="full brief: totals, variance, findings, quality",
                        description="Totals, month-over-month bridge, Level-2 billing-signature findings and "
                                    "data-quality results in one report.")
    sp.add_argument("paths", nargs="+")
    sp.add_argument("--format", choices=["text", "json", "md", "sarif"], default="text")
    sp.add_argument("--min-cost", default="1", help="ignore finding groups below this observed cost (default 1)")
    sp.add_argument("--no-variance", action="store_true", help="skip the month-over-month bridge")
    sp.add_argument("--fail-on", choices=["fail", "warn", "findings", "never"], default="fail",
                    help="exit 1 on doctor failures (default), warnings, or any Level-2 finding")
    out_opt(sp, ".html .json .md .csv .sarif")
    sp.set_defaults(func=cmd_audit)

    sp = sub.add_parser("variance", parents=[common], help="explain what changed between two billing periods",
                        description="Deterministic month-over-month bridge. Give two exports (BASE CURRENT), or "
                                    "one path containing at least two billing periods.")
    sp.add_argument("base_path", metavar="BASE")
    sp.add_argument("current_path", metavar="CURRENT", nargs="?")
    sp.add_argument("--base", help="base billing period YYYY-MM")
    sp.add_argument("--current", help="current billing period YYYY-MM")
    sp.add_argument("--top", type=int, default=12)
    sp.add_argument("--format", choices=["text", "json", "md"], default="text")
    out_opt(sp, ".html .json .md .csv")
    sp.set_defaults(func=cmd_variance)

    sp = sub.add_parser("reconcile", parents=[common], help="compare an export with an invoice total",
                        description="Compare billed cost against invoice totals. Exit 0 when the arithmetic "
                                    "agrees within tolerance (check status and finality), 1 for a difference "
                                    "or no matching rows, and 2 for invalid or ambiguous input.")
    sp.add_argument("paths", nargs="+")
    sp.add_argument("--total", help="invoice total as a plain decimal")
    sp.add_argument("--currency", help="ISO 4217 currency of the invoice")
    sp.add_argument("--invoice-id")
    sp.add_argument("--period", help="billing period YYYY-MM")
    sp.add_argument("--provider", help="restrict to one normalized service provider")
    sp.add_argument("--issuer", help="restrict to one invoice issuer name")
    sp.add_argument("--billing-account", help="restrict to one billing account ID")
    sp.add_argument("--invoices", help="CSV of invoices: total,currency,invoice_id|period"
                                      "[,provider,issuer,billing_account,label]")
    sp.add_argument("--tolerance", default="0", help="accepted absolute difference (default 0)")
    sp.add_argument("--format", choices=["text", "json"], default="text")
    out_opt(sp, ".json .html .md .csv")
    sp.set_defaults(func=cmd_reconcile)

    sp = sub.add_parser("diff", parents=[common], help="what changed between two snapshots of a period",
                        description="Compare an earlier and a later delivery of the same billing period.")
    sp.add_argument("before")
    sp.add_argument("after")
    sp.add_argument("--format", choices=["text", "json"], default="text")
    sp.add_argument("--fail-on-change", action="store_true",
                    help="exit 1 when compared aggregates or invoice coverage changed")
    out_opt(sp, ".json")
    sp.set_defaults(func=cmd_diff)

    sp = sub.add_parser("redact", parents=[common], help="pseudonymize exports for sharing (fail-closed)",
                        description="Keyed, deterministic pseudonymization with a residual-identifier gate. "
                                    "Output is a directory, or a .zip when the path ends in .zip.")
    sp.add_argument("paths", nargs="+")
    sp.add_argument("-o", "--output", dest="output_path", required=True, help="output directory or .zip")
    sp.add_argument("--key-file", help="redaction key (default .cloudleak/redaction.key; created if missing)")
    sp.add_argument("--drop-unclassified", action="store_true",
                    help="drop columns missing from the registry instead of stopping")
    sp.add_argument("--force", action="store_true", help="replace an existing output")
    sp.set_defaults(func=cmd_redact)

    sp = sub.add_parser("resolve", parents=[common], help="translate aliases back using the original exports",
                        description="Resolve aliases locally. Needs the key and the original exports.")
    sp.add_argument("aliases", nargs="*")
    sp.add_argument("--source", nargs="+", required=True, help="the original (unredacted) exports")
    sp.add_argument("--key-file")
    sp.add_argument("--in", dest="input", help="resolve every alias in this text file ('-' for stdin)")
    sp.set_defaults(func=cmd_resolve)

    sp = sub.add_parser("demo", parents=[common], help="generate synthetic exports and a sample report",
                        description="Write a two-month synthetic AWS/Azure/GCP/FOCUS corpus and audit it.")
    sp.add_argument("directory", nargs="?", default="cloudleak-demo")
    sp.add_argument("--scale", type=int, default=1, help="multiply resources (for benchmarking)")
    sp.add_argument("--force", action="store_true")
    sp.add_argument("--format", default="text", help=argparse.SUPPRESS)
    sp.set_defaults(func=cmd_demo)

    sub.add_parser("checks", help="list doctor checks").set_defaults(func=cmd_checks)
    sub.add_parser("rules", help="list billing-signature rules").set_defaults(func=cmd_rules)
    sp = sub.add_parser("schema", help="print a JSON schema")
    sp.add_argument("which", choices=["finding", "report"])
    sp.set_defaults(func=cmd_schema)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return EXIT_INPUT
    try:
        if hasattr(args, "max_decompressed_gb"):
            budget = args.max_decompressed_gb * (1 << 30)
            if not math.isfinite(budget) or budget < 1:
                raise InputError("--max-decompressed-gb must give a finite budget of at least one byte")
        if getattr(args, "threads", None) is not None and args.threads < 1:
            raise InputError("--threads must be a positive integer")
        return int(args.func(args))
    except CloudLeakError as exc:
        print(f"cloudleak: error: {exc}", file=sys.stderr)
        if exc.hint:
            print(f"  hint: {exc.hint}", file=sys.stderr)
        return exc.exit_code
    except BrokenPipeError:
        return EXIT_OK
    except OSError as exc:
        where = f" {exc.filename}" if getattr(exc, "filename", None) else ""
        print(f"cloudleak: error: cannot access{where}: {exc.strerror or exc}", file=sys.stderr)
        print("  hint: check the path exists and that you can read the inputs and write the output location.",
              file=sys.stderr)
        return EXIT_INPUT
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        import duckdb
        if isinstance(exc, (duckdb.OutOfMemoryException, duckdb.IOException)):
            print(f"cloudleak: error: the query engine ran out of memory or spill space ({str(exc)[:200]})",
                  file=sys.stderr)
            print("  hint: raise --memory-limit, or point --temp-dir at a disk with more free space.",
                  file=sys.stderr)
            return EXIT_INPUT
        if os.environ.get("CLOUDLEAK_DEBUG"):
            traceback.print_exc()
        print(f"cloudleak: internal error: {type(exc).__name__}: {str(exc)[:300]}", file=sys.stderr)
        print("  This is a bug. Re-run with CLOUDLEAK_DEBUG=1 and report the traceback (no data rows).",
              file=sys.stderr)
        return EXIT_INPUT


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
