# Contributing / engineering notes

Zero-IAM multi-cloud cost governance. Users upload a billing export; the engine
normalizes it into an internal schema modelled on FOCUS, runs deterministic waste heuristics, and emits
reviewable cleanup commands. No credentials, no live cloud connection, no
stored files.

## Stack
- Backend: Python 3.12, FastAPI, Pydantic v2, pandas. All math deterministic.
- Frontend: Next.js 14 App Router, TypeScript, Tailwind.
- Data model: internal schema modelled on the FinOps FOCUS specification (not conformant -- see README).

## Non-negotiables

1. **Shell-quote everything.** Any value from an uploaded file that reaches a
   generated command goes through `shlex.quote`. Users are told to paste this
   output into a terminal; unquoted interpolation is remote code execution.
   `backend/tests/test_engine.py::test_commands_are_shell_quoted` guards this.
2. **Never run a destructive command.** CloudLeak generates plans. The user
   confirms and executes. The UI gates commands behind explicit acknowledgement.
3. **Aggregate before you count.** Billing exports carry one row per resource
   per day. Group by resource before generating findings or commands, or a
   single disk becomes 700 findings.
4. **Don't zero bad data.** A cost value that will not parse is rejected and
   counted, never coerced to 0.0 — a zeroed row silently shrinks the waste
   ratio's denominator.
5. **State every assumption.** Billing data cannot prove a disk is detached.
   Every estimate the engine makes is surfaced in `assumptions` on the response
   and rendered on the dashboard. Do not quietly change a reclaim factor.
6. **No wildcard CORS.** Origins come from `CLOUDLEAK_ALLOWED_ORIGINS`.
7. **No per-row Pydantic.** Ingestion is vectorized. A CUR file has millions of
   rows; Pydantic validates the response contract, not each line item.

8. **The API key never reaches the browser.** The frontend calls its own
   `/api/audit` routes; those attach the key server-side. Never introduce a
   `NEXT_PUBLIC_*` variable holding a secret -- that prefix inlines the value
   into the client bundle.
9. **Audits run on the queue, not in the request.** Parsing is CPU-bound and
   blocks the event loop; workers dispatch it to a thread pool. Adding a
   synchronous parse back into a handler regresses this.
10. **Fail closed.** Production without configured keys must raise at startup,
    never degrade to an open endpoint.

## Layout
```
backend/
  core/config.py            Settings + startup safety checks
  core/security.py          API key auth, constant-time comparison
  core/ratelimit.py         Sliding window; memory or Redis backend
  keygen.py                 Mint an API key + its digest
  schemas/focus_schema.py   Internal schema contract + response models
  schemas/job_schema.py     Async job response models
  services/job_queue.py     Bounded queue, worker pool, TTL store
  services/parsers.py       Header-signature detection + vectorized normalization
  services/audit_engine.py  Heuristics, findings, command generation
  routers/audit.py          Upload endpoint, size caps, error mapping
  tests/test_engine.py      Engine correctness, runs against ../samples
  tests/test_api.py         Auth, rate limits, queue, startup safety
frontend/
  app/api/audit/*           Server-side proxy; holds the API key
  app/page.tsx              Landing; the uploader is the hero
  app/dashboard/page.tsx    Report
  components/leak-gauge.tsx Signature element: the billing meter
samples/                    Azure/AWS/GCP exports + malformed + injection probe
```

## Adding a heuristic
1. Add the match patterns as module constants in `audit_engine.py`. Match on
   canonical FOCUS categories, never on a vendor column, so one rule covers
   all three clouds.
2. Add a reclaim factor and state it in `assumptions`.
3. Add the per-provider command to `_build_command`, quoting every value.
4. Add a sample row to each file in `samples/` and a test asserting the finding.
