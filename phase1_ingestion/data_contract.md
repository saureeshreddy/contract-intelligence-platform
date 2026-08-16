# Data Contract — Contract Clause Silver Layer

**Contract version:** 1.0.0
**Producer:** `phase1_ingestion/` (ingest → drift → normalize)
**Dataset:** `phase1_ingestion/output/silver/clauses.jsonl` (one JSON object per line)
**Machine-readable schema:** `phase1_ingestion/silver_schema.json`
**Audience:** anything downstream of Phase 1 — starting with the Phase 2 agents.

This is the interface. Bronze is an implementation detail of the producer; do not
read it directly except for audit and replay.

---

## 1. What fields to expect

### Stable core — present in every API version seen so far, never null

| Field | Type | Notes |
|---|---|---|
| `clause_id` | string | `CLZ-YYYY-NNNN`. Unique within the dataset. **Primary key.** |
| `contract_id` | string | `CTR-NNNN`. Many clauses per contract. |
| `client_name` | string | Free text. Not normalized against a client master. |
| `project_name` | string | Free text. |
| `clause_text` | string | The clause body. Verbatim from source; no cleaning, truncation, or encoding changes. |
| `section_ref` | string | e.g. `Section 8.1`. May carry an `(Amended)` marker — see §3. |
| `effective_date` | string | `YYYY-MM-DD`. May be in the future. |
| `expiration_date` | string | `YYYY-MM-DD`. |
| `status` | string | Observed: `active`, `draft`, `under_review`. **Open set** — see §3. |
| `last_modified` | string | RFC3339 UTC. Source system's timestamp, not ours. |

### Unified — one field, two upstream names

| Field | Type | Notes |
|---|---|---|
| `clause_category` | string \| null | From `clause_type` (API v2.1) or `category` (v2.3), via the human-confirmed alias map in `config/schema_aliases.json`. Observed values: `indemnification`, `limitation_of_liability`, `insurance`, `payment_terms`, `termination`, `scope_of_work`, `consequential_damages`, `security_clearance`. **Open set.** |

`null` here means a source field arrived with no confirmed alias. It is always
accompanied by an entry in `_unmapped_fields`. It never means "uncategorized".

### Version-conditional — null when the source version did not carry the field

| Field | Type | Present in | Notes |
|---|---|---|---|
| `modified_by` | string \| null | v2.1 only | Team that last edited the clause. **Removed in v2.3 and not recoverable.** |
| `review_history` | object \| null | v2.3 only | `{reviews: [{reviewer, date, action, notes}], review_count: int, last_review_date: string\|null}`. `reviews` may be empty with `review_count: 0`. |

> **`null` in these two fields means "the source API version did not send this
> field", not "the value was empty".** Do not treat `review_history: null` as
> "never reviewed" — for a v2.1 record we simply have no idea.

### Producer-added, prefixed with `_`

| Field | Type | Notes |
|---|---|---|
| `_unmapped_fields` | string[] | Source fields present in Bronze with no confirmed alias. Usually `[]`. Non-empty means a schema change is awaiting human confirmation. |
| `_lineage` | object | `{run_id, source_file, source_api_version, record_index, record_hash, ingested_at}` |

Underscore-prefixed fields are metadata, not contract data. New ones may be added
in a minor version; consumers should ignore unknown `_` fields.

### Current dataset

20 clauses, 6 contracts, from `clauses_batch_1.json` (v2.1, 12 records) and
`clauses_batch_2.json` (v2.3, 8 records). `modified_by` is null for 8 records;
`review_history` is null for 12.

---

## 2. Guarantees

1. **Raw preservation.** Every Silver record traces to a Bronze record whose
   `payload` is byte-equivalent to the source object. The source file's original
   bytes are also stored at `_source_snapshot.json`, hash-verified in the manifest.
2. **Uniqueness.** `clause_id` is unique in Silver. If a collision ever occurs it is
   logged as `normalize.clause_id_collision` at ERROR — it is never resolved silently.
3. **Deduplication.** Replay duplicates from crash recovery are removed on
   `_bronze.record_hash`. Bronze is at-least-once; **Silver is exactly-once.**
