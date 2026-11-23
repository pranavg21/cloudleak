"""Self-contained offline HTML brief.

One file, no JavaScript, no external requests (enforced by a Content
Security Policy), readable in light and dark mode, and printable to PDF.
Every value from the export is HTML-escaped.
"""

from __future__ import annotations

from decimal import Decimal
from html import escape

from cloudleak.analysis import totals_finality
from cloudleak.money import display, money_context
from cloudleak.render.formats import owner_label
from cloudleak.report import CAN_CANNOT, ISSUES_URL

CSS = """
:root{--bg:#fbfbfa;--panel:#fff;--ink:#1b1d21;--muted:#5d636f;--line:#e3e4e8;--accent:#1d5fd1;
--good:#1f7a4d;--warn:#9a6200;--bad:#b42318;--info:#4a5568;--bar:#2f6fde;--bar2:#9db6e8;--pos:#c2410c;--neg:#15803d;
--chip:#f1f2f5;--code:#f4f5f7}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#121317;--panel:#1a1c21;--ink:#e8e9ec;
--muted:#a0a5b1;--line:#2c2f37;--accent:#7aa7ff;--good:#4cc38a;--warn:#f0b44c;--bad:#ff7b72;--info:#a0aec0;
--bar:#5b8ef0;--bar2:#33456b;--pos:#fb923c;--neg:#4ade80;--chip:#24272e;--code:#202329}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,
sans-serif}
main{max-width:1040px;margin:0 auto;padding:28px 16px 64px}
h1{font-size:26px;margin:0 0 4px;letter-spacing:-.01em}h2{font-size:19px;margin:40px 0 10px;
padding-top:8px;border-top:1px solid var(--line)}h3{font-size:16px;margin:22px 0 8px}
p{margin:6px 0 10px}.muted{color:var(--muted)}small,.small{font-size:13px}
a{color:var(--accent)}code,pre{font:12.5px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
pre{background:var(--code);border:1px solid var(--line);border-radius:6px;padding:8px 10px;overflow-x:auto;
white-space:pre-wrap;word-break:break-all;margin:4px 0 10px}
.banner{border:1px solid;border-radius:8px;padding:10px 14px;margin:12px 0;font-weight:500}
.banner.warn{border-color:var(--warn);color:var(--warn)}.banner.bad{border-color:var(--bad);color:var(--bad)}
.banner.info{border-color:var(--line);color:var(--muted)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px;margin:16px 0}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.kpi .v{font-size:22px;font-weight:650;font-variant-numeric:tabular-nums;margin-top:2px}
.kpi .l{font-size:12.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.table-wrap{overflow-x:auto;margin:8px 0 14px;border:1px solid var(--line);border-radius:8px;background:var(--panel)}
table{border-collapse:collapse;width:100%;font-size:13.5px}th,td{padding:7px 10px;text-align:left;
border-bottom:1px solid var(--line);vertical-align:top}th{font-weight:600;color:var(--muted);font-size:12.5px;
white-space:nowrap}tr:last-child td{border-bottom:0}td.n,th.n{text-align:right;font-variant-numeric:tabular-nums;
white-space:nowrap}
.chip{display:inline-block;border-radius:999px;padding:1px 8px;font-size:12px;font-weight:600;
background:var(--chip);white-space:nowrap}
.s-PASS{color:var(--good)}.s-WARN{color:var(--warn)}.s-FAIL{color:var(--bad)}.s-INFO,.s-SKIP{color:var(--info)}
.fin-finalized{color:var(--good)}.fin-preliminary_estimate{color:var(--warn)}.fin-unknown{color:var(--info)}
details{background:var(--panel);border:1px solid var(--line);border-radius:8px;margin:8px 0}
summary{cursor:pointer;padding:10px 12px;list-style-position:outside;overflow-wrap:anywhere}
p,li,.banner{overflow-wrap:anywhere}td{overflow-wrap:break-word}
.chart{overflow-x:auto;margin:8px 0}.chart svg{min-width:620px}.chart .note{font-size:12px;color:var(--muted)}
details>div{padding:0 14px 12px}
summary .amt{float:right;font-variant-numeric:tabular-nums;font-weight:600;margin-left:12px}
ul{margin:4px 0 10px;padding-left:22px}li{margin:2px 0}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}@media (max-width:760px){.grid2{grid-template-columns:1fr}
summary .amt{float:none;display:block;margin:2px 0 0}}
svg text{fill:var(--ink);font:12px system-ui,sans-serif}svg .muted{fill:var(--muted)}
.bar{fill:var(--bar)}.bar2{fill:var(--bar2)}.pos{fill:var(--pos)}.neg{fill:var(--neg)}.axis{stroke:var(--line)}
footer{margin-top:48px;color:var(--muted);font-size:13px;border-top:1px solid var(--line);padding-top:14px}
@media print{body{background:#fff}details{break-inside:avoid}details:not([open])>div{display:block}
.banner{break-inside:avoid}a{color:inherit}}
"""


