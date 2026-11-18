"use client";

/**
 * Charts, drawn as plain SVG.
 *
 * No charting library: the shapes needed here are bars and a stacked ratio,
 * and a dependency would add ~50 kB to render rectangles. Every chart carries
 * the underlying numbers as text too, because a report that only works
 * visually is unreadable when printed to PDF or read by a screen reader.
 */

import type { CategorySpend, RuleFinding } from "@/lib/types";
import { money } from "@/lib/format";

// --- Waste by rule ------------------------------------------------------------

export function WasteByRuleChart({ findings, currency }: { findings: RuleFinding[]; currency: string }) {
  if (findings.length === 0) return null;
  const max = Math.max(...findings.map((finding) => finding.matched_spend));
  return (
    <section className="border border-hairline bg-panel">
      <div className="border-b border-hairline px-5 py-3">
        <span className="eyebrow text-[11px] text-graphite">Billing signatures to investigate</span>
      </div>
      <ul className="space-y-4 px-5 py-5">
        {findings.map((finding) => (
          <li key={finding.key}>
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <span className="text-sm font-500">{finding.title}</span>
              <span className="font-mono text-xs tabular text-graphite">
                {finding.resource_count} resource{finding.resource_count === 1 ? "" : "s"} · {money(finding.matched_spend, currency)} billed
              </span>
            </div>
            <div className="relative mt-2 h-3 border border-hairline bg-concrete" role="img" aria-label={`${finding.title}: ${money(finding.matched_spend, currency)} billed`}>
              <div className="h-full bg-verdigris/70" style={{ width: `${max > 0 ? finding.matched_spend / max * 100 : 0}%` }} />
            </div>
            <p className="mt-1.5 text-[11px] leading-relaxed text-graphite">
              {finding.evidence_status === "idle_charge_observed" ? "Idle-address charge observed." : "Needs external verification."} {finding.rationale}
            </p>
            <p className="mt-1 text-[11px] leading-relaxed text-graphite">Check: {finding.verification}</p>
          </li>
        ))}
      </ul>
    </section>
  );
}

// --- Spend breakdown ----------------------------------------------------------

export function SpendBreakdownChart({
  title,
  data,
  currency,
}: {
  title: string;
  data: CategorySpend[];
  currency: string;
}) {
  if (data.length === 0) return null;

  const max = Math.max(...data.map((d) => d.amount));

  return (
    <section className="border border-hairline bg-panel">
      <div className="border-b border-hairline px-5 py-3">
        <span className="eyebrow text-[11px] text-graphite">{title}</span>
      </div>

      <ul className="space-y-3 px-5 py-5">
        {data.map((item) => {
          const barPct = max > 0 ? (item.amount / max) * 100 : 0;
          const idlePct = item.amount > 0 ? (item.observed_idle_spend / item.amount) * 100 : 0;

          return (
            <li key={item.label}>
              <div className="flex items-baseline justify-between gap-3">
                <span className="truncate text-xs" title={item.label}>
                  {item.label}
                </span>
                <span className="shrink-0 font-mono text-xs tabular text-graphite">
                  {money(item.amount, currency)}
                  {item.observed_idle_spend > 0 && (
                    <span className="text-rust"> · {money(item.observed_idle_spend, currency)} idle IPv4</span>
                  )}
                </span>
              </div>
              <div
                className="mt-1 h-3 bg-concrete"
                role="img"
                aria-label={`${item.label}: ${money(item.amount, currency)}`}
              >
                <div className="h-full bg-verdigris/50" style={{ width: `${barPct}%` }}>
                  <div className="h-full bg-rust" style={{ width: `${idlePct}%` }} />
                </div>
              </div>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

// --- Cost composition ---------------------------------------------------------

export function CostCompositionBar({
  usage,
  nonUsage,
  waste,
  currency,
}: {
  usage: number;
  nonUsage: number;
  waste: number;
  currency: string;
}) {
  const total = Math.abs(usage) + Math.abs(nonUsage);
  if (total === 0) return null;

  const usagePct = (Math.abs(usage) / total) * 100;
  const wastePct = usage > 0 ? (waste / usage) * usagePct : 0;

  const segments = [
    { label: "Observed idle IPv4", value: waste, width: wastePct, className: "bg-rust" },
    {
      label: "Other usage charges",
      value: usage - waste,
      width: usagePct - wastePct,
      className: "bg-verdigris",
    },
    {
      label: "Tax, credits & fees",
      value: nonUsage,
      width: 100 - usagePct,
      className: "bg-brass",
    },
  ].filter((segment) => segment.width > 0.01);

  return (
    <section className="border border-hairline bg-panel">
      <div className="border-b border-hairline px-5 py-3">
        <span className="eyebrow text-[11px] text-graphite">What the invoice is made of</span>
      </div>

      <div className="px-5 py-5">
        <div className="flex h-8 overflow-hidden border border-hairline">
          {segments.map((segment) => (
            <div
              key={segment.label}
              className={segment.className}
              style={{ width: `${segment.width}%` }}
              role="img"
              aria-label={`${segment.label}: ${money(segment.value, currency)}`}
            />
          ))}
        </div>

        <dl className="mt-4 space-y-1.5">
          {segments.map((segment) => (
            <div key={segment.label} className="flex items-center gap-2 text-xs">
              <span className={`h-2.5 w-2.5 shrink-0 ${segment.className}`} aria-hidden="true" />
              <dt className="flex-1 text-graphite">{segment.label}</dt>
              <dd className="font-mono tabular">{money(segment.value, currency)}</dd>
            </div>
          ))}
        </dl>
      </div>
    </section>
  );
}