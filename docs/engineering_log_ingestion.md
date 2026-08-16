# Engineering Log — Ingestion

**Status: complete.** Everything below was verified by running the pipeline, not
recalled. Numbers come from the committed outputs.

`architecture_note.md` records *why* each decision was made; this file records *what exists*, *what it emits*, and *what
changed during the build*. §4 is the input for the runbook's monitoring section.

---

## 1. What this covers

Capability, where it lives, and the test that proves it. Every behaviour described here is asserted by a test; nothing below is a claim.

Run `python run.py test -v` for the full list.

---

## 2. What exists

| File | Lines | Role |
|---|---|---|
| `common/observability.py` | 504 | `Telemetry` facade: spans, correlated logs, metrics. Real OTel SDK when installed, byte-identical shim when not. |
| `phase1_ingestion/ingest.py` | 622 | Bronze landing, checkpoint protocol, pause/resume, drift orchestration. **Entry point.** |
| `phase1_ingestion/drift.py` | 513 | Field inventory, fingerprint, diff, rename/enum detection, report rendering. **Pure — emits no logs.** |
| `phase1_ingestion/normalize.py` | 341 | Bronze → Silver: aliases, dedupe, lineage, coverage report. |
| `phase1_ingestion/supersede.py` | 237 | Supersession proposals. Advisory only; cannot modify Silver. |
| `tests/test_phase1.py` | 476 | 24 tests, stdlib `unittest`, temp dirs, zero installs. Includes the schema-contract check. |

**Configuration surface** (things a human tunes):

| Setting | Location | Value | Tuned against |
|---|---|---|---|
| `RENAME_CONFIDENCE_THRESHOLD` | `drift.py` | `0.5` | observed 0.75 for `clause_type`→`category` |
| `ENUM_CARDINALITY_LIMIT` | `drift.py` | `20` | — |
| `ENUM_MIN_REPETITION` | `drift.py` | `4` | `status` (2 values / 12 records) vs `clause_id` (12/12) |
| `VALUE_TRACKING_MAX_LENGTH` | `drift.py` | `120` | excludes `clause_text` from the registry |
| field aliases | `config/schema_aliases.json` | 2 confirmed | human-owned; pipeline never writes here |
| telemetry destination | `CI_LOG_DIR` env | `output/logs/` | — |
| OTLP export | `OTEL_EXPORTER_OTLP_ENDPOINT` env | unset | — |

---

## 3. Verified run results

From the committed outputs, produced by `python run.py phase1`:

| | |
|---|---|
| Batch 1 | 12 records, api_version 2.1, `run_id=clauses_batch_1__7f61df85` |
| Batch 2 | 8 records, api_version 2.3, `run_id=clauses_batch_2__6a33ab9b` |
| Drift events | **6** — 1 BREAKING, 1 NEEDS_HUMAN_CONFIRMATION, 1 ADDITIVE, 3 INFO |
| Bronze rows | **21** written, **20** unique (1 replay duplicate from the simulated crash) |
| Silver records | **20**, byte-identical to the upstream export's `clauses_ingested_fallback.json` |
| Proposals | 2 — `SUPERSESSION_CANDIDATE` (CLZ-0001 ← CLZ-0013), `AMENDS_UNKNOWN_CLAUSE` (CLZ-0017) |
| Telemetry | 12 spans, 43 log lines, 5 metric snapshots |
| Tests | 24 in the Phase 1 suite; 65 across both phases |

The six drift events, exactly: `API_VERSION_CHANGED metadata.api_version` ·
`FIELD_REMOVED modified_by` (BREAKING) · `SUSPECTED_RENAME clause_type → category`
(0.75) · `FIELD_ADDED review_history` (+7 nested) · `FIELD_REMOVED clause_type`
(INFO, explained by the rename) · `ENUM_VALUE_ADDED status` (`under_review`).

---

## 4. Observability reference — raw material for the Phase 3 runbook

### Spans

```
ingest.run
  └─ ingest.land_records      attrs: run.id, source.file, source.api_version,
  │                                  bronze.records_total/start_index/records_written
  └─ ingest.detect_drift      attrs: run.id, source.api_version
normalize.run
  ├─ normalize.read_bronze    attrs: bronze.records_read
  └─ normalize.map_to_silver  attrs: silver.records
supersede.run
```

### Log events

Alerts key off `event`, never the human message. Severity of `drift.*` is derived
from the drift event's own severity (`BREAKING`→ERROR, `NEEDS_HUMAN_CONFIRMATION`/
`WARN`→WARN, else INFO).

