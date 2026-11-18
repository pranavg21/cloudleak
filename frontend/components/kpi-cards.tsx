"use client";

import type { AuditMetrics, WasteBreakdown } from "@/lib/types";
import { count, money } from "@/lib/format";

export function KpiCards({ metrics, breakdown }: { metrics: AuditMetrics; breakdown: WasteBreakdown }) {
  const currency = metrics.billing_currency;
  const plates = [
    {
      label: "Disk spend to verify",
      amount: breakdown.disk_review_spend,
      note: "Billed capacity; attachment state is unknown",
      zeroNote: "No disk-capacity SKUs matched",
      accent: "text-brass",
    },
    {
      label: "AWS idle IPv4 charges",
      amount: breakdown.idle_ipv4_spend,
      note: "Historical idle-address usage, not forecast savings",
      zeroNote: "No explicit AWS idle-address charges found",
      accent: "text-rust",
    },
    {
      label: "Unattributed account spend",
      amount: breakdown.untagged_spend,
      note: "Ownership review; not classified as waste",
      zeroNote: "All charges have account attribution",
      accent: "text-brass",
    },
  ];
  return (
    <div className="grid grid-cols-1 gap-px border border-hairline bg-hairline sm:grid-cols-2 lg:grid-cols-4">
      {plates.map((plate) => (
        <div key={plate.label} className="bg-panel px-5 py-6">
          <p className="eyebrow text-[10px] text-graphite">{plate.label}</p>
          <p className={`mt-3 font-mono text-2xl tabular ${plate.amount === 0 ? "text-graphite/50" : plate.accent}`}>
            {money(plate.amount, currency)}
          </p>
          <p className="mt-2 text-xs leading-relaxed text-graphite">
            {plate.amount === 0 ? plate.zeroNote : plate.note}
          </p>
        </div>
      ))}
      <div className="bg-panel px-5 py-6">
        <p className="eyebrow text-[10px] text-graphite">Line items read</p>
        <p className="mt-3 font-mono text-2xl tabular text-verdigris">{count(metrics.line_items_ingested)}</p>
        <p className="mt-2 text-xs leading-relaxed text-graphite">
          {metrics.line_items_rejected > 0
            ? `${count(metrics.line_items_rejected)} skipped due to invalid costs`
            : "All cost fields parsed cleanly"}
        </p>
      </div>
    </div>
  );
}