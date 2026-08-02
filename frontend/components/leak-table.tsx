"use client";

import type { TopLeak } from "@/lib/types";
import { moneyExact } from "@/lib/format";

export function LeakTable({ leaks, currency }: { leaks: TopLeak[]; currency: string }) {
  return (
    <section className="border border-hairline bg-panel">
      <div className="border-b border-hairline px-5 py-3">
        <span className="eyebrow text-[11px] text-graphite">Ranked findings</span>
      </div>

      {leaks.length === 0 ? (
        <p className="px-5 py-8 text-sm text-graphite">
          No resource matched a waste rule in this export.
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b border-hairline">
                <th className="eyebrow px-5 py-2 text-[10px] font-500 text-graphite">Resource</th>
                <th className="eyebrow px-5 py-2 text-[10px] font-500 text-graphite">Account</th>
                <th className="eyebrow px-5 py-2 text-[10px] font-500 text-graphite">Region</th>
                <th className="eyebrow px-5 py-2 text-right text-[10px] font-500 text-graphite">
                  Recoverable
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-hairline">
              {leaks.map((leak, index) => (
                <tr key={`${leak.resource_id}-${index}`}>
                  <td className="px-5 py-3">
                    <span className="block break-all font-mono text-xs text-ink">
                      {leak.resource_id}
                    </span>
                    <span className="mt-0.5 block text-[11px] text-graphite">{leak.finding}</span>
                  </td>
                  <td className="px-5 py-3 font-mono text-xs text-graphite">
                    {leak.sub_account_name}
                  </td>
                  <td className="px-5 py-3 font-mono text-xs text-graphite">{leak.region}</td>
                  <td className="px-5 py-3 text-right font-mono text-xs tabular text-rust">
                    {moneyExact(leak.estimated_waste, currency)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