| Severity | Event | Meaning | Normal? |
|---|---|---|---|
| INFO | `run.start`, `run.complete` | stage lifecycle | yes |
| DEBUG | `record.committed` | one Bronze record durable | yes, one per record |
| INFO | `run.already_complete` | re-run of a finished batch | yes, idempotent |
| INFO | `drift.none` | shape unchanged | yes |
| INFO | `drift.api_version_changed`, `drift.enum_value_added` | additive/declared change | yes, note it |
| INFO | `drift.field_added` | new field landed, not yet in Silver | expected; needs a decision |
| WARN | `run.resumed` | picked up after a crash or pause | **investigate why it stopped** |
| WARN | `run.paused` / `run.paused_exit` | STOP file present | only if someone asked |
| WARN | `bronze.replay_duplicates` | crash in the commit window | expected after a crash; never otherwise |
| WARN | `normalize.duplicates_dropped` | the above, settled at Silver | should match the count above |
| WARN | `drift.suspected_rename` | **a human must confirm an alias** | **action required** |
| WARN | `normalize.unmapped_fields` | source field with no confirmed alias | **action required** |
| WARN | `supersede.supersession_candidate` / `…amends_unknown_clause` | clause lineage needs review | **action required** |
| WARN | `run.gated_on_drift` | landed, downstream gated (exit 2) | by design |
| WARN | `run.attempt_archived` | `--restart` set an attempt aside | only if someone asked |
| ERROR | `drift.field_removed` (BREAKING) | a field downstream relies on is gone | **page someone** |
| ERROR | `normalize.missing_clause_category` | Silver has rows with no category | **page someone** |
| ERROR | `normalize.clause_id_collision` | same id, different content | **page someone** |
| ERROR | `run.source_not_found`, `run.failed`, `normalize.no_bronze`, `supersede.failed` | stage failure | **page someone** |
| ERROR | `run.simulated_crash` | only from `--simulate-crash-after` | **should never appear in prod** |

### Metrics

| Metric | Type | Healthy |
|---|---|---|
| `ingest.records_landed_total` | counter | equals source record count |
| `ingest.runs_completed_total` | counter | 1 per source file |
| `ingest.runs_resumed_total` | counter | **0** in steady state |
| `ingest.runs_crashed_total` / `runs_failed_total` | counter | **0** |
| `ingest.runs_paused_total` / `runs_noop_total` | counter | 0 unless intended |
| `ingest.records_skipped_on_resume_total` | counter | 0 in steady state |
| `ingest.replay_duplicates_total` | counter | **0** unless a crash occurred |
| `ingest.record_commit_duration_ms` | histogram | p95 single-digit ms locally; a rising trend means the disk or fsync path |
| `drift.events_total` | counter | 0 for a stable source; any BREAKING is an alert |
| `normalize.records_total` | counter | equals unique Bronze rows |
| `normalize.duplicates_dropped_total` | counter | matches `ingest.replay_duplicates_total` |
| `normalize.unmapped_records_total` | counter | **0**; non-zero = unconfirmed alias |
| `normalize.clause_id_collisions_total` | counter | **0** |
| `supersede.proposals_total` | counter | currently 2; a rise means contract churn |

### Exit codes

| Code | Meaning | Orchestrator should |
|---|---|---|
| 0 | success | continue |
| 1 | unrecoverable error (or crash/pause) | alert; re-run to resume |
| 2 | **data landed**, drift needs human attention (`--fail-on-drift`) | hold the downstream stage, do **not** re-ingest |

### Rollback primitives (Phase 3 §3 will build on these)

- Bronze runs are immutable once `complete`; Silver is fully regenerable from Bronze.
- `run_id` is content-derived, so a run directory *is* a versioned snapshot.
- To roll back a bad normalization: revert `config/schema_aliases.json`, re-run `normalize.py`. No data movement.
- To roll back a bad batch: move its `run_id=…` directory aside and re-run `normalize.py`. Nothing is deleted.
- `--restart` archives (`*.superseded.<ts>`) rather than deleting.

---

## 5. Assumptions on record

1. Files stand in for API responses; a real client adds pagination, auth, retries.
2. `clause_id` is globally unique and stable.
3. One process per Bronze run — no lock, not detected.
4. Source timestamps are trustworthy and UTC.
5. `clauses_ingested_fallback.json` represents the intended Silver shape.
6. `data/` files were renamed from dashes to underscores to match the requirements; contents unmodified.

