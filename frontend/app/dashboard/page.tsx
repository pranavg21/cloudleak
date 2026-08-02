"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import {
  CostCompositionBar,
  SpendBreakdownChart,
  WasteByRuleChart,
} from "@/components/charts";
import { KpiCards } from "@/components/kpi-cards";
import { LeakGauge } from "@/components/leak-gauge";
import { LeakTable } from "@/components/leak-table";
import { RemediationTerminal } from "@/components/remediation-terminal";
import { REPORT_STORAGE_KEY, type AuditReport } from "@/lib/types";

type LoadState = "loading" | "ready" | "empty";

export default function DashboardPage() {
  const [report, setReport] = useState<AuditReport | null>(null);
  const [state, setState] = useState<LoadState>("loading");

  useEffect(() => {
    // sessionStorage is only available in the browser, so this runs after mount.
    const raw = sessionStorage.getItem(REPORT_STORAGE_KEY);
    if (!raw) {
      setState("empty");
      return;
    }
    try {
      setReport(JSON.parse(raw) as AuditReport);
      setState("ready");
    } catch {
      sessionStorage.removeItem(REPORT_STORAGE_KEY);
      setState("empty");
    }
  }, []);

  if (state === "loading") {
    return (
      <main className="measure-grid flex min-h-screen items-center justify-center">
        <p className="font-mono text-xs uppercase tracking-[0.14em] text-graphite">
          Opening report
        </p>
      </main>
    );
  }

  if (state === "empty" || !report) {
    return (
      <main className="measure-grid flex min-h-screen items-center justify-center px-6">
        <div className="max-w-md text-center">
          <h1 className="font-display text-3xl font-700 tracking-signage">No report open</h1>
          <p className="mt-3 text-sm leading-relaxed text-graphite">
            Reports live in this browser tab only, so they clear when the tab closes. Upload a
            billing export to run a new audit.
          </p>
          <Link
            href="/"
            className="mt-6 inline-block bg-ink px-5 py-3 font-mono text-xs uppercase tracking-[0.14em] text-panel transition-colors hover:bg-verdigris"
          >
            Upload an export
          </Link>
        </div>
      </main>
    );
  }

  const {
    metrics,
    waste_breakdown,
    top_leaks,
    remediation_commands,
    assumptions,
    rule_findings,
    spend_by_service,
    spend_by_account,
    focus_version,
  } = report;
  const currency = metrics.billing_currency;
  const lowConfidence = report.detection_confidence < 0.5;

  return (
    <main className="measure-grid min-h-screen">
      <header className="border-b border-hairline bg-panel/70">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-3 px-6 py-4">
          <Link href="/" className="font-display text-lg font-700 tracking-signage">
            CloudLeak
          </Link>
          <div className="flex items-center gap-5 font-mono text-[10px] uppercase tracking-[0.14em] text-graphite">
            <span>
              {report.detected_provider} · {(report.detection_confidence * 100).toFixed(0)}% match
            </span>
            <span>{focus_version.label}</span>
            <Link href="/" className="border-b border-graphite hover:border-ink hover:text-ink">
              New audit
            </Link>
          </div>
        </div>
      </header>

      <div className="mx-auto max-w-6xl space-y-8 px-6 py-10">
        {lowConfidence && (
          <p
            role="alert"
            className="border-l-2 border-brass bg-brass/5 px-4 py-3 text-sm leading-relaxed text-ink"
          >
            Only {(report.detection_confidence * 100).toFixed(0)}% of the expected{" "}
            {report.detected_provider} columns were found in this file. Check the detected provider
            before you act on anything below.
          </p>
        )}

        <LeakGauge
          totalBilled={metrics.total_billed_cost}
          usageCost={metrics.usage_cost}
          nonUsageCost={metrics.non_usage_cost}
          identifiedWaste={metrics.identified_waste}
          ratioPct={metrics.waste_leak_ratio_pct}
          currency={currency}
        />

        <KpiCards metrics={metrics} breakdown={waste_breakdown} />

        <CostCompositionBar
          usage={metrics.usage_cost}
          nonUsage={metrics.non_usage_cost}
          waste={metrics.identified_waste}
          currency={currency}
        />

        <WasteByRuleChart findings={rule_findings} currency={currency} />

        <div className="grid gap-8 lg:grid-cols-2">
          <SpendBreakdownChart
            title="Spend by service"
            data={spend_by_service}
            currency={currency}
          />
          <SpendBreakdownChart
            title="Spend by account"
            data={spend_by_account}
            currency={currency}
          />
        </div>

        {focus_version.detected_version && (
          <section className="border border-hairline bg-panel px-5 py-4">
            <span className="eyebrow text-[11px] text-graphite">Schema</span>
            <p className="mt-2 text-sm leading-relaxed text-ink">
              This export follows <strong>{focus_version.label}</strong>.{" "}
              <span className="text-graphite">{focus_version.reasoning}</span>
            </p>
            {focus_version.unsupported_rules.length > 0 && (
              <p className="mt-2 text-sm leading-relaxed text-graphite">
                Analyses unavailable at this version:{" "}
                {focus_version.unsupported_rules.map((r) => r.replace(/_/g, " ")).join(", ")}.
                They were skipped rather than reported as zero.
              </p>
            )}
          </section>
        )}

        <div className="grid gap-8 lg:grid-cols-2">
          <LeakTable leaks={top_leaks} currency={currency} />
          <RemediationTerminal
            commands={remediation_commands}
            provider={report.detected_provider}
            currency={currency}
          />
        </div>

        <section className="border border-hairline bg-panel">
          <div className="border-b border-hairline px-5 py-3">
            <span className="eyebrow text-[11px] text-graphite">How these numbers were reached</span>
          </div>
          <ul className="space-y-3 px-5 py-5">
            {assumptions.map((assumption) => (
              <li key={assumption} className="flex gap-3 text-sm leading-relaxed text-graphite">
                <span aria-hidden="true" className="mt-[0.45em] h-px w-3 shrink-0 bg-hairline" />
                {assumption}
              </li>
            ))}
          </ul>
        </section>
      </div>
    </main>
  );
}