DRIVER_LABELS = {"volume_and_rate": "volume / rate", "new": "new", "discontinued": "discontinued",
                 "unquantified": "unquantified", "credits": "credits", "tax": "tax", "purchases": "purchases",
                 "adjustments": "adjustments", "volume": "volume", "rate": "rate"}


def e(v: object) -> str:
    return escape("" if v is None else str(v), quote=True)


def D(v: object) -> Decimal:
    if v is None:
        return Decimal(0)
    return v if isinstance(v, Decimal) else Decimal(str(v))


def money(v: object, cur: str | None = None) -> str:
    return e(display(D(v), cur))


def chip(text: str, cls: str = "") -> str:
    return f'<span class="chip {e(cls)}">{e(text)}</span>'


def table(headers: list[str], rows: list[list[str]], numeric: set[int] | None = None) -> str:
    numeric = numeric or set()
    th = "".join(f'<th class="{"n" if i in numeric else ""}">{e(h)}</th>' for i, h in enumerate(headers))
    body = "".join("<tr>" + "".join(f'<td class="{"n" if i in numeric else ""}">{c}</td>'
                                    for i, c in enumerate(r)) + "</tr>" for r in rows)
    return f'<div class="table-wrap"><table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>'


def hbars(items: list[tuple[str, Decimal]], cur: str | None, width: int = 480) -> str:
    """Horizontal bar chart of positive amounts (negatives are shown as zero-length with a label)."""
    if not items:
        return ""
    top = max((v for _, v in items), default=Decimal(0))
    rowh, label_w, pad = 24, 190, 8
    h = rowh * len(items) + pad
    parts = [f'<svg viewBox="0 0 {width} {h}" width="100%" role="img" aria-label="Spend by category">']
    for i, (label, v) in enumerate(items):
        y = i * rowh + 4
        w = 0 if top <= 0 or v <= 0 else float(v / top) * (width - label_w - 110)
        short = label if len(label) <= 28 else label[:27] + "…"
        parts.append(f'<text x="0" y="{y + 13}"><title>{e(label)}</title>{e(short)}</text>')
        parts.append(f'<rect class="bar" x="{label_w}" y="{y + 2}" width="{w:.1f}" height="14" rx="2"/>')
        parts.append(f'<text class="muted" x="{label_w + w + 6:.1f}" y="{y + 13}">{money(v)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def waterfall(bridge: dict, width: int = 900) -> str:
    steps = [("start", bridge["base_period"], D(bridge["base_total"]))]
    for b in bridge["bridge"]:
        if display(D(b["amount"])) not in ("0.00", "-0.00"):
            steps.append(("delta", b["label"], D(b["amount"])))
    steps.append(("end", bridge["current_period"], D(bridge["current_total"])))
    running, lo, hi, bars = Decimal(0), Decimal(0), Decimal(0), []
    for kind, label, amt in steps:
        if kind == "delta":
            a, b = running, running + amt
            running = b
        else:
            a, b = Decimal(0), amt
            running = amt
        bars.append((kind, label, amt, min(a, b), max(a, b)))
        lo, hi = min(lo, a, b), max(hi, a, b)
    levels = [x for _, _, _, a, b in bars for x in (a, b) if x != 0]
    moves = sum((abs(amt) for kind, _, amt, _, _ in bars if kind == "delta"), Decimal(0))
    broken = bool(levels) and min(levels) > 0 and moves < min(levels) * Decimal("0.5")
    if broken:  # small drivers on large totals: zoom in and say so
        lo = min(levels) - (max(levels) - min(levels)) * Decimal("0.6") - moves * Decimal("0.2")
    span = (hi - lo) or Decimal(1)
    n = len(bars)
    top, bottom, left = 18, 200, 8
    slot = (width - left) / n
    bw = min(70.0, slot * 0.62)

    def y(v: Decimal) -> float:
        return top + float((hi - v) / span) * (bottom - top)

    out = [f'<svg viewBox="0 0 {width} 250" width="100%" role="img" aria-label="Month-over-month bridge">',
           f'<line class="axis" x1="0" x2="{width}" y1="{y(Decimal(0)):.1f}" y2="{y(Decimal(0)):.1f}"/>']
    for i, (kind, label, amt, a, b) in enumerate(bars):
        a, b = max(a, lo), max(b, lo)
        x = left + i * slot + (slot - bw) / 2
        cls = "bar" if kind != "delta" else ("pos" if amt > 0 else "neg")
        hgt = max(1.0, y(a) - y(b))
        out.append(f'<rect class="{cls}" x="{x:.1f}" y="{y(b):.1f}" width="{bw:.1f}" height="{hgt:.1f}" rx="2">'
                   f'<title>{e(label)}: {money(amt)}</title></rect>')
        val = ("+" if kind == "delta" and amt > 0 else "") + display(amt, None, 0)
        out.append(f'<text x="{x + bw / 2:.1f}" y="{y(b) - 4:.1f}" text-anchor="middle">{e(val)}</text>')
        words = label.split()
        line1 = " ".join(words[:2])
        line2 = " ".join(words[2:4])
        out.append(f'<text class="muted" x="{x + bw / 2:.1f}" y="222" text-anchor="middle">{e(line1)}</text>')
        if line2:
            out.append(f'<text class="muted" x="{x + bw / 2:.1f}" y="237" text-anchor="middle">{e(line2)}</text>')
    out.append("</svg>")
    note = '<div class="note">Vertical axis does not start at zero.</div>' if broken else ""
    return f'<div class="chart">{"".join(out)}{note}</div>'


@money_context
def render(report: dict) -> str:
    r = report
    title = "CloudLeak audit brief"
    sections: list[str] = []
    periods = r.get("periods") or []
    prelim = [p for p in periods if p["finality"] == "preliminary_estimate"]
    doctor = r.get("doctor")

    head = [f"<h1>{e(title)}</h1>",
            f'<p class="muted small">Run {e(r["run"]["report_run_id"])} · generated {e(r["run"]["generated_at"])} · '
            f'CloudLeak {e(r["tool"]["version"])} · DuckDB {e(r["tool"]["duckdb_version"])} · processed offline, '
            "no cloud credentials</p>"]
    if r.get("synthetic_data"):
        head.append('<div class="banner info">SYNTHETIC TEST DATA: these inputs are generated fixtures, not a '
                    "real bill.</div>")
    if prelim:
        names = ", ".join(sorted({f"{p['provider']} {p['billing_period']}" for p in prelim}))
        head.append(f'<div class="banner warn">PRELIMINARY ESTIMATE: {e(names)} not finalized. Amounts can '
                    "still change as late charges, credits and refunds post.</div>")
    if doctor and doctor["counts"]["FAIL"]:
        fails = [c for c in doctor["results"] if c["status"] == "FAIL"]
        items = "".join(f"<li>{e(c['check_id'])} {e(c['title'])}: {e(c['message'])}</li>" for c in fails[:6])
        head.append(f'<div class="banner bad">TOTALS MAY BE WRONG: {len(fails)} data-quality check(s) failed. '
                    f"Fix the inputs before relying on any number in this report.<ul>{items}</ul></div>")
    sections.append("".join(head))

    # KPIs
    kpis = []
    totals = r.get("totals") or []
    principal = {(p["currency"], p["cost_basis"], p["billing_period"]) for p in periods if p["days_covered"] > 0}
    agg: dict[tuple, Decimal] = {}
    for t in totals:
        if (t["currency"], t["cost_basis"], t["billing_period"]) not in principal:
            continue
        k = (t["currency"], t["cost_basis"], t["billing_period"])
        agg[k] = agg.get(k, Decimal(0)) + D(t["total"])
    shown = set()
    for (cur, basis, period), amount in sorted(agg.items(), key=lambda kv: (str(kv[0][0]), kv[0][1], kv[0][2]),
                                               reverse=True):
        if (cur, basis) in shown:
            continue
        shown.add((cur, basis))
        kpis.append(f'<div class="kpi"><div class="l">{e(basis)} spend · {e(period)}</div>'
                    f'<div class="v">{money(amount, cur)}</div></div>')
    l2 = [f for f in r.get("findings", []) if f["evidence_level"] == "L2"]
    kpis.append(f'<div class="kpi"><div class="l">Billing-signature findings</div><div class="v">{len(l2)}</div>'
                '</div>')
    if doctor:
        c = doctor["counts"]
        kpis.append(f'<div class="kpi"><div class="l">Data quality</div><div class="v s-{e(doctor["status"])}">'
                    f'{e(doctor["status"])}</div><div class="small muted">{c["FAIL"]} fail · {c["WARN"]} warn · '
                    f'{c["PASS"]} pass</div></div>')
    sections.append(f'<div class="kpis">{"".join(kpis)}</div>')

    sections.append("<h2 id=\"proof\">What this report can and cannot prove</h2>" + table(
        ["Question", "Status"], [[e(q), e(a)] for q, a in CAN_CANNOT]))

    # Variance
    for br in r.get("variance") or []:
        cur = br["currency"]
        rows = [[e(d["label"]), money(d["amount"], cur)] for d in br["bridge"]
                if display(D(d["amount"])) not in ("0.00", "-0.00")]
        changes = [[e(c["service"]), e(c["sku"]), e(c.get("sub_account_name") or c.get("sub_account_id") or ""),
                    money(c["base_cost"]), money(c["current_cost"]), money(c["delta"]),
                    e(DRIVER_LABELS.get(c["driver"], c["driver"])),
                    e(owner_label(c.get("owner_routing")))]
                   for c in br["top_line_changes"][:15]]
        cats = [[e(c["service_category"]), money(c["base_cost"]), money(c["current_cost"]), money(c["delta"])]
                for c in br["by_service_category"]]
        notes = "".join(f'<p class="small muted">{e(n)}</p>' for n in br.get("notes", []))
        sections.append(
            f'<h2 id="variance-{e(cur)}-{e(br["cost_basis"])}">Month over month: {e(br["base_period"])} → '
            f'{e(br["current_period"])} ({e(cur)}, {e(br["cost_basis"])})</h2>'
            f"<p>{e(br['narrative'])}</p>{notes}{waterfall(br)}"
            '<div class="grid2"><div><h3>Bridge</h3>' + table(["Driver", "Amount"], rows, {1}) + "</div>"
            "<div><h3>By service category</h3>" + table(["Category", br["base_period"], br["current_period"], "Δ"],
                                                         cats, {1, 2, 3}) + "</div></div>"
            "<h3>Largest SKU group changes</h3>" + table(
                ["Service", "SKU", "Account", "Before", "After", "Δ", "Driver", "Owner"], changes, {3, 4, 5}))

    # Spend
    spend = ['<h2 id="spend">Spend</h2>']
    trows = []
    finality = totals_finality(periods)
    for t in totals:
        cur = t["currency"]
        comp = []
        for k in ("purchase", "fee", "tax", "credit", "refund", "discount", "adjustment", "embedded_credits"):
            if t.get(k) not in (None, "0.00") and D(t.get(k)) != 0:
                comp.append(f"{k.replace('_', ' ')} {display(D(t[k]))}")
        fin = finality.get((t["provider"], t["billing_period"], cur, t["cost_basis"]), "unknown")
        trows.append([e(t["provider"]), e(t["billing_period"]), e(t["cost_basis"]),
                      f'<span class="fin-{e(fin)}">{e(fin.replace("_", " "))}</span>', money(t["total"], cur),
                      money(t.get("usage"), cur), e("; ".join(comp))])
    spend.append(table(["Provider", "Period", "Basis", "Finality", "Total", "Usage", "Other components"],
                       trows, {4, 5}))
    bd = r.get("breakdowns") or {}
    charts = []
    for dim, label in (("service_category", "By service category"), ("service", "Top services"),
                       ("sub_account_id", "Top accounts / subscriptions / projects")):
        rows = bd.get(dim) or []
        groups: dict[tuple, list] = {}
        for row in rows:
            groups.setdefault((row["currency"], row["cost_basis"]), []).append(row)
        for (cur, basis), items in groups.items():
            charts.append(f"<div><h3>{e(label)} <span class=\"muted small\">{e(cur)} · {e(basis)}, all periods"
                          f"</span></h3>{hbars([(i['key'], D(i['cost'])) for i in items], cur)}</div>")
    if charts:
        spend.append('<div class="grid2">' + "".join(charts) + "</div>")
    sections.append("".join(spend))

    # Findings
    fs = r.get("findings") or []
    sig = [f for f in fs if f["classification"] == "billing_signature_candidate"]
    other = [f for f in fs if f["classification"] in ("reconciliation_delta",)]
    fsec = ['<h2 id="findings">Billing-signature findings (Level 2)</h2>',
            '<p class="muted small">Each amount uses the cost basis shown on its finding. '
            "Verify the billing signal with the read-only steps before making a change."
            "</p>"]
    if not sig:
        fsec.append("<p>No billing signatures matched.</p>")
    by_rule: dict[str, list] = {}
    for f in sig:
        by_rule.setdefault(f["rule_id"], []).append(f)
    for rid, items in by_rule.items():
        subtotals: dict[tuple, Decimal] = {}
        for f in items:
            key = (f["currency"], f["financials"]["cost_basis"])
            subtotals[key] = subtotals.get(key, Decimal(0)) + D(f["financials"]["observed_cost"])
        tot = " · ".join(f"{display(v, c)} {basis}" for (c, basis), v in
                         sorted(subtotals.items(), key=lambda kv: tuple(map(str, kv[0]))))
        fsec.append(f"<h3>{e(items[0]['title'])} <span class=\"muted small\">{e(rid)} · {len(items)} item(s) · "
                    f"{e(tot)} observed</span></h3>")
        fsec.append(f'<p class="small muted">{e(items[0]["details"].get("rationale", ""))}</p>')
        for f in items[:40]:
            fsec.append(_finding_html(f))
        if len(items) > 40:
            fsec.append(f'<p class="small muted">{len(items) - 40} more in the JSON output.</p>')
    for f in other:
        fsec.append(_finding_html(f))
    sections.append("".join(fsec))

    # Data quality
    if doctor:
        rows = []
        for c in doctor["results"]:
            if c["status"] in ("WARN", "FAIL"):
                rows.append([f'<span class="chip s-{e(c["status"])}">{e(c["status"])}</span>', e(c["check_id"]),
                             e(c["scope"]), e(c["message"]) + (f'<br><span class="small muted">Fix: '
                                                               f'{e(c["remediation"])}</span>' if c["remediation"]
                                                               else "")])
        quiet = [c for c in doctor["results"] if c["status"] not in ("WARN", "FAIL")]
        qrows = [[f'<span class="s-{e(c["status"])}">{e(c["status"])}</span>', e(c["check_id"]), e(c["scope"]),
                  e(c["message"])] for c in quiet]
        sections.append(
            '<h2 id="quality">Data quality</h2>'
            + (table(["Status", "Check", "Scope", "Finding"], rows) if rows else "<p>No warnings or failures.</p>")
            + f"<details><summary>{len(quiet)} passing and informational checks</summary><div>"
            + table(["Status", "Check", "Scope", "Detail"], qrows) + "</div></details>")

    # Provenance
    inputs = [[e(i["name"]), e(i["format"]), e(i.get("dataset") or "unrecognized"), f'{i.get("rows", 0):,}',
               f'<code>{e(i["sha256"][:16])}…</code>'] for i in r.get("inputs", [])]
    t = r["tool"]
    sections.append(
        '<h2 id="provenance">Provenance and method</h2>'
        + table(["File", "Format", "Detected as", "Rows", "SHA-256"], inputs, {3})
        + f'<p class="small">CloudLeak {e(t["version"])}, DuckDB {e(t["duckdb_version"])}, Python '
          f'{e(t["python_version"])}, rule pack {e(t["rule_pack_version"])} ({e(t["rule_pack_hash"])}). '
          "Amounts are exact decimals (no floating point); totals are never summed across currencies or across "
          "actual and amortized bases. Identical rows are kept, not de-duplicated.</p>"
        + "<ul>" + "".join(f"<li class=\"small\">{e(x)}</li>" for x in r.get("limitations", [])) + "</ul>")

    footer = (f'<footer>Generated locally by CloudLeak. This file contains identifiers and costs from the source '
              f'export; share it within the same access boundary as the export, or run '
              f'<code>cloudleak redact</code> first. · <a href="{e(ISSUES_URL)}">Report a provider schema quirk '
              "(use synthetic headers)</a></footer>")
    csp = ("default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'")
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f'<meta http-equiv="Content-Security-Policy" content="{csp}">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="referrer" content="no-referrer">'
        f"<title>CloudLeak audit brief</title><style>{CSS}</style></head><body><main>"
        + "".join(sections) + footer + "</main></body></html>\n"
    )


