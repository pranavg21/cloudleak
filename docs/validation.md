# Validation status

What has been tested, how, and what has not. Evidence levels:

- **TESTED (synthetic)**: an automated test builds a file in the provider's documented column layout and
  asserts exact totals or behavior. The test is named.
- **PARTIAL**: parses and normalizes, but a provider behavior that affects money has not been confirmed.
- **UNVERIFIED**: expected to work from the column names, but no test exercises it.

No row below has been validated on real customer exports yet. That is the main gap before 1.0, and the
reason [schema quirk reports](https://github.com/pranavg21/cloudleak/issues/new?template=schema-quirk.yml)
(headers only) are the most valuable contribution right now.

## Export coverage

| Export variant | Parse | Normalize | Reconcile | Evidence |
| --- | --- | --- | --- | --- |
| AWS CUR 2.0 / Data Exports, CSV and `.csv.gz` | TESTED | TESTED | TESTED | `test_totals_match_ground_truth_exactly`, `test_parquet_and_gzip_match_csv` |
| AWS CUR 2.0 Parquet (incl. `resource_tags` MAP) | TESTED | TESTED | TESTED | `test_parquet_float_columns_cast_exactly`, `test_cur2_parquet_map_tags` |
| AWS CUR 2.0 with RI and Savings Plans | TESTED | TESTED (billed and amortized) | TESTED | `test_aws_reserved_instances_and_savings_plans_billed_and_amortized` |
| AWS CUR without resource IDs (`INCLUDE_RESOURCES=FALSE`) | TESTED | TESTED | TESTED | `test_missing_resource_id_column` (doctor warns) |
| AWS CUR with split cost allocation | TESTED | PARTIAL | PARTIAL | Columns detected and reported (CL-DQ-005); whether split rows carry non-zero `line_item_unblended_cost` is not confirmed |
| AWS legacy CUR (`lineItem/…`) + manifest | TESTED | TESTED | TESTED | `test_legacy_cur_columns`, `test_d2_stale_cur_assembly_beside_manifest_fails` |
| AWS Data Exports FOCUS 1.2 | TESTED (FOCUS adapter) | PARTIAL | TESTED when `InvoiceId` is populated | Real `x_` column set not exercised |
| Azure EA actual and amortized (CSV, MM/DD/YYYY) | TESTED | TESTED | TESTED | demo corpus, `test_azure_identical_rows_kept_and_reported`, `test_mixed_basis_warns` |
| Azure MCA (camelCase headers, `invoiceId`) | TESTED | TESTED | TESTED | `test_azure_mca_camel_case_headers` |
| Azure export `manifest.json` (row counts, runs) | TESTED | n/a | n/a | `test_manifest_missing_file_and_rowcount`, `test_d2_two_azure_runs_with_manifests_fail` |
| Azure CSP | UNVERIFIED | UNVERIFIED | UNVERIFIED | Assumed MCA-like columns |
| Azure MOSP (pay-as-you-go usage details) | UNVERIFIED | UNVERIFIED | UNVERIFIED | |
| Azure FOCUS 1.0r2 / 1.2-preview | TESTED (FOCUS adapter) | PARTIAL | PARTIAL | `x_` columns covered by the redaction registry; real files not exercised |
| GCP detailed usage cost, CSV (credits as JSON text) | TESTED | TESTED | TESTED | demo corpus (net = cost + credits, `invoice.month`, late usage) |
| GCP standard usage cost, CSV | TESTED | TESTED | TESTED | dataset `gcp_standard` when no resource columns |
| GCP BigQuery export as Parquet (nested STRUCT / LIST) | TESTED | TESTED | TESTED | `test_d6_bigquery_parquet_with_nested_structs` |
| GCP 2026 spend-based CUD change (`consumption_model`) | TESTED (detected) | PARTIAL | PARTIAL | Mixed shapes flagged (CL-DQ-007); net-cost semantics after the change not confirmed against Google's documentation |
| GCP console "Cost table" CSV download | PARTIAL | PARTIAL | UNVERIFIED | Column names from memory, not a real file |
| GCP FOCUS (BigQuery view) | UNVERIFIED | UNVERIFIED | UNVERIFIED | |
| FOCUS 1.0 – 1.4, any provider | TESTED (1.2 fixture) | TESTED | TESTED when `InvoiceId` is populated | Version inferred from columns; normative conformance is the FinOps Foundation validator's job |
| OCI FOCUS reports | UNVERIFIED | UNVERIFIED | UNVERIFIED | |

## Columns CloudLeak deliberately does not add into totals

| Provider | Columns | Why |
| --- | --- | --- |
| AWS | `line_item_net_unblended_cost`, `line_item_blended_cost`, `pricing_public_on_demand_cost`, `split_line_item_*` costs | Unblended cost over all line types (including discount, tax, credit and refund lines) is the invoiced amount; the others are views of the same money |
| Azure | `PaygCostInBillingCurrency`, `CostInUsd`, `CostInPricingCurrency` | Billing-currency cost is the invoiced amount; Azure cost exports carry no tax lines |
| GCP | `cost_at_list`, `price.*`, `currency_conversion_rate` | `cost` is already in billing currency; credits are added from `credits[]` |
| FOCUS | `ListCost`, `ContractedCost`, `PricingCurrency*` | `BilledCost` is the invoiced amount; `EffectiveCost` is kept separately as amortized cost |

## Failure behavior (tested)

| Input | Result | Test |
| --- | --- | --- |
| Ragged row, duplicate header, empty file, truncated gzip, 1000:1 decompression bomb | exit 2, line number where available, nothing written | `test_malformed_files_fail_closed`, `test_truncated_gzip_fails_closed`, `test_decompression_bomb_refused` |
| Unparseable amount (`1,250.00`, `$3`) | counted, excluded, doctor FAIL | `test_unparseable_costs_are_counted_not_zeroed` |
| Same file twice; a re-delivered month; a stale assembly or export run | doctor FAIL | `test_duplicate_file_fails`, `test_redelivered_export_fails`, `test_d2_*` |
| A provider missing from one month | doctor WARN; variance reports an export gap, not "discontinued spend" | `test_d3_missing_provider_is_an_export_gap_not_discontinued_spend` |
| Unwritable output, missing path | exit 2 with the path, no traceback | `test_d4_unwritable_output_is_actionable` |
| Redaction: unclassified column; residual identifier | exit 2, output discarded | `test_unclassified_column_fails_closed`, `test_residual_gate_catches_leak` |

## Not yet tested

- Real customer exports from any provider (see top).
- Inputs above 4 GB uncompressed (see the performance table in the README for what was measured).
- Windows paths with network shares; inputs on read-only network filesystems.
