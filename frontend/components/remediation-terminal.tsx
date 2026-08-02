"use client";

/**
 * Deletes are irreversible and these commands were generated from an uploaded
 * file, so the component is built around review rather than blind execution:
 * commands are gated behind an explicit acknowledgement, each one shows the
 * resource and its cost, and copy is available per line as well as in bulk.
 */

import { useState } from "react";
import type { RemediationCommand } from "@/lib/types";
import { moneyExact } from "@/lib/format";

interface RemediationTerminalProps {
  commands: RemediationCommand[];
  provider: string;
  currency: string;
}

export function RemediationTerminal({ commands, provider, currency }: RemediationTerminalProps) {
  const [copiedIndex, setCopiedIndex] = useState<number | null>(null);
  const [copiedAll, setCopiedAll] = useState(false);
  const [revealed, setRevealed] = useState(false);

  const copy = async (text: string, index: number | null) => {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      return; // Clipboard blocked (insecure origin); the text stays selectable.
    }
    if (index === null) {
      setCopiedAll(true);
      setTimeout(() => setCopiedAll(false), 2000);
    } else {
      setCopiedIndex(index);
      setTimeout(() => setCopiedIndex(null), 2000);
    }
  };

  const script = [
    `# CloudLeak cleanup plan — ${provider}`,
    "# Review every line. These deletions cannot be undone.",
    "set -euo pipefail",
    "",
    ...commands.flatMap((command) => [
      `# ${command.finding} — ${command.resource_id} — ${moneyExact(command.monthly_cost, currency)}`,
      command.command,
    ]),
  ].join("\n");

  return (
    <section className="border border-hairline bg-panel">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-hairline px-5 py-3">
        <span className="eyebrow text-[11px] text-graphite">Cleanup plan · {provider}</span>
        {commands.length > 0 && revealed && (
          <button
            type="button"
            onClick={() => void copy(script, null)}
            className="bg-ink px-3 py-1.5 font-mono text-[10px] uppercase tracking-[0.12em] text-panel transition-colors hover:bg-verdigris"
          >
            {copiedAll ? "Copied" : "Copy as script"}
          </button>
        )}
      </div>

      {commands.length === 0 ? (
        <p className="px-5 py-8 text-sm text-graphite">
          No resource matched a cleanup rule, so there is nothing to run. The waste figures above
          still hold; they just have no single command that resolves them.
        </p>
      ) : !revealed ? (
        <div className="px-5 py-8">
          <p className="text-sm leading-relaxed text-ink">
            {commands.length} deletion {commands.length === 1 ? "command" : "commands"} were
            generated from your export. They permanently destroy resources and their data.
          </p>
          <p className="mt-2 text-sm leading-relaxed text-graphite">
            Billing data alone cannot prove a disk is detached. Confirm each resource in your
            console before running anything.
          </p>
          <button
            type="button"
            onClick={() => setRevealed(true)}
            className="mt-5 border border-ink px-4 py-2 font-mono text-[11px] uppercase tracking-[0.12em] text-ink transition-colors hover:bg-ink hover:text-panel"
          >
            Show the commands
          </button>
        </div>
      ) : (
        <ol className="max-h-[26rem] divide-y divide-hairline overflow-y-auto">
          {commands.map((command, index) => (
            <li key={`${command.command}-${index}`} className="px-5 py-4">
              <div className="flex items-baseline justify-between gap-4">
                <span className="eyebrow text-[10px] text-graphite">{command.finding}</span>
                <span className="font-mono text-xs tabular text-rust">
                  {moneyExact(command.monthly_cost, currency)}
                </span>
              </div>
              <div className="mt-2 flex items-start gap-3">
                <code className="flex-1 break-all font-mono text-xs leading-relaxed text-ink">
                  <span className="select-none text-hairline">$ </span>
                  {command.command}
                </code>
                <button
                  type="button"
                  onClick={() => void copy(command.command, index)}
                  aria-label={`Copy command for ${command.resource_id}`}
                  className="shrink-0 border border-hairline px-2 py-1 font-mono text-[10px] uppercase tracking-[0.1em] text-graphite transition-colors hover:border-ink hover:text-ink"
                >
                  {copiedIndex === index ? "Copied" : "Copy"}
                </button>
              </div>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
