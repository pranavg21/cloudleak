/** Mirrors backend/schemas/focus_schema.py. Keep both sides in sync. */

export interface AuditMetrics {
  total_billed_cost: number;
  identified_waste: number;
  waste_leak_ratio_pct: number;
  billing_currency: string;
  line_items_ingested: number;
  line_items_rejected: number;
  usage_cost: number;
  non_usage_cost: number;
  non_usage_line_items: number;
}

export interface WasteBreakdown {
  orphaned_storage_waste: number;
  zombie_ip_waste: number;
  untagged_spend: number;
}

export interface RemediationCommand {
  resource_id: string;
  sub_account_name: string;
  monthly_cost: number;
  finding: string;
  command: string;
}

export interface TopLeak {
  resource_id: string;
  sub_account_name: string;
  service_category: string;
  region: string;
  finding: string;
  estimated_waste: number;
}

export interface RuleFinding {
  key: string;
  title: string;
  resource_count: number;
  matched_spend: number;
  estimated_waste: number;
  reclaim_factor: number;
  rationale: string;
}

export interface CategorySpend {
  label: string;
  amount: number;
  waste: number;
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
  remediation_commands: RemediationCommand[];
  assumptions: string[];
}

/** Billing data is held for the session only and never persisted to disk. */
export const REPORT_STORAGE_KEY = "cloudleak.report";

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
