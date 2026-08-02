"use client";

/**
 * Uploads an export and follows the resulting job to completion.
 *
 * Requests go to this app's own /api/audit routes rather than to the audit
 * service directly, so the API key stays on the server and never reaches the
 * browser bundle.
 */

import React, { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { REPORT_STORAGE_KEY, type JobAccepted, type JobState } from "@/lib/types";

const POLL_INTERVAL_MS = 800;
const POLL_TIMEOUT_MS = 180_000;

type Phase = "idle" | "uploading" | "queued" | "running";

const PHASE_COPY: Record<Exclude<Phase, "idle">, string> = {
  uploading: "Sending the export",
  queued: "Waiting for a worker",
  running: "Reading the meter",
};

export function CsvUploader() {
  const [phase, setPhase] = useState<Phase>("idle");
  const [error, setError] = useState<string | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  const [filename, setFilename] = useState<string | null>(null);
  const [queueDepth, setQueueDepth] = useState(0);

  const inputRef = useRef<HTMLInputElement>(null);
  const cancelled = useRef(false);
  const router = useRouter();

  // A poll loop that outlives the component would keep writing to dead state.
  useEffect(() => {
    cancelled.current = false;
    return () => {
      cancelled.current = true;
    };
  }, []);

  const followJob = useCallback(
    async (jobId: string) => {
      const deadline = Date.now() + POLL_TIMEOUT_MS;

      while (Date.now() < deadline) {
        if (cancelled.current) return;
        await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
        if (cancelled.current) return;

        let state: JobState;
        try {
          const response = await fetch(`/api/audit/jobs/${jobId}`, { cache: "no-store" });
          const payload = await response.json().catch(() => null);

          if (!response.ok) {
            setError(payload?.detail ?? "The audit could not be completed.");
            setPhase("idle");
            return;
          }
          state = payload as JobState;
        } catch {
          setError("Lost contact with the audit service while the report was running.");
          setPhase("idle");
          return;
        }

        if (state.status === "running") setPhase("running");

        if (state.status === "succeeded" && state.report) {
          // Session storage, not local storage: billing data clears with the tab.
          sessionStorage.setItem(REPORT_STORAGE_KEY, JSON.stringify(state.report));
          router.push("/dashboard");
          return;
        }

        if (state.status === "failed") {
          setError(state.error ?? "The audit failed. Check the export and try again.");
          setPhase("idle");
          return;
        }
      }

      setError("The audit is taking longer than expected. Try a single billing period.");
      setPhase("idle");
    },
    [router],
  );

  const audit = useCallback(
    async (file: File) => {
      setError(null);
      setFilename(file.name);
      setPhase("uploading");

      const form = new FormData();
      form.append("file", file);

      let accepted: JobAccepted;
      try {
        const response = await fetch("/api/audit", { method: "POST", body: form });
        const payload = await response.json().catch(() => null);

        if (response.status === 429) {
          const retryAfter = response.headers.get("Retry-After");
          setError(
            payload?.detail ??
              `Too many audits at once.${retryAfter ? ` Try again in ${retryAfter}s.` : ""}`,
          );
          setPhase("idle");
          return;
        }

        if (!response.ok) {
          setError(payload?.detail ?? "The audit could not be started.");
          setPhase("idle");
          return;
        }

        accepted = payload as JobAccepted;
      } catch {
        setError("Could not reach the audit service. Start the backend, then upload again.");
        setPhase("idle");
        return;
      }

      setQueueDepth(accepted.queue_depth);
      setPhase("queued");
      void followJob(accepted.job_id);
    },
    [followJob],
  );

  const busy = phase !== "idle";

  return (
    <div>
      <div
        onDragOver={(event) => {
          event.preventDefault();
          if (!busy) setIsDragging(true);
        }}
        onDragLeave={() => setIsDragging(false)}
        onDrop={(event) => {
          event.preventDefault();
          setIsDragging(false);
          if (busy) return;
          const file = event.dataTransfer.files?.[0];
          if (file) void audit(file);
        }}
        className={`relative border-2 border-dashed bg-panel px-6 py-12 text-center transition-colors ${
          isDragging ? "border-verdigris bg-verdigris/5" : "border-hairline"
        }`}
      >
        <input
          ref={inputRef}
          id="billing-export"
          type="file"
          accept=".csv,text/csv"
          className="sr-only"
          disabled={busy}
          onChange={(event) => {
            const file = event.target.files?.[0];
            if (file) void audit(file);
            event.target.value = "";
          }}
        />

        <p className="font-display text-2xl font-600 tracking-signage">
          {busy ? `${PHASE_COPY[phase as Exclude<Phase, "idle">]}…` : "Drop your billing export here"}
        </p>

        <p className="mx-auto mt-2 max-w-md text-sm text-graphite">
          {phase === "idle" &&
            "A raw .csv from Azure Cost Management, an AWS Cost and Usage Report, or a GCP billing export. CloudLeak works out which one it is."}
          {phase === "uploading" && filename}
          {phase === "queued" &&
            (queueDepth > 0
              ? `${queueDepth} audit${queueDepth === 1 ? "" : "s"} ahead of yours.`
              : "Your audit is next in line.")}
          {phase === "running" && `Normalizing ${filename}.`}
        </p>

        <button
          type="button"
          onClick={() => inputRef.current?.click()}
          disabled={busy}
          className="mt-6 bg-ink px-6 py-3 font-mono text-xs uppercase tracking-[0.14em] text-panel transition-colors hover:bg-verdigris disabled:cursor-not-allowed disabled:opacity-50"
        >
          {busy ? "Auditing" : "Choose a file"}
        </button>

        {busy && (
          <div
            className="mt-6 h-[2px] w-full overflow-hidden bg-hairline"
            role="status"
            aria-live="polite"
            aria-label={PHASE_COPY[phase as Exclude<Phase, "idle">]}
          >
            <div className="gauge-fill h-full w-full bg-verdigris" />
          </div>
        )}
      </div>

      {error && (
        <p role="alert" className="mt-4 border-l-2 border-rust bg-rust/5 px-4 py-3 text-sm text-ink">
          {error}
        </p>
      )}
    </div>
  );
}
