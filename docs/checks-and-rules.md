# Doctor check catalog

Generated from `cloudleak checks`. IDs are stable and never reused, so CI can gate on them.

| ID | Category | Check | What it means |
| --- | --- | --- | --- |
| `CL-ING-001` | ingestion | Billing format recognized | Each file is identified as AWS CUR (legacy or 2.0), Azure cost export, GCP billing export or FOCUS. |
| `CL-ING-002` | ingestion | No file supplied twice | Byte-identical files (same SHA-256) would double count every row. |
| `CL-ING-003` | ingestion | Manifest completeness | Every data file a provider manifest lists is present, and no unlisted file sits beside it. |
| `CL-ING-004` | ingestion | Manifest row counts | Row counts declared by an Azure export manifest match the rows read. |
| `CL-ING-005` | ingestion | Cost values parse exactly | Every non-blank cost value is a plain decimal. Unparseable values are counted, never zeroed. |
| `CL-ING-006` | ingestion | Dates and billing periods parse | Every row has a usage date and a billing period CloudLeak can place in UTC. |
| `CL-ING-007` | ingestion | Decimal precision preserved | Cost values with more than 12 decimal places are rounded at the 12th place. |
| `CL-ING-008` | ingestion | Unambiguous column names | No two columns normalize to the same name. |
| `CL-ACC-001` | accounting | Single currency per total | Totals are never summed across currencies; rows without a currency are flagged. |
| `CL-ACC-002` | accounting | Billing period finality | Whether each period is finalized (invoice issued) or a preliminary estimate. |
| `CL-ACC-003` | accounting | Full-period coverage | Whether each billing period contains usage for every day of the month. |
| `CL-ACC-004` | accounting | Cost basis not mixed | Azure actual and amortized exports for the same scope are reported separately, never summed. |
| `CL-ACC-005` | accounting | Charge composition | Tax, credits, refunds, discounts, fees and purchases are separated from usage. |
| `CL-ACC-006` | accounting | No overlapping deliveries | Identical line items in two different files indicate overlapping exports and double counting. |
| `CL-ACC-007` | accounting | Billed versus effective cost | Difference between billed and amortized (effective) cost, where both are present. |
| `CL-ACC-008` | accounting | Every export covers every period | A provider or billing account with no rows in a period that other exports cover. Comparisons across that period would mistake a missing file for a change in spend. |
| `CL-DQ-001` | data_quality | Resource identifiers present | Share of usage cost carrying a resource ID; exports without resource IDs cannot support resource-level findings. |
| `CL-DQ-002` | data_quality | Tag and label coverage | Share of usage cost carrying any tag or label. |
| `CL-DQ-003` | data_quality | Consistent tag keys | Tag keys that differ only by case or prefix (env, ENV, Environment) split ownership reports. |
| `CL-DQ-004` | data_quality | Identical rows within a file | Rows identical in every column. Kept, because Azure documents them as legitimate in amortized exports; de-duplicating would understate spend. |
| `CL-DQ-005` | data_quality | Split cost allocation rows | AWS split cost allocation columns are present; split costs must not be added to parent costs. |
| `CL-DQ-006` | data_quality | Late-arriving usage | GCP usage billed to a later invoice month than the month it was used in. |
| `CL-DQ-007` | data_quality | Consistent GCP commitment schema | GCP exports with and without consumption_model columns for one billing account. |
| `CL-DQ-008` | data_quality | Negative usage rows | Negative-cost usage rows (corrections) reduce totals; they are kept and counted. |
| `CL-DQ-009` | data_quality | Recognized charge types | Charge or line-item types CloudLeak does not recognize are listed so they can be mapped. |
| `CL-FOC-001` | focus | FOCUS version | The FOCUS version inferred from the columns present (approximate, not certification). |
| `CL-FOC-002` | focus | FOCUS columns CloudLeak relies on | Key FOCUS columns that are absent, and what analysis each enables. |
| `CL-FOC-003` | focus | FOCUS columns present but empty | Columns a provider includes but never populates. Cross-cloud SQL silently returns nothing for them. |
| `CL-FOC-004` | focus | FOCUS ChargeCategory values | ChargeCategory values outside Usage, Purchase, Tax, Credit and Adjustment. |
| `CL-FOC-005` | focus | FOCUS corrections | Rows with ChargeClass = Correction restate earlier periods. |

Statuses: **PASS** the property holds · **INFO** a fact worth knowing · **WARN** usable with care ·
**FAIL** totals cannot be trusted until fixed · **SKIP** not applicable. `--fail-on` picks the exit-1 threshold.

# Billing-signature rules (Level 2)

| ID | Rule | Providers | Why the billing line is evidence |
| --- | --- | --- | --- |
| `CL-SIG-001` | Idle public IPv4 address charges | aws | AWS bills idle addresses under an explicit IdleAddress usage type; the charge itself records that the address was allocated but not associated during the billed hours. |
| `CL-SIG-002` | NAT gateway data-processing charges | aws, azure, gcp, focus | Per-GB NAT processing is billed on every byte routed through the gateway, including traffic to the provider's own storage and database services that could use private endpoints instead. |
| `CL-SIG-003` | Regional (inter-zone) data transfer charges | aws, gcp | AWS bills DataTransfer-Regional-Bytes for traffic between availability zones (in each direction) and for same-region traffic over public or Elastic IPs; GCP bills inter-zone egress. Chatty services split across zones generate this cost silently. |
| `CL-SIG-004` | Log ingestion charges | aws, azure, gcp, focus | Log ingestion is billed per GB written, independent of whether anyone queries the logs. |
| `CL-SIG-005` | Snapshot storage charges | aws, azure, gcp, focus | Snapshot storage accumulates silently; the bill shows how much is retained, not whether it is needed. |
| `CL-SIG-006` | Unused commitment charges | aws, azure, gcp, focus | The bill explicitly attributes this amount to commitment capacity that no usage consumed. |
| `CL-SIG-007` | EBS gp2 volume charges (storage-tier review) | aws | The usage type records gp2 volumes. gp3 has a lower list price per GB with independently provisioned IOPS and throughput, so gp2 volumes are worth a tier review. |
| `CL-SIG-008` | Extended support surcharges | aws | AWS bills an explicit surcharge for running engine or Kubernetes versions past standard support. |
