"""``cloudleak doctor``: deterministic data-quality, finality and conformance-gap checks.

Every check has a stable ID, so CI pipelines can gate on specific checks and
the catalog can be documented. Status meanings:

    PASS  the property holds
    INFO  a fact worth knowing; not a defect
    WARN  results remain usable but a conclusion could be wrong without care
    FAIL  the data cannot support a trustworthy total until fixed
    SKIP  the check does not apply to these inputs
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from cloudleak.analysis import period_statuses
from cloudleak.engine import LoadedSource, Workspace
from cloudleak.money import display, fmt, pct
from cloudleak.providers import focus as focus_adapter
from cloudleak.providers.base import Columns, quote_ident

STATUSES = ("PASS", "INFO", "WARN", "FAIL", "SKIP")


@dataclass(frozen=True)
class CheckSpec:
    check_id: str
    title: str
    category: str
    description: str


CATALOG: list[CheckSpec] = [
    CheckSpec("CL-ING-001", "Billing format recognized", "ingestion",
              "Each file is identified as AWS CUR (legacy or 2.0), Azure cost export, GCP billing export or FOCUS."),
    CheckSpec("CL-ING-002", "No file supplied twice", "ingestion",
              "Byte-identical files (same SHA-256) would double count every row."),
    CheckSpec("CL-ING-003", "Manifest completeness", "ingestion",
              "Every data file a provider manifest lists is present, and no unlisted file sits beside it."),
    CheckSpec("CL-ING-004", "Manifest row counts", "ingestion",
              "Row counts declared by an Azure export manifest match the rows read."),
    CheckSpec("CL-ING-005", "Cost values parse exactly", "ingestion",
              "Every non-blank cost value is a plain decimal. Unparseable values are counted, never zeroed."),
    CheckSpec("CL-ING-006", "Dates and billing periods parse", "ingestion",
              "Every row has a usage date and a billing period CloudLeak can place in UTC."),
    CheckSpec("CL-ING-007", "Decimal precision preserved", "ingestion",
              "Cost values with more than 12 decimal places are rounded at the 12th place."),
    CheckSpec("CL-ING-008", "Unambiguous column names", "ingestion",
              "No two columns normalize to the same name."),
    CheckSpec("CL-ACC-001", "Single currency per total", "accounting",
              "Totals are never summed across currencies; rows without a currency are flagged."),
    CheckSpec("CL-ACC-002", "Billing period finality", "accounting",
              "Finality evidence for each billing period, including cases where invoice issuance is unknown."),
    CheckSpec("CL-ACC-003", "Full-period coverage", "accounting",
              "Whether each billing period contains usage for every day of the month."),
    CheckSpec("CL-ACC-004", "Cost basis not mixed", "accounting",
              "Azure actual and amortized exports for the same scope are reported separately, never summed."),
    CheckSpec("CL-ACC-005", "Charge composition", "accounting",
              "Tax, credits, refunds, discounts, fees and purchases are separated from usage."),
    CheckSpec("CL-ACC-006", "No overlapping deliveries", "accounting",
              "Identical line items in two different files indicate overlapping exports and double counting."),
    CheckSpec("CL-ACC-007", "Billed versus effective cost", "accounting",
              "Difference between billed and amortized (effective) cost, where both are present."),
    CheckSpec("CL-ACC-008", "Every export covers every period", "accounting",
              "A provider or billing account with no rows in a period that other exports cover. Comparisons "
              "across that period would mistake a missing file for a change in spend."),
    CheckSpec("CL-DQ-001", "Resource identifiers present", "data_quality",
              "Share of usage cost carrying a resource ID; exports without resource IDs cannot support "
              "resource-level findings."),
    CheckSpec("CL-DQ-002", "Tag and label coverage", "data_quality",
              "Share of usage cost carrying any tag or label."),
    CheckSpec("CL-DQ-003", "Consistent tag keys", "data_quality",
              "Tag keys that differ only by case or prefix (env, ENV, Environment) split ownership reports."),
    CheckSpec("CL-DQ-004", "Identical rows within a file", "data_quality",
              "Rows identical in every column. Kept, because Azure documents them as legitimate in "
              "amortized exports; de-duplicating would understate spend."),
    CheckSpec("CL-DQ-005", "Split cost allocation rows", "data_quality",
              "AWS split cost allocation columns are present; split costs must not be added to parent costs."),
    CheckSpec("CL-DQ-006", "Late-arriving usage", "data_quality",
              "GCP usage billed to a later invoice month than the month it was used in."),
    CheckSpec("CL-DQ-007", "Consistent GCP commitment schema", "data_quality",
              "GCP exports with and without consumption_model columns for one billing account."),
    CheckSpec("CL-DQ-008", "Negative usage rows", "data_quality",
              "Negative-cost usage rows (corrections) reduce totals; they are kept and counted."),
    CheckSpec("CL-DQ-009", "Recognized charge types", "data_quality",
              "Charge or line-item types CloudLeak does not recognize are listed so they can be mapped."),
    CheckSpec("CL-FOC-001", "FOCUS version", "focus",
              "The FOCUS version inferred from the columns present (approximate, not certification)."),
    CheckSpec("CL-FOC-002", "FOCUS columns CloudLeak relies on", "focus",
              "Key FOCUS columns that are absent, and what analysis each enables."),
    CheckSpec("CL-FOC-003", "FOCUS columns present but empty", "focus",
              "Columns a provider includes but never populates. Cross-cloud SQL silently returns nothing for them."),
    CheckSpec("CL-FOC-004", "FOCUS ChargeCategory values", "focus",
              "ChargeCategory values outside Usage, Purchase, Tax, Credit and Adjustment."),
    CheckSpec("CL-FOC-005", "FOCUS corrections", "focus",
              "Rows with ChargeClass = Correction restate earlier periods."),
]
SPECS = {c.check_id: c for c in CATALOG}


@dataclass
class CheckResult:
    check_id: str
    status: str
    message: str
    scope: str = "all inputs"
    remediation: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def title(self) -> str:
        return SPECS[self.check_id].title

    @property
    def category(self) -> str:
        return SPECS[self.check_id].category

    def to_dict(self) -> dict:
        from cloudleak.findings import _jsonable
        return {
            "check_id": self.check_id, "title": self.title, "category": self.category, "status": self.status,
            "scope": self.scope, "message": self.message, "remediation": self.remediation,
            "metrics": _jsonable(self.metrics),
        }


@dataclass
class DoctorReport:
    results: list[CheckResult]

    def counts(self) -> dict[str, int]:
        c = dict.fromkeys(STATUSES, 0)
        for r in self.results:
            c[r.status] += 1
        return c

    @property
    def worst(self) -> str:
        c = self.counts()
        return "FAIL" if c["FAIL"] else "WARN" if c["WARN"] else "PASS"


def _src_name(s: LoadedSource) -> str:
    return s.file.display_name


def delivery_checks(ws: Workspace) -> list[CheckResult]:
    """Check delivery integrity without running unrelated analytical checks."""
    results: list[CheckResult] = []
    add = results.append
    by_sha: dict[tuple[str, str | None], list[str]] = defaultdict(list)
    for s in ws.sources:
        by_sha[(s.file.sha256, s.mapping.cost_basis if s.mapping else None)].append(_src_name(s))
    dupes = {k: v for k, v in by_sha.items() if len(v) > 1}
    if dupes:
        for (sha, _basis), names in dupes.items():
            add(CheckResult("CL-ING-002", "FAIL", f"Identical content supplied {len(names)} times: {', '.join(names)}.",
                            remediation="Remove the duplicate copies; every row would otherwise be counted twice.",
                            metrics={"sha256": sha}))
    else:
        add(CheckResult("CL-ING-002", "PASS", f"{len(ws.sources)} distinct files."))

    _manifest_checks(ws, add)

    return results


def run_doctor(ws: Workspace) -> DoctorReport:
    out: list[CheckResult] = []
    add = out.append

    # ---- ingestion ---------------------------------------------------------
    for s in ws.sources:
        if s.mapping is None:
            add(CheckResult("CL-ING-001", "FAIL", f"Unrecognized billing format ({len(s.columns)} columns).",
                            _src_name(s),
                            "Supply a native AWS CUR, Azure cost export, GCP billing export or FOCUS file. If this "
                            "is one, open an issue with a synthetic header example.",
                            {"columns_sample": s.columns[:12]}))
        else:
            m = s.mapping
            label = m.dataset + (f" (FOCUS {m.focus_version})" if m.focus_version else "")
            status = "WARN" if m.cost_basis == "unknown" or s.rows == 0 else "PASS"
            if s.rows == 0:
                m.notes.append("The file has a header but no data rows.")
            add(CheckResult("CL-ING-001", status,
                            f"{label}; {s.rows:,} rows; cost basis: {m.cost_basis}"
                            + (f" (from {s.basis_evidence})" if s.basis_evidence else "") + ".",
                            _src_name(s), " ".join(m.notes) or None,
                            {"dataset": m.dataset, "rows": s.rows, "cost_basis": m.cost_basis}))
    if ws.sources and sum(s.rows for s in ws.sources) == 0:
        add(CheckResult("CL-ING-001", "FAIL", "No data rows in any input file; there is nothing to audit.",
                        remediation="Check that the export finished and that you passed the data files."))
    for name, reason in ws.discovery.skipped:
        add(CheckResult("CL-ING-001", "INFO", f"Skipped: {reason}.", name))

    out.extend(delivery_checks(ws))

    bad = ws.rows("""SELECT source_id, count(*) AS n, min(source_row) AS first_row, any_value(cost_raw) AS example
                     FROM items WHERE cost_parse_error GROUP BY 1 ORDER BY 1""")
    names = {s.file.source_id: _src_name(s) for s in ws.sources}
    if bad:
        for b in bad:
            add(CheckResult("CL-ING-005", "FAIL",
                            f"{b['n']:,} cost value(s) are not plain decimals (first at data row {b['first_row']}, "
                            f"e.g. {b['example'][:40]!r}). These rows are excluded from every total.",
                            names[b["source_id"]],
                            "Re-export without spreadsheet formatting (no thousands separators, currency symbols "
                            "or locale decimal commas).", {"rows": b["n"], "first_row": b["first_row"]}))
    else:
        add(CheckResult("CL-ING-005", "PASS", "Every cost value parsed as an exact decimal."))

    undated = ws.rows("""SELECT source_id, count(*) FILTER (WHERE charge_start IS NULL) AS no_date,
                                count(*) FILTER (WHERE billing_period_start IS NULL) AS no_period,
                                min(source_row) FILTER (WHERE billing_period_start IS NULL) AS first_row
                         FROM items GROUP BY 1 HAVING no_date > 0 OR no_period > 0 ORDER BY 1""")
    if undated:
        for u in undated:
            status = "FAIL" if u["no_period"] else "WARN"
            add(CheckResult("CL-ING-006", status,
                            f"{u['no_period']:,} rows have no billing period and {u['no_date']:,} have no usage "
                            f"date" + (f" (first at data row {u['first_row']})" if u["first_row"] else "") + ".",
                            names[u["source_id"]],
                            "Check the date format; CloudLeak reads ISO 8601, 'YYYY-MM-DD HH:MM:SS UTC', "
                            "MM/DD/YYYY and YYYYMM invoice months.",
                            {"rows_without_period": u["no_period"], "rows_without_date": u["no_date"]}))
    else:
        add(CheckResult("CL-ING-006", "PASS", "Every row has a usage date and billing period."))

    precise = ws.scalar(r"""SELECT count(*) FROM items
                            WHERE regexp_matches(cost_raw, '^[+-]?[0-9]*\.[0-9]{13,}$')""")
    add(CheckResult("CL-ING-007", "INFO" if precise else "PASS",
                    f"{precise:,} cost values carry more than 12 decimal places and were rounded at the 12th."
                    if precise else "No cost value exceeds 12 decimal places."))

    issues = [(s, i) for s in ws.sources for i in s.header_issues]
    if issues:
        for s, i in issues:
            add(CheckResult("CL-ING-008", "WARN", i, _src_name(s), "Rename or drop one of the columns."))
    else:
        add(CheckResult("CL-ING-008", "PASS", "Column names are unambiguous."))

    # ---- accounting --------------------------------------------------------
    cur = ws.rows("""SELECT provider,
                            list(DISTINCT currency ORDER BY currency) FILTER (WHERE currency IS NOT NULL) AS cs,
                            count(*) FILTER (WHERE currency IS NULL) AS missing
                     FROM valid GROUP BY 1 ORDER BY 1""")
    all_cur = sorted({c for r in cur for c in (r["cs"] or [])})
    missing = sum(r["missing"] for r in cur)
    if missing:
        add(CheckResult("CL-ACC-001", "WARN", f"{missing:,} rows carry no currency; they are totalled as "
                        "currency 'None' and never merged with priced rows.",
                        remediation="Include the billing currency column in the export."))
    if len(all_cur) > 1:
        add(CheckResult("CL-ACC-001", "WARN", f"{len(all_cur)} billing currencies ({', '.join(all_cur)}). "
                        "Totals are kept separate per currency; no exchange rate is applied.",
                        metrics={"currencies": all_cur}))
    elif not missing:
        add(CheckResult("CL-ACC-001", "PASS", f"Single billing currency: {all_cur[0] if all_cur else 'none'}."))

    statuses = period_statuses(ws)
    for st in statuses:
        scope = " ".join(x for x in (st.provider, st.billing_account_id, st.billing_period, st.currency,
                                     f"({st.cost_basis})") if x)
        if st.finality == "finalized":
            add(CheckResult("CL-ACC-002", "PASS", f"Finalized: {st.reason}.", scope))
        elif st.finality == "preliminary_estimate":
            add(CheckResult("CL-ACC-002", "WARN", f"PRELIMINARY ESTIMATE: {st.reason}. Totals can still change; "
                            "invoice reconciliation is not conclusive.", scope,
                            "Re-run after the provider finalizes the period (AWS typically in the first days of the "
                            "following month, with late fees such as support posting after that)."))
        else:
            add(CheckResult("CL-ACC-002", "INFO", f"Finality unknown: {st.reason}.", scope))
        if st.days_covered == 0:
            add(CheckResult("CL-ACC-003", "INFO",
                            f"None of the {st.rows:,} rows has a charge date within {st.billing_period}. "
                            "Daily coverage cannot be established.", scope))
        elif st.partial:
            add(CheckResult("CL-ACC-003", "WARN",
                            f"Covers {st.days_covered} of {st.days_in_period} days ({st.first_day} to "
                            f"{st.last_day}). Compare the observed coverage before interpreting changes.", scope,
                            metrics={"days_covered": st.days_covered, "days_in_period": st.days_in_period}))
    if statuses and not any(s.partial for s in statuses):
        add(CheckResult("CL-ACC-003", "PASS", "Every billing period has usage on every day."))

    mixed = ws.rows("""SELECT provider, sub_account_id, billing_period,
                              list(DISTINCT cost_basis ORDER BY cost_basis) AS bases
                       FROM valid GROUP BY ALL HAVING count(DISTINCT cost_basis) > 1 ORDER BY ALL""")
    unknown_basis = ws.scalar("SELECT count(*) FROM valid WHERE cost_basis = 'unknown'")
    for r in mixed:
        add(CheckResult("CL-ACC-004", "WARN",
                        f"Both {' and '.join(r['bases'])} costs are present. They are reported side by side; "
                        "adding them would double count.",
                        f"{r['provider']} {r['sub_account_id'] or ''} {r['billing_period']}",
                        "Audit one basis at a time, or compare them deliberately."))
    if unknown_basis:
        add(CheckResult("CL-ACC-004", "WARN", f"{unknown_basis:,} rows have an unknown cost basis "
                        "(actual vs amortized).", remediation="Name the Azure export folder 'actual' or 'amortized'."))
    if not mixed and not unknown_basis:
        add(CheckResult("CL-ACC-004", "PASS", "One cost basis per scope and period."))

    comp = ws.rows("""SELECT provider, billing_period, currency, cost_basis, charge_category,
                             sum(cost) AS amount, count(*) AS n
                      FROM valid WHERE charge_category NOT IN ('usage')
                      GROUP BY ALL ORDER BY ALL""")
    if comp:
        parts = defaultdict(list)
        for r in comp:
            parts[(r["provider"], r["billing_period"], r["currency"], r["cost_basis"])].append(
                f"{r['charge_category']} {display(r['amount'])}")
        for (p, period, c, b), items in parts.items():
            add(CheckResult("CL-ACC-005", "INFO", "Non-usage charges: " + "; ".join(items) + ".",
                            f"{p} {period} {c} ({b})"))
    else:
        add(CheckResult("CL-ACC-005", "INFO", "Only usage charges present (no tax, credit, refund or fee rows)."))

    # Identical line items in different files: overlapping deliveries. When at least half of a file
    # reappears elsewhere, it is a duplicated delivery and totals are wrong -> FAIL.
    overlap = ws.rows("""
        WITH k AS (
          SELECT source_id, provider,
                 hash(provider, billing_account_id, sub_account_id, resource_id, sku, sku_detail, charge_raw,
                      charge_start, charge_end, cost, usage_quantity, invoice_id, cost_basis, region) AS h
          FROM valid),
        c AS (SELECT h, count(DISTINCT source_id) AS ns FROM k GROUP BY h)
        SELECT k.source_id, any_value(provider) AS provider, count(*) AS n,
               count(*) FILTER (WHERE ns > 1) AS dup, count(DISTINCT h) FILTER (WHERE ns > 1) AS keys
        FROM k JOIN c USING (h) GROUP BY 1 HAVING count(*) FILTER (WHERE ns > 1) > 0 ORDER BY 1""")
    if overlap:
        for r in overlap:
            share = r["dup"] / r["n"]
            status = "FAIL" if share >= 0.5 else "WARN"
            add(CheckResult("CL-ACC-006", status,
                            f"{r['dup']:,} of {r['n']:,} rows ({share:.0%}) also appear identically in another file."
                            + (" This file duplicates another delivery; totals count it twice."
                               if status == "FAIL" else " Overlapping deliveries double count."),
                            names[r["source_id"]], "Keep only the latest delivery for each billing period.",
                            {"duplicated_rows": r["dup"], "duplicated_line_items": r["keys"], "rows": r["n"]}))
    else:
        add(CheckResult("CL-ACC-006", "PASS", "No line item appears in more than one file."))

    principal = ws.rows("""SELECT provider, billing_account_id, cost_basis,
                                  list(DISTINCT billing_period ORDER BY billing_period) AS periods
                           FROM valid WHERE strftime(charge_date, '%Y-%m') = billing_period GROUP BY ALL""")
    all_periods = sorted({p for r in principal for p in r["periods"]})
    gaps = 0
    for r in principal:
        missing = [p for p in all_periods if p not in r["periods"]]
        if missing:
            gaps += 1
            add(CheckResult("CL-ACC-008", "WARN",
                            f"No rows for {', '.join(missing)}, which other exports cover. Totals and "
                            "month-over-month comparisons for those periods omit this scope.",
                            f"{r['provider']} {r['billing_account_id'] or ''} ({r['cost_basis']})".replace("  ", " "),
                            "Supply the missing export, or compare only periods every scope covers."))
    if principal and not gaps:
        add(CheckResult("CL-ACC-008", "PASS", f"Every provider and billing account covers all "
                        f"{len(all_periods)} period(s)."))

    eff = ws.rows("""SELECT provider, billing_period, currency, sum(cost) AS billed, sum(effective_cost) AS eff
                     FROM valid WHERE cost_basis = 'billed' GROUP BY ALL
                     HAVING count(effective_cost) = count(*) AND sum(cost) <> sum(effective_cost) ORDER BY ALL""")
    for r in eff:
        add(CheckResult("CL-ACC-007", "INFO",
                        f"Billed {display(r['billed'])} vs effective {display(r['eff'])} "
                        f"(difference {display(r['billed'] - r['eff'])}). Expected when commitments are paid "
                        "in one period and amortized across others.",
                        f"{r['provider']} {r['billing_period']} {r['currency']}"))

    # ---- data quality ------------------------------------------------------
    for s in ws.recognized:
        caps, m = s.mapping.capabilities, s.mapping
        if not caps.get("resource_id"):
            hint = {
                "aws": "Enable 'Include resource IDs' on the CUR / Data Export (INCLUDE_RESOURCES).",
                "gcp": "Use the detailed usage cost export, which carries resource.name.",
            }.get(m.provider, "Include the resource identifier column in the export.")
            add(CheckResult("CL-DQ-001", "WARN", "No resource ID column: resource-level findings are impossible.",
                            _src_name(s), hint))
    cov = ws.rows("""SELECT provider, currency, sum(cost) AS total,
                            sum(cost) FILTER (WHERE resource_id IS NOT NULL) AS with_id,
                            sum(cost) FILTER (WHERE tags IS NOT NULL) AS tagged
                     FROM valid WHERE charge_category = 'usage' AND cost > 0 GROUP BY ALL ORDER BY ALL""")
    for r in cov:
        total = r["total"] or Decimal(0)
        with_id = r["with_id"] or Decimal(0)
        add(CheckResult("CL-DQ-001", "INFO", f"{pct(with_id, total)} of positive usage cost carries a resource ID.",
                        f"{r['provider']} {r['currency']}",
                        metrics={"share": pct(with_id, total)}))
        tagged = r["tagged"] or Decimal(0)
        share = (tagged / total) if total else Decimal(0)
        status = "WARN" if share < Decimal("0.5") else "PASS"
        add(CheckResult("CL-DQ-002", status,
                        f"{pct(tagged, total)} of positive usage cost carries any tag or label "
                        f"({display(total - tagged, r['currency'])} untagged).",
                        f"{r['provider']} {r['currency']}",
                        {"aws": "Activate cost allocation tags in the Billing console; tags only appear in exports "
                                "after activation and are not backfilled.",
                         "gcp": "Apply labels to projects and resources; project labels can stand in for owners.",
                         "azure": "Apply tags at resource-group level and enable tag inheritance in Cost "
                                  "Management."}.get(r["provider"]) if status == "WARN" else None,
                        {"tagged_share": pct(tagged, total), "untagged_cost": fmt(total - tagged)}))

    keys = ws.rows("""SELECT provider, k, count(*) AS n FROM (
                         SELECT provider, unnest(json_keys(tags)) AS k FROM valid
                         WHERE tags IS NOT NULL AND json_valid(tags) AND json_type(tags) = 'OBJECT')
                      GROUP BY ALL""")
    families: dict[tuple[str, str], set[str]] = defaultdict(set)
    for r in keys:
        base = r["k"].lower()
        for prefix in ("user:", "user_", "tag:"):
            if base.startswith(prefix):
                base = base[len(prefix):]
        base = {"environment": "env", "stage": "env"}.get(base, base).replace("-", "_")
        families[(r["provider"], base)].add(r["k"])
    conflicts = {k: v for k, v in families.items() if len(v) > 1}
    for (p, base), variants in sorted(conflicts.items()):
        add(CheckResult("CL-DQ-003", "WARN", f"Tag key '{base}' is spelled {len(variants)} ways: "
                        f"{', '.join(sorted(variants))}.", p,
                        "Standardize the key and use tag policies; reports group these separately until then."))
    if not conflicts and keys:
        add(CheckResult("CL-DQ-003", "PASS", "No tag key variants that differ only by case or prefix."))

    for s in ws.recognized:
        cols = [c for c in s.columns if c != "__cl_row"]
        if not cols:
            continue
        sel = ", ".join(quote_ident(c) for c in cols)
        r = ws.rows(f"""SELECT count(*) AS clusters, sum(c) AS rows FROM (
                          SELECT count(*) AS c FROM {quote_ident(s.table)} GROUP BY {sel} HAVING count(*) > 1)""")[0]
        if r["clusters"]:
            azure = s.mapping.provider == "azure"
            add(CheckResult("CL-DQ-004", "INFO" if azure else "WARN",
                            f"{r['clusters']:,} groups of fully identical rows ({r['rows']:,} rows). Kept as "
                            "separate charges" + (": Azure documents identical rows in amortized exports, and "
                                                  "de-duplicating them understates spend." if azure else
                                                  "; confirm the file was not concatenated twice."),
                            _src_name(s), metrics={"clusters": r["clusters"], "rows": r["rows"]}))
        if s.mapping.capabilities.get("split_cost_allocation"):
            add(CheckResult("CL-DQ-005", "INFO",
                            "split_line_item_* columns present. CloudLeak totals use line_item_unblended_cost only "
                            "and never add split costs on top of parent instance costs.", _src_name(s)))

    late = ws.rows("""SELECT currency, billing_period, count(*) AS n, sum(cost) AS amount FROM valid
                      WHERE provider = 'gcp' AND charge_date IS NOT NULL
                        AND strftime(charge_date, '%Y-%m') <> billing_period
                      GROUP BY ALL ORDER BY ALL""")
    for r in late:
        add(CheckResult("CL-DQ-006", "INFO",
                        f"{r['n']:,} rows ({display(r['amount'], r['currency'])}) were used in an earlier month but "
                        f"invoiced in {r['billing_period']}. CloudLeak assigns them to the invoice month.",
                        f"gcp {r['billing_period']}"))
    gcp_sources = [s for s in ws.recognized if s.mapping.provider == "gcp"]
    if gcp_sources:
        shapes = {bool(s.mapping.capabilities.get("consumption_model")) for s in gcp_sources}
        if len(shapes) > 1:
            add(CheckResult("CL-DQ-007", "WARN",
                            "Some GCP files carry consumption_model columns and others do not. Commitment "
                            "discounts are represented differently between the two shapes; compare totals, "
                            "not credit lines, across them.", "gcp"))

    neg = ws.rows("""SELECT provider, currency, count(*) AS n, sum(cost) AS amount FROM valid
                     WHERE charge_category = 'usage' AND cost < 0 GROUP BY ALL ORDER BY ALL""")
    for r in neg:
        add(CheckResult("CL-DQ-008", "INFO", f"{r['n']:,} negative usage rows totalling "
                        f"{display(r['amount'], r['currency'])} (corrections). Kept; they reduce totals.",
                        r["provider"]))

    unk = ws.rows("""SELECT provider, charge_raw, count(*) AS n, sum(cost) AS amount FROM valid
                     WHERE charge_category = 'unknown' GROUP BY ALL ORDER BY amount DESC""")
    if unk:
        for r in unk[:20]:
            add(CheckResult("CL-DQ-009", "WARN",
                            f"Unrecognized charge type {r['charge_raw']!r}: {r['n']:,} rows, "
                            f"{display(r['amount'])}. Counted in totals as 'unknown'.", r["provider"],
                            "Open an issue naming the charge type (no data rows) so it can be mapped."))
    else:
        add(CheckResult("CL-DQ-009", "PASS", "Every charge type is recognized."))

    # ---- FOCUS -------------------------------------------------------------
    focus_sources = [s for s in ws.recognized if s.detected == "focus"]
    if not focus_sources:
        add(CheckResult("CL-FOC-001", "SKIP", "No FOCUS files supplied."))
    for s in focus_sources:
        cols = Columns.from_names(s.columns)
        add(CheckResult("CL-FOC-001", "INFO",
                        f"Columns suggest FOCUS {s.mapping.focus_version} or later. Inferred from column presence; "
                        "use the FinOps Foundation focus_validator for normative conformance.", _src_name(s)))
        absent = [(k, why) for k, why in focus_adapter.KEY_COLUMNS.items() if k not in cols.by_norm]
        if absent:
            add(CheckResult("CL-FOC-002", "WARN",
                            "Missing: " + "; ".join(f"{k} ({why})" for k, why in absent) + ".", _src_name(s),
                            metrics={"missing": [k for k, _ in absent]}))
        else:
            add(CheckResult("CL-FOC-002", "PASS", "All key columns present.", _src_name(s)))
        present = [k for k in focus_adapter.KEY_COLUMNS if k in cols.by_norm]
        if present:
            exprs = ", ".join(f"count(NULLIF(TRIM({cols.ref(k)}), '')) AS {quote_ident(k)}" for k in present)
            filled = ws.rows(f"SELECT {exprs} FROM {quote_ident(s.table)}")[0]
            empty = [k for k in present if filled[k] == 0]
            if empty:
                add(CheckResult("CL-FOC-003", "WARN",
                                "Present but never populated: " + "; ".join(
                                    f"{k} ({focus_adapter.KEY_COLUMNS[k]})" for k in empty) + ".",
                                _src_name(s),
                                "Expect these analyses to be unavailable for this provider's FOCUS export; do not "
                                "join on these columns across clouds.", {"empty_columns": empty}))
            else:
                add(CheckResult("CL-FOC-003", "PASS", "Every key column present is populated.", _src_name(s)))
        if cols.has("charge_category"):
            allowed = ", ".join(f"'{v}'" for v in focus_adapter.VALID_CHARGE_CATEGORIES)
            badcat = ws.rows(f"""SELECT {cols.ref('charge_category')} AS v, count(*) AS n
                                 FROM {quote_ident(s.table)}
                                 WHERE {cols.ref('charge_category')} NOT IN ({allowed})
                                    OR {cols.ref('charge_category')} IS NULL GROUP BY 1 ORDER BY 2 DESC""")
            if badcat:
                add(CheckResult("CL-FOC-004", "WARN", "Non-standard ChargeCategory values: " + ", ".join(
                    f"{b['v']!r} ({b['n']:,})" for b in badcat[:8]) + ".", _src_name(s)))
            else:
                add(CheckResult("CL-FOC-004", "PASS", "ChargeCategory values are standard.", _src_name(s)))
        if cols.has("charge_class"):
            n = ws.scalar(f"""SELECT count(*) FROM {quote_ident(s.table)}
                              WHERE lower({cols.ref('charge_class')}) = 'correction'""")
            if n:
                add(CheckResult("CL-FOC-005", "INFO", f"{n:,} correction rows restate earlier periods.", _src_name(s)))
    return DoctorReport(out)


def _manifest_checks(ws: Workspace, add) -> None:
    """Completeness and staleness of manifest-described deliveries.

    Match each object key to one relative path inside the manifest's delivery.
    A flattened download can satisfy a key only when its basename is unique.
    """
    manifests = ws.discovery.manifests
    if not manifests:
        add(CheckResult("CL-ING-003", "INFO",
                        "No provider manifest supplied, so delivery completeness cannot be verified.",
                        remediation="Include the export's manifest JSON alongside the data files."))
        add(CheckResult("CL-ING-004", "SKIP", "No manifest row counts to compare."))
        return
    sources = list(ws.sources)
    claimed: dict[int, list] = defaultdict(list)
    for m in manifests:
        scope = m.path.name
        local = defaultdict(list)
        for s in sources:
            if m.path.parent in s.file.path.parents:
                local[s.file.path.name].append(s)
        matched = {}
        missing = []
        basename_counts = Counter(m.files)
        for key in m.keys:
            name = key.rsplit("/", 1)[-1]
            matches = []
            for s in local[name]:
                rel = s.file.path.relative_to(m.path.parent).as_posix()
                if rel == name:
                    # Allow a flattened download only for a unique basename.
                    if basename_counts[name] == 1:
                        matches.append(s)
                elif key == rel or key.endswith("/" + rel):
                    matches.append(s)
            if len(matches) == 1 and key not in matched:
                matched[key] = matches[0]
            else:
                missing.append(key)
        listed = list(matched.values())
        for s in listed:
            claimed[s.file.source_id].append(m)
        if len({s.file.source_id for s in listed}) != len(listed):
            add(CheckResult("CL-ING-003", "FAIL", "Several manifest entries resolve to one data file.", scope))
        if missing:
            add(CheckResult("CL-ING-003", "FAIL", f"{len(missing)} file(s) listed in the manifest are missing: "
                            f"{', '.join(k.rsplit('/', 1)[-1] for k in missing[:5])}.", scope,
                            "Download the complete delivery; totals from a partial delivery understate spend.",
                            {"missing": [k.rsplit("/", 1)[-1] for k in missing]}))
        elif m.keys:
            add(CheckResult("CL-ING-003", "PASS", f"All {len(m.keys)} listed file(s) present.", scope))
        if m.row_counts or m.declared_rows is not None:
            mismatches = [f"{key}: manifest {m.row_counts[key]:,} rows, read {s.rows:,}"
                          for key, s in matched.items() if key in m.row_counts and m.row_counts[key] != s.rows]
            total = sum(s.rows for s in listed)
            if m.declared_rows is not None and m.declared_rows != total:
                mismatches.append(f"delivery: manifest {m.declared_rows:,} rows, read {total:,}")
            if mismatches:
                add(CheckResult("CL-ING-004", "FAIL", "; ".join(mismatches[:5]), scope,
                                "Check the delivery for a truncated or missing partition."))
            elif not missing:
                add(CheckResult("CL-ING-004", "PASS", "Row counts match the manifest.", scope))

    # Files inside a manifest's delivery folder that no manifest lists: older assemblies / runs.
    stale = []
    for m in manifests:
        if m.kind == "unknown":
            continue
        root = m.path.parent
        for s in sources:
            if s.file.source_id in claimed:
                continue
            if root in s.file.path.parents:
                stale.append((m, s))
    for m, s in stale:
        add(CheckResult("CL-ING-003", "FAIL",
                        f"{s.file.path.parent.name}/{s.file.path.name} sits inside the delivery folder of "
                        f"{m.path.name} but is not listed in it: it belongs to an older delivery and double counts.",
                        s.file.display_name, "Delete files not listed in the current manifest, or pass only the "
                        "files the manifest lists."))

    # Two complete deliveries of the same export and period.
    groups: dict[tuple, list] = defaultdict(list)
    for m in manifests:
        if m.kind != "unknown" and m.billing_period and any(m in claimed[i] for i in claimed):
            groups[(m.kind, m.identity, m.billing_period, str(m.path.parent.parent))].append(m)
    for (kind, _ident, period, _root), ms in groups.items():
        if len(ms) > 1:
            add(CheckResult("CL-ING-003", "FAIL",
                            f"{len(ms)} deliveries of the same {kind} export for {period} are present "
                            f"({', '.join(str(x.path.parent.name) for x in ms[:4])}); every row would be counted "
                            f"{len(ms)} times.", period, "Keep only the latest delivery (newest manifest)."))
