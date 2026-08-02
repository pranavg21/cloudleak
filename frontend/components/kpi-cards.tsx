"use client";

import type { AuditMetrics, WasteBreakdown } from "@/lib/types";
import { count, money } from "@/lib/format";

interface KpiCardsProps {
  metrics: AuditMetrics;
  breakdown: WasteBreakdown;
}

export function KpiCards({ metrics, breakdown }: KpiCardsProps) {
  const currency = metrics.billing_currency;

  const plates = [
    {
      label: "Orphaned storage",
      value: money(breakdown.orphaned_storage_waste, currency),
      note: "Disks and volumes billing for capacity nothing reads",
      accent: "text-rust",
    },
    {
      label: "Idle public IPs",
      value: money(breakdown.zombie_ip_waste, currency),
      note: "Addresses reserved and billed while unattached",
      accent: "text-rust",
    },
    {
      label: "Untagged spend",
      value: money(breakdown.untagged_spend, currency),
      note: "Has an owner somewhere, just not in the data",
      accent: "text-brass",
    },
    {
      label: "Line items read",
      value: count(metrics.line_items_ingested),
      note:
        metrics.line_items_rejected > 0
          ? `${count(metrics.line_items_rejected)} skipped: unreadable cost value`
          : "Every row parsed cleanly",
      accent: "text-verdigris",
    },
  ];

  return (
    <div className="grid grid-cols-1 gap-px border border-hairline bg-hairline sm:grid-cols-2 lg:grid-cols-4">
      {plates.map((plate) => (
        <div key={plate.label} className="bg-panel px-5 py-6">
          <p className="eyebrow text-[10px] text-graphite">{plate.label}</p>
          <p className={`mt-3 font-mono text-2xl tabular ${plate.accent}`}>{plate.value}</p>
          <p className="mt-2 text-xs leading-relaxed text-graphite">{plate.note}</p>
        </div>
      ))}
    </div>
  );
}