---

## 6. Decisions made *during* implementation

Not in the original plan — these came from running the thing and looking at the
output. Recording them because they are the ones most easily lost, and several are
the strongest material for Phase 3.

**C-1 — Drift detection was too noisy to be useful.** First run reported 13 events,
including new `clause_id` and `clause_text` values as "drift". Those are data, not
schema. Added `is_categorical()` (`ENUM_MIN_REPETITION = 4`) and
`VALUE_TRACKING_MAX_LENGTH = 120`. **Principle: a drift report that cries wolf gets
ignored, which is functionally the same as not having one.** Guarded by
`test_does_not_report_identifiers_as_enum_drift`.

**C-2 — A new nested object is one decision, not eight.** `review_history` produced
8 near-identical "action required" lines. Now the subtree root is reported once with
descendants as evidence. 13 events → 6.

**C-3 — The original crash demo never exercised the risk it documented.**
`--simulate-crash-after` raised *after* the checkpoint advanced — the clean case.
The at-least-once claim was therefore untested. Added `--crash-window`, which dies
between fsync and checkpoint. The committed manifest now genuinely shows
`duplicates_from_replay: 1`. **Principle: a durability guarantee you cannot
demonstrate is a guess.**

**C-4 — `metrics.json` was overwritten by whichever stage exited last.** Four
processes, one file. Changed to append-only `metrics.jsonl`, one snapshot per
process. Telemetry is now append-only across all three signals.

**C-5 — Errors escaped the logging system.** A missing `--source` file was reported
by a raw `print` to stderr, before `Telemetry` was constructed. Telemetry is now
built before argument validation. **No failure is reported through a channel the log
files cannot see.**

**C-6 — Telemetry lived in the wrong directory.** It was under
`phase1_ingestion/output/logs/`, which would have split traces across phases and
broken the ingestion→agent→human-override join. Moved to repo-root `output/logs/`
with a `CI_LOG_DIR` override. See AD-9. **Phase 2's `audit_log.json` deliberately
does *not* move here** — it is a legal record, not telemetry.

**C-7 — A cosmetic path call could crash the pipeline.** `Path.relative_to(ROOT)`
raised when output lived outside the repo (as in tests). Replaced with
`display_path()`. Trivial, but it was in the success path of every run.

**C-8 — Unclosed file handle.** The metrics writer leaked; caught by running the
suite under `-W error::ResourceWarning`.

---

## 7. Not yet done

| Deliverable | Status | Source material |
|---|---|---|
| `docs/runbook.md` — monitoring | **not started** | §4 above is the complete input: events, metrics, healthy values, exit codes |
| `docs/runbook.md` — agent failure | **not started** | Phase 1 half exists (resume semantics, `_checkpoint.json`, in-flight = uncommitted record); needs the Phase 2 agent half |
| `docs/runbook.md` — rollback | **partially drafted** | §4 "Rollback primitives" |
| `docs/buy_vs_build.md` | **not started** | The checkpoint protocol is the chosen component. Build now (30 lines, no broker, reviewer-runnable); buy later — Celery/RabbitMQ or a transactional sink. **Trigger to switch:** continuous rather than batched arrival, >1 worker per batch, or the checkpoint growing features (per-record retries, poison handling). That last one is the real tell — when you are rebuilding a queue by hand, go buy one. Also see AD-3 and AD-6 for the sunset conditions. |
| `README.md` — all-phase rewrite | Phase-1 scoped today | current README + this file |
| `docs/architecture_note.md` — **1 page max** | 10 ADRs, over length | must be **condensed** for release; keep the full version as an appendix |

**Watch the page limit.** The requirements caps the architecture note at one page and asks
for bullets over prose. The current ADR file is a working record, not the
deliverable. Phase 3 should ship a one-page summary that links here.

## 8. Deferred, and why

| Item | Why deferred | Would revisit when |
|---|---|---|
| Quarantine path for malformed records | no malformed data in the supplied batches | first parse failure in production |
| CI contract test against `silver_schema.json` | no CI in scope | repo gets CI |
| Retention / PII controls | out of scope | before the dataset leaves the platform |
| Clause supersession as a real Silver dimension | needs human confirmation first, by design | source exposes an authoritative link |
| Multi-writer locking | single-process assumption stated | parallel ingestion |
| Bronze retention | grows unbounded | disk pressure or a policy requirement |