def _finding_html(f: dict) -> str:
    fin = f["period_finality"]
    cur = f["currency"]
    owner = f.get("owner_routing") or {}
    owner_txt = owner_label(owner)
    where = f.get("resource_id") or f.get("sub_account_id") or ""
    steps = []
    for s in f.get("required_verification_steps", []):
        steps.append(f"<li>{e(s['description'])}" + (f"<pre>{e(s['command'])}</pre>" if s.get("command") else "")
                     + "</li>")
    ev = f["exact_evidence"]
    sample = ", ".join(f"source {x['source_id']} row {x['row']}" for x in ev.get("sample_rows", [])[:5])
    billed = f["financials"].get("observed_billed_cost")
    billed_detail = f"<p>Billed cost of matched rows: {money(billed, cur)}</p>" if billed is not None else ""
    return (
        f"<details><summary><span class=\"amt\">{money(f['financials']['observed_cost'], cur)} "
        f"{e(f['financials']['cost_basis'])}</span>"
        f"<strong>{e(f['provider'])}</strong> · {e(where)} · {e(f['billing_period'])} "
        f'<span class="chip fin-{e(fin)}">{e(fin.replace("_", " "))}</span> '
        f'<span class="chip">{e(f["evidence_level"])}</span> '
        f'<span class="small muted">owner: {e(owner_txt)}</span></summary><div>'
        f"<p>{e(f['summary'])}</p>{billed_detail}"
        f'<p class="small"><strong>Evidence:</strong> {ev["matched_rows"]:,} line item(s) matching '
        f'{e(", ".join(ev.get("matched_values", [])[:5]))}; {e(sample)}. Finding <code>{e(f["finding_id"])}</code>.</p>'
        f'<div class="grid2"><div><strong class="small">Unknown from billing data</strong><ul>'
        + "".join(f"<li class=\"small\">{e(x)}</li>" for x in f.get("unknown_information", []))
        + '</ul></div><div><strong class="small">Could be legitimate when</strong><ul>'
        + "".join(f"<li class=\"small\">{e(x)}</li>" for x in f.get("potential_false_positives", []))
        + "</ul></div></div>"
        + (f'<strong class="small">Read-only verification</strong><ol>{"".join(steps)}</ol>' if steps else "")
        + "</div></details>"
    )