4. **Traceability.** Every value is attributable to a `(run_id, record_index,
   record_hash)` triple, and every run to a source file SHA-256.
5. **Immutability.** A Bronze run marked `complete` is never rewritten. Silver is
   fully regenerable from Bronze — it is a derived view, not a system of record.
6. **Drift is always reported before Silver changes.** A schema change produces
   `output/drift/drift_report.{json,md}` on the run that landed it.
7. **No silent field mapping.** A renamed field is only unified after a human
   records it in `config/schema_aliases.json`. Nothing in the pipeline writes to
   that file; a test enforces it.
8. **Visible degradation.** When something is not understood, the field is null
   **and** named in `_unmapped_fields`. There is no code path that quietly guesses.

### Changes that will bump this contract

- **Major** — removing a field, changing a type, changing `clause_id` semantics.
- **Minor** — adding a field, adding a value to an open set, adding an `_` field.

---

## 3. Known limitations

**Read this section before building on the dataset.**

1. **`modified_by` is gone from v2.3 onward and is not recoverable.** It is null for
   every record after the API upgrade. Any consumer treating it as required will
   break. This is upstream data loss, faithfully represented.

2. **`status` and `clause_category` are open sets.** v2.3 introduced `under_review`
   with no notice. Handle unknown values; do not switch exhaustively on them. Note
   that `consequential_damages` and `security_clearance` do not map onto the seven
   categories the Phase 2 classifier uses — they belong in `other`.

3. **Clause supersession is not modelled.** `CLZ-2025-0013` ("Section 8.1 (Amended)")
   replaces `CLZ-2025-0001` ("Section 8.1") on contract CTR-4401 — the original had
   broad indemnification, the amendment narrows it to a negligence standard. Both
   carry `status: active` and there is no link between them.
   **A consumer that reviews every active clause will review a clause that is no
   longer in force.** Candidates are proposed in
   `output/proposals/supersession_candidates.json` for human review; nothing
   automatically acts on them.

4. **We hold at least one amendment whose original we never received.**
   `CLZ-2025-0017` is marked "Section 2.1 (Amended)" for CTR-4438, and no prior
   version of that section exists in our data. Our view of some contracts is
   incomplete.

5. **No PII handling.** `client_name`, `project_name` and `review_history.reviews[].reviewer`
   contain real-world names in cleartext. There is no redaction, masking, or
   access control. Do not export this dataset outside the platform boundary.

6. **Field-level drift detection only.** We detect fields appearing, disappearing,
   changing type or nullability, and new values in categorical fields. We cannot
   detect *semantic* drift — the same field, same type, new meaning. Only a human
   reading clause text will catch that.

7. **Rename detection is a heuristic.** It matches on value-set overlap (≥0.5
   Jaccard). It can miss a rename between fields with disjoint values, and it will
   not propose one for high-cardinality or long-text fields. A miss is safe — the
   field lands in `_unmapped_fields` rather than being silently mismapped.

8. **Enum-drift detection needs repetition.** A field is treated as categorical only
   if its values repeat ~4× on average. On very small batches a genuine enum may be
   missed. Tuned against the supplied batches; see `drift.ENUM_MIN_REPETITION`.

9. **Single-writer assumption.** One process per Bronze run. Concurrent writers to
   the same `run_id` are not supported and not detected — there is no lock.

10. **No schema *migration*, only detection.** When a field is added, it lands in
    Bronze but does not appear in Silver until someone extends this contract and
    `normalize.py`. `review_history` is the exception: it is already published.

11. **Local filesystem only.** No object store, no catalog, no retention policy.
    Bronze grows without bound.

12. **Ordering.** Silver is sorted by `clause_id` for stable diffs. This is a
    convenience, not a guarantee — do not depend on file order.

---

## 4. Regenerating this dataset

```bash
python phase1_ingestion/ingest.py --source data/clauses_batch_1.json
python phase1_ingestion/ingest.py --source data/clauses_batch_2.json
python phase1_ingestion/normalize.py
```

Silver is rebuilt from scratch from all completed Bronze runs on every
`normalize.py` invocation. **Never hand-edit Silver** — the next run overwrites it.
Corrections belong upstream, or in the alias config.
