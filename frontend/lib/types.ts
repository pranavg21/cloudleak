/** Matches backend/schemas/focus_schema.py (cloudleak-internal-v2). */

export type EvidenceStatus = "idle_charge_observed" | "needs_verification";

export interface AuditMetrics {
  total_billed_cost: number;
  billing_currency: string;
  line_items_ingested: number;
  line_items_rejected: number;
  usage_cost: number;
  non_usage_cost: number;
  non_usage_line_items: number;
  observed_idle_spend: number;
  review_spend: number;
  idle_charge_ratio_pct: number;
}

export interface WasteBreakdown {
  disk_review_spend: number;
  idle_ipv4_spend: number;
  untagged_spend: number;
}

export interface TopLeak {
  resource_id: string;
  sub_account_name: string;
  service_category: string;
  region: string;
  finding: string;
  evidence_status: EvidenceStatus;
  observed_cost: number;
}

export interface RuleFinding {
  key: string;
  title: string;
  resource_count: number;
  matched_spend: number;
  evidence_status: EvidenceStatus;
  rationale: string;
  verification: string;
}

export interface CategorySpend {
  label: string;
  amount: number;
  observed_idle_spend: number;
}

export interface FocusVersionInfo {
  detected_version: string | null;
  label: string;
  confidence: number;
  reasoning: string;
  unsupported_rules: string[];
}

export interface AuditReport {
  schema_version: string;
  detected_provider: string;
  detection_confidence: number;
  focus_version: FocusVersionInfo;
  metrics: AuditMetrics;
  waste_breakdown: WasteBreakdown;
  rule_findings: RuleFinding[];
  spend_by_service: CategorySpend[];
  spend_by_account: CategorySpend[];
  top_leaks: TopLeak[];
  assumptions: string[];
}

export const REPORT_STORAGE_KEY = "cloudleak.report.v2";

export type JobStatus = "queued" | "running" | "succeeded" | "failed";

export interface JobAccepted {
  job_id: string;
  status: JobStatus;
  status_url: string;
  queue_depth: number;
  poll_after_ms: number;
}

export interface JobState {
  job_id: string;
  status: JobStatus;
  filename: string;
  queued_ms: number;
  duration_ms: number | null;
  report: AuditReport | null;
  error: string | null;
}