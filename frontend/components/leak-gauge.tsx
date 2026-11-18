"use client";

import { money } from "@/lib/format";

interface LeakGaugeProps {
  totalBilled: number;
  usageCost: number;
  nonUsageCost: number;
  identifiedWaste: number;
  ratioPct: number;
  currency: string;
}

export function LeakGauge({ totalBilled, usageCost, nonUsageCost, identifiedWaste, ratioPct, currency }: LeakGaugeProps) {
  const fill = Math.max(0, Math.min(100, ratioPct));
  return (
    <section aria-label="Historical idle-address charge meter" className="border border-hairline bg-panel">
      <div className="flex flex-wrap items-baseline justify-between gap-2 border-b border-hairline px-5 py-3">
        <span className="eyebrow text-[11px] text-graphite">Observed idle IPv4 charges</span>
        <span className="eyebrow text-[11px] text-graphite">{currency} · export period</span>
      </div>
      <div className="px-5 pb-6 pt-7">
        <div className="flex flex-wrap items-end justify-between gap-6">
          <div>
            <div className="font-display text-[clamp(3rem,9vw,5.5rem)] font-700 leading-[0.85] tracking-signage text-rust tabular">
              {fill.toFixed(1)}<span className="text-[0.4em] align-top">%</span>
            </div>
            <p className="mt-2 max-w-xs text-sm text-graphite">
              of usage spend matched explicit AWS idle-address billing codes. This is historical spend, not guaranteed future savings.
            </p>
          </div>
          <dl className="grid grid-cols-3 gap-x-6 gap-y-1 text-right">
            <dt className="eyebrow text-[10px] text-graphite">Invoice</dt>
            <dt className="eyebrow text-[10px] text-graphite">Usage</dt>
            <dt className="eyebrow text-[10px] text-graphite">Idle charges</dt>
            <dd className="font-mono text-base tabular text-graphite">{money(totalBilled, currency)}</dd>
            <dd className="font-mono text-base tabular">{money(usageCost, currency)}</dd>
            <dd className="font-mono text-base tabular text-rust">{money(identifiedWaste, currency)}</dd>
          </dl>
        </div>
        <div className="relative mt-8">
          <div className="relative h-14 overflow-hidden border border-ink/25 bg-concrete">
            <div className="h-full bg-rust" style={{ width: `${fill}%` }} role="meter" aria-valuenow={Number(fill.toFixed(1))} aria-valuemin={0} aria-valuemax={100} aria-label="Share of usage charges tagged AWS idle IPv4" />
            <div className="pointer-events-none absolute inset-0 flex">
              {Array.from({ length: 10 }).map((_, index) => (
                <div key={index} className="flex-1 border-r border-ink/20 last:border-r-0" />
              ))}
            </div>
          </div>
          <div className="mt-3 flex justify-between font-mono text-[10px] text-graphite tabular">
            <span>0%</span><span>50%</span><span>100% of usage spend</span>
          </div>
          {nonUsageCost !== 0 && (
            <p className="mt-4 border-l-2 border-hairline pl-3 text-xs leading-relaxed text-graphite">
              Tax, credits and other non-usage charges total {money(nonUsageCost, currency)} and are excluded from the ratio.
            </p>
          )}
        </div>
      </div>
    </section>
  );
}