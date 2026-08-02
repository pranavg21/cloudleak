"use client";

import type { AuditMetrics, WasteBreakdown } from "@/lib/types";
import { count, money } from "@/lib/format";

interface KpiCardsProps {
  metrics: AuditMetrics;
  breakdown: WasteBreakdown;
}

/**
 * A zero is a real result, not a blank. Each plate carries a separate caption
 * for the zero case, because "$0" under "Disks billing for capacity nothing
 * reads" reads as a failure rather than as a clean bill of health.
 */
export function KpiCards({ metrics, breakdown }: KpiCardsProps) {
  const currency = metrics.billing_currency;

  const plates = [
    {
      label: "Orphaned storage",
      amount: breakdown.orphaned_storage_waste,
      value: money(breakdown.orphaned_storage_waste, currency),
      note: "Disks and volumes billing for capacity nothing reads",
      zeroNote: "No unattached disks or volumes matched",
      accent: "text-rust",
    },
    {
      label: "Idle public IPs",
      amount: breakdown.zombie_ip_waste,
      value: money(breakdown.zombie_ip_waste, currency),
      note: "Addresses reserved and billed while unattached",
      zeroNote: "No idle-address charges found",
      accent: "text-rust",
    },
    {
      label: "Untagged spend",
      amount: breakdown.untagged_spend,
      value: money(breakdown.untagged_spend, currency),
      note: "Has an owner somewhere, just not in the data",
      zeroNote: "Every charge is attributed to an account",
      accent: "text-brass",
    },
  ];

  return (
    <div className="grid grid-cols-1 gap-px border border-hairline bg-hairline sm:grid-cols-2 lg:grid-cols-4">
      {plates.map((plate) => {
        const isZero = plate.amount === 0;
        return (
          <div key={plate.label} className="bg-panel px-5 py-6">
            <p className="eyebrow text-[10px] text-graphite">{plate.label}</p>
            <p
              className={`mt-3 font-mono text-2xl tabular ${
                isZero ? "text-graphite/50" : plate.accent
              }`}
            >
              {plate.value}
            </p>
            <p className="mt-2 text-xs leading-relaxed text-graphite">
              {isZero ? plate.zeroNote : plate.note}
            </p>
          </div>
        );
      })}

      <div className="bg-panel px-5 py-6">
        <p className="eyebrow text-[10px] text-graphite">Line items read</p>
        <p className="mt-3 font-mono text-2xl tabular text-verdigris">
          {count(metrics.line_items_ingested)}
        </p>
        <p className="mt-2 text-xs leading-relaxed text-graphite">
          {metrics.line_items_rejected > 0
            ? `${count(metrics.line_items_rejected)} skipped: unreadable cost value`
            : "Every row parsed cleanly"}
        </p>
      </div>
    </div>
  );
}
