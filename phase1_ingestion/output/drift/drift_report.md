# Schema Drift Report

- **Run:** `clauses_batch_2__6a33ab9b`
- **Source:** contract_management_api (api_version **2.3**)
- **Compared against:** 2.1
- **Records in batch:** 8
- **Schema fingerprint:** `sha256:5cb9576eb28d466e2626e59ad3f40572e177da17517f49904fffaff2230105f3`
- **Generated:** 2026-08-16T16:52:39.698Z

**6 change(s) detected:** 3 INFO, 1 ADDITIVE, 1 NEEDS_HUMAN_CONFIRMATION, 1 BREAKING

| Severity | Change | Field | Detail |
|---|---|---|---|
| `INFO` | API_VERSION_CHANGED | `metadata.api_version` | Source declared api_version 2.1 -> 2.3. |
| `BREAKING` | FIELD_REMOVED | `modified_by` | Present in 12 records of the previous schema, absent from every record of this one. Downstream consumers that require this field will break; the value is not recoverable. |
| `NEEDS_HUMAN_CONFIRMATION` | SUSPECTED_RENAME | `clause_type -> category` | Field 'clause_type' disappeared and 'category' appeared carrying 75% of the same values. This looks like a rename. |
| `ADDITIVE` | FIELD_ADDED | `review_history` | New field of type object, present in 8 records. It carries 7 nested field(s). Landed in Bronze; not yet exposed in Silver. |
| `INFO` | FIELD_REMOVED | `clause_type` | Absent from the new batch, but explained by suspected rename to 'category'. |
| `INFO` | ENUM_VALUE_ADDED | `status` | New value(s) not seen in the previous schema: under_review. |

## Action required

- **FIELD_REMOVED** on `modified_by` — Confirm the field is genuinely retired upstream, then mark it nullable in data_contract.md and notify consumers.
- **SUSPECTED_RENAME** on `clause_type -> category` _(confidence 0.75)_ — A human must confirm by adding "category": "<target_field>" to phase1_ingestion/config/schema_aliases.json. Until then normalize.py emits null for the target field and lists 'category' in _unmapped_fields.
- **FIELD_ADDED** on `review_history` — Decide whether Silver should surface this field, then extend the data contract.

## Guarantee

All records in this batch were written to Bronze unchanged, regardless of the drift reported above. Drift detection never filters, rewrites, or rejects data.
