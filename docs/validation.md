# Validation status

What has been tested, how, and what has not. Evidence levels:

- **TESTED (synthetic)**: an automated test builds a file in the provider's documented column layout and
  asserts exact totals or behavior. Evidence identifies the CI test or the separate release review.
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
| AWS Data Exports FOCUS 1.2 | TESTED (FOCUS adapter) | PARTIAL | TESTED by period | Release review used the AWS 60-column default query with synthetic charges totaling USD 9.00 by period. Invoice finality remained unknown. |
| Azure EA actual and amortized (CSV, MM/DD/YYYY) | TESTED | TESTED | TESTED | demo corpus, `test_azure_identical_rows_kept_and_reported`, `test_mixed_basis_warns` |
| Azure MCA (camelCase headers, `invoiceId`) | TESTED | TESTED | TESTED | `test_azure_mca_camel_case_headers` |
| Azure export `manifest.json` (row counts, runs) | TESTED | n/a | n/a | `test_manifest_missing_file_and_rowcount`, `test_d2_two_azure_runs_with_manifests_fail` |
| Azure MPA / CSP Cost Management, selected fields | TESTED | TESTED | TESTED | `test_azure_ea_mosp_and_mpa_cost_management_fields` compares USD 7.25. The separate Partner Center invoice shape is refused by `test_azure_partner_center_invoice_is_not_read_as_cost_management`. |
| Azure MOSP, selected usage details | TESTED | TESTED | TESTED | `test_azure_ea_mosp_and_mpa_cost_management_fields` compares USD 7.25 by invoice ID. Taxes and credits absent from the export remain unverified. |
| Azure FOCUS 1.0 preview, 1.0, 1.0r2, 1.2 preview and 1.2 Microsoft translator projections | TESTED | TESTED for billed currency | TESTED by period and invoice ID | Separate release review compared EUR 9.95 in five synthetic projections. CI checks the 1.0 invoice extension in `test_azure_extension_and_header_lower_bounds`. |
| GCP detailed usage cost, CSV (credits as JSON text) | TESTED | TESTED | TESTED | demo corpus (net = cost + credits, `invoice.month`, late usage) |
| GCP standard usage cost, CSV | TESTED | TESTED | TESTED | dataset `gcp_standard` when no resource columns |
| GCP BigQuery export as Parquet (nested STRUCT / LIST) | TESTED | TESTED | TESTED | `test_d6_bigquery_parquet_with_nested_structs` |
| GCP 2026 spend-based CUD change (`consumption_model`) | TESTED (detected) | PARTIAL | PARTIAL | Mixed shapes flagged (CL-DQ-007); net-cost semantics after the change not confirmed against Google's documentation |
| GCP console "Cost table" CSV download | PARTIAL | PARTIAL | UNVERIFIED | Column names from memory, not a real file |
| GCP FOCUS (BigQuery view) | UNVERIFIED | UNVERIFIED | UNVERIFIED | |
| FOCUS 1.0–1.4 normative header sets | TESTED | TESTED | TESTED by period | Separate release review used four provider labels per version. All 20 synthetic ledgers totaled USD 28.73. Vendor-specific deliveries require separate evidence. |
| OCI FOCUS reports | UNVERIFIED | UNVERIFIED | UNVERIFIED | |

## Columns CloudLeak deliberately does not add into totals

| Provider | Columns | Why |
| --- | --- | --- |
| AWS | `line_item_net_unblended_cost`, `line_item_blended_cost`, `pricing_public_on_demand_cost`, `split_line_item_*` costs | CloudLeak sums unblended amounts across line types, including tax, credits and refunds. Split allocations are excluded from the billed sum. Contractual net discount treatment remains unverified. |
| Azure | `PaygCostInBillingCurrency`, `CostInUsd`, `CostInPricingCurrency` | Billing-currency cost is the invoiced amount; Azure cost exports carry no tax lines |
| GCP | `cost_at_list`, `price.*`, `currency_conversion_rate` | `cost` is already in billing currency; credits are added from `credits[]` |
| FOCUS | `ListCost`, `ContractedCost`, `PricingCurrency*` | `BilledCost` is the invoiced amount; `EffectiveCost` is kept separately as amortized cost |

## Failure behavior (tested)

| Input | Result | Test |
| --- | --- | --- |
| Ragged row, duplicate header, empty file, truncated gzip, 1000:1 decompression bomb | exit 2, line number where available, nothing written | `test_malformed_files_fail_closed`, `test_truncated_gzip_fails_closed`, `test_decompression_bomb_refused` |
| Unparseable amount (`1,250.00`, `$3`) | doctor FAIL; reconcile exits 2 for an affected invoice scope | `test_unparseable_costs_are_counted_not_zeroed`, `test_scoped_invalid_row_prevents_matching_subset` |
| Same file twice; a re-delivered month; a stale assembly or export run | doctor FAIL; on main, reconcile exits 2 for known duplicate or incomplete deliveries | `test_duplicate_file_fails`, `test_redelivered_export_fails`, `test_delivery_errors_block_invoice_output`, `test_d2_*` |
| A provider missing from one month | doctor WARN; variance reports an export gap, not "discontinued spend" | `test_d3_missing_provider_is_an_export_gap_not_discontinued_spend` |
| Unwritable output, missing path | exit 2 with the path, no traceback | `test_d4_unwritable_output_is_actionable` |
| Redaction: unclassified column; residual identifier | exit 2, output discarded | `test_unclassified_column_fails_closed`, `test_residual_gate_catches_leak` |

## Not yet tested

- Real customer exports from any provider (see top).
- 50 GB inputs. The [README performance results](../README.md) include a 10,063,750,122-byte synthetic CSV reconciliation and a separate 10-million-row benchmark.
- Windows paths with network shares; inputs on read-only network filesystems.
