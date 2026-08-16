# Production Readiness — Design TODOs

What this pipeline needs to become a production service, with enough design that
the next engineer can pick up a section and start.

Everything here is **deliberately not built**. The requirements asked for a
proof-of-concept, and local files were the right choice for one (see
[`architecture_note.md`](architecture_note.md), AD-6). This is the map out.

**Read [§1 first](#1-what-we-are-hardcoded-to).** Most of the work below is
cheap; the schema coupling is what will actually slow someone down.

---

## 1. What we are hardcoded to

**Short answer: Bronze is schema-agnostic. Everything from Silver onward is
hardcoded to the contract-clause schema, across 16 files.**

Some of that is correct — a data contract *is* a deliberate commitment, and
`normalize.py` enumerating 13 fields is why adding a field requires a code change
and a test. Some of it is accidental coupling that should be centralised.

### Correctly coupled — leave alone

| Location | What | Why it should stay |
|---|---|---|
| `normalize.py` `STABLE_FIELDS`, `VERSION_CONDITIONAL_FIELDS` | the 13 Silver fields | changing the published contract *should* require a code change and a test |
| `silver_schema.json` | the contract, machine-readable | consumers bind to it |
| `models.py` `ClauseCategory` | the 7 review categories | it is the review taxonomy; changing it changes the rubric that applies |

### Accidentally coupled — should be centralised

| Location | Coupling | Fix |
|---|---|---|
| `ingest.py` | `document["clauses"]`, `document["metadata"]` | move to config: `source.records_path`, `source.metadata_path`. ~10 lines, makes ingestion source-agnostic |
| `rules.py` `CHECKS` | dict keys must match `firm_standards.json` section names, unenforced | validate at load; a typo currently means a rule silently never fires |
| `supersede.py` | `contract_id` + `section_ref` | fine for clauses, meaningless for any other entity. Accept as clause-specific and name it so |
| `classifier.py` `SOURCE_CATEGORY_MAP` | source taxonomy → review taxonomy | correct as code, but it should fail loudly on an unseen source category rather than falling through to the model |
| `silver_schema.json` vs `normalize.py` | **two sources of truth for the same 13 fields** | generate the schema from a pydantic `SilverClause` model. Today a drift between them is caught only by a test |
| Everywhere | `clause_id` as the universal key | genuinely universal here; keep, but name it `entity_id` if this is ever generalised |

**TODO-1** — Define `SilverClause` as a pydantic model; generate
`silver_schema.json` from it in CI. Removes the dual source of truth. *~2 hours.*

**TODO-2** — Move source record/metadata paths into config. *~1 hour.*

**TODO-3** — Validate `rules.CHECKS` keys against `firm_standards.json` at
startup; fail fast on a mismatch. *~30 minutes.* A rule that silently never fires
is the worst failure mode here — the report looks clean.

---

## 2. Queue — RabbitMQ + Celery

Today ingestion and review are single-process batch scripts. That is correct at
20 clauses per file and wrong the moment clauses arrive continuously.

### Topology

```
                    ┌────────────────────────────────────────────┐
  contract API ───► │ FastAPI receiver  (webhook or poller)      │
                    │  - validates envelope only, never content  │
                    │  - writes raw payload to S3 immediately    │
                    │  - publishes one message, then returns 202 │
                    └───────────────┬────────────────────────────┘
                                    │
              exchange: contract.ingest (direct, durable)
                                    │
                    ┌───────────────▼───────────────┐
                    │ queue: ingest.batch           │──► DLQ: ingest.batch.dlq
                    │ one message per source file   │
                    │ routing key = run_id          │
                    └───────────────┬───────────────┘
                                    │ celery worker: ingest
                                    │ (lands Bronze, runs drift, emits per-clause)
              exchange: contract.agents (direct, durable)
                                    │
                    ┌───────────────▼───────────────┐
                    │ queue: agents.clause          │──► DLQ: agents.clause.dlq
                    │ one message per clause        │
                    │ priority: high-severity first │
                    └───────────────────────────────┘
```

### Task granularity — and why it differs per stage

| Stage | Granularity | Why |
|---|---|---|
| Ingest | **one task per source file** | Bronze append order and checkpoint semantics assume a single writer per run. Parallelising within a file buys nothing and breaks that. |
| Agents | **one task per clause** | Clauses are independent. Per-clause retry means one bad clause does not re-run 19 good ones. |

Route ingest tasks by `run_id` so one worker owns a run
(`task_routes` + a consistent-hash exchange, or `queue=f"ingest.{shard}"`).

### Celery configuration that actually matters

```python
task_acks_late = True              # ack AFTER work, so a killed worker redelivers
worker_prefetch_multiplier = 1     # no hoarding; long tasks would starve peers
task_reject_on_worker_lost = True
task_time_limit = 300              # hard kill
task_soft_time_limit = 240         # raises SoftTimeLimitExceeded -> checkpoint first
broker_transport_options = {"visibility_timeout": 600}   # MUST exceed task_time_limit

task_autoretry_for = (ProviderRateLimited, ProviderUnavailable)
task_retry_backoff = True
task_retry_backoff_max = 60
task_retry_jitter = True           # without jitter, N workers retry in lockstep
task_max_retries = 3
```

**Do not** autoretry parse failures at the transport layer. Repeating an identical
request that produced malformed output produces malformed output again. That case
is already handled inside the agent, which rewords the request once and then
escalates.

### Idempotency

Redelivery is guaranteed, not exceptional. Every task must be safe to run twice.

- **Idempotency key:** `sha256(clause_id + record_hash + prompt_version + firm_standards_version)`.
- Worker checks the audit ledger for that key before doing work; if present, ack and return.
- This is the same dedupe discipline as Phase 1's `record_hash`, moved up a layer.
- **The key must include the prompt and standards versions** — the same clause under a new policy is legitimately new work, and omitting them would silently skip re-reviews after a policy change.

**TODO-4** — Add `idempotency_key` to `AuditRecord` and a unique index on it.
*~2 hours.* Do this **before** any queue work; it is the precondition.

### Dead-letter policy

```
x-dead-letter-exchange: contract.dlx
x-dead-letter-routing-key: <queue>.dlq
x-message-ttl: 86400000        # 24h in the DLQ, then alert
```

A message reaching the DLQ is a **human event**, not an automatic retry. Draining
the DLQ requires an explicit operator action — the same discipline as
`--acknowledge-halt` today. Auto-draining a DLQ is how you replay a poison
message forever.

**TODO-5** — DLQ consumer that writes to `escalations` rather than reprocessing.
*~4 hours.*

### ⚠ The part that will bite: all three guardrails are process-local

This is the most important item in this document. Every control we built assumes
one process. **Scale out without fixing these and all three silently stop
working.**

| Control | Today | Failure at N workers | Fix |
|---|---|---|---|
| `BudgetGuard` | in-process counter | **N × the budget is spent.** 10 workers × $1 cap = $10 | Redis atomic counter with reserve-then-settle: `INCRBY` an estimate before the call, adjust to actual after. Check-then-spend across processes is a race. |
| `KillSwitch` | local `STOP` file | only stops the worker that can see the file | shared flag (Redis key or feature flag), checked before every task |
| `InMemoryRateLimiter` | per-process token bucket | **10 workers = 10× the provider's rate limit**, then 429s and a retry storm | distributed token bucket in Redis (`CL.THROTTLE`, or a Lua script) |
| `DecisionCache` | local JSON file | each worker keeps its own; hit rate collapses | Redis or the database, keyed identically |

**TODO-6** — Distributed budget guard with reservation semantics. *~1 day.* **Highest risk item here** — the failure is financial and silent.
**TODO-7** — Shared kill switch. *~2 hours.*
**TODO-8** — Distributed rate limiter. *~4 hours.*

### What we would delete

Once a real queue is in place, `_checkpoint.json` and the resume logic in
`ingest.py` and `run.py` are **deleted, not extended**. The broker's ack
semantics do that job properly. Keeping both is how you end up with two
disagreeing sources of truth.

---

## 3. Database — PostgreSQL

Replaces: checkpoints, the schema registry, the audit ledger, the Gold register.
Bronze payloads move to object storage (§4) with only metadata in the database.

```sql
-- ─── BRONZE ────────────────────────────────────────────────────────────────
CREATE TABLE bronze_runs (
    run_id              text PRIMARY KEY,
    source_file         text        NOT NULL,
    source_sha256       text        NOT NULL,
    source_api_version  text        NOT NULL,
    schema_fingerprint  text        NOT NULL,
    object_uri          text        NOT NULL,   -- s3://.../part-0000.jsonl
    records_in_source   integer     NOT NULL,
    status              text        NOT NULL CHECK (status IN ('in_progress','paused','complete','quarantined')),
    ingested_at         timestamptz NOT NULL,
    completed_at        timestamptz
);

CREATE TABLE bronze_records (
    -- The dedupe key becomes a PRIMARY KEY, so at-least-once delivery is
    -- resolved by the database on insert. ON CONFLICT DO NOTHING replaces
    -- the entire hand-rolled dedupe path.
    record_hash   text PRIMARY KEY,
    run_id        text        NOT NULL REFERENCES bronze_runs(run_id),
    record_index  integer     NOT NULL,
    payload       jsonb       NOT NULL,          -- verbatim; never transformed
    ingested_at   timestamptz NOT NULL
);
CREATE INDEX ON bronze_records (run_id, record_index);

-- ─── SCHEMA REGISTRY ───────────────────────────────────────────────────────
CREATE TABLE schema_versions (
    source       text        NOT NULL,
    api_version  text        NOT NULL,
    fingerprint  text        NOT NULL,
    inventory    jsonb       NOT NULL,
    first_seen   timestamptz NOT NULL,
    last_seen    timestamptz NOT NULL,
    PRIMARY KEY (source, api_version)
);

CREATE TABLE drift_events (
    id          bigserial PRIMARY KEY,
    run_id      text NOT NULL REFERENCES bronze_runs(run_id),
    kind        text NOT NULL,
    severity    text NOT NULL,
    field_path  text NOT NULL,
    detail      text NOT NULL,
    confidence  numeric(4,3),
    resolved_by text,                            -- who confirmed a rename
    resolved_at timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON drift_events (severity, resolved_at) WHERE resolved_at IS NULL;

-- ─── SILVER ────────────────────────────────────────────────────────────────
-- SCD2, which finally models the supersession Phase 1 can only propose.
CREATE TABLE silver_clauses (
    clause_id        text        NOT NULL,
    valid_from       timestamptz NOT NULL,
    valid_to         timestamptz,               -- NULL = current
    superseded_by    text,
    contract_id      text        NOT NULL,
    client_name      text        NOT NULL,
    project_name     text        NOT NULL,
    clause_category  text,
    clause_text      text        NOT NULL,
    section_ref      text        NOT NULL,
    effective_date   date,
    expiration_date  date,
    status           text        NOT NULL,
    last_modified    timestamptz,
    modified_by      text,                       -- NULL for v2.3+, documented
    review_history   jsonb,                      -- NULL for v2.1, documented
    unmapped_fields  text[]      NOT NULL DEFAULT '{}',
    lineage          jsonb       NOT NULL,
    PRIMARY KEY (clause_id, valid_from)
);
CREATE UNIQUE INDEX silver_clauses_current
    ON silver_clauses (clause_id) WHERE valid_to IS NULL;

-- ─── AUDIT (append-only, enforced by the DATABASE) ─────────────────────────
CREATE TABLE audit_records (
    id                     bigserial,
    idempotency_key        text        NOT NULL,
    run_id                 text        NOT NULL,
    clause_id              text        NOT NULL,
    agent                  text        NOT NULL,
    action                 text        NOT NULL,
    model_output           jsonb,
    human_decision         jsonb,
    effective_value        jsonb,
    decided_by             text        NOT NULL CHECK (decided_by IN ('rule','model','human','cache')),
    status                 text        NOT NULL,
    prompt_version         text,
    model_name             text,
    firm_standards_version text,
    usage                  jsonb,
    latency_ms             numeric,
    trace_id               text,
    span_id                text,
    lineage                jsonb,
    created_at             timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (id, created_at)
) PARTITION BY RANGE (created_at);

CREATE UNIQUE INDEX ON audit_records (idempotency_key, created_at);
CREATE INDEX ON audit_records (clause_id, created_at DESC);
CREATE INDEX ON audit_records (trace_id);
CREATE INDEX ON audit_records (decided_by, created_at DESC);

-- Append-only stops being a convention and becomes a permission.
-- This is the single most valuable line in this file.
REVOKE UPDATE, DELETE, TRUNCATE ON audit_records FROM app_role;

-- ─── HUMAN-IN-THE-LOOP ─────────────────────────────────────────────────────
CREATE TABLE human_overrides (
    override_id text PRIMARY KEY,
    clause_id   text NOT NULL,
    agent       text NOT NULL,
    reviewer    text NOT NULL,
    field       text NOT NULL,
    model_value jsonb,
    human_value jsonb,
    rationale   text NOT NULL,
    source      text NOT NULL CHECK (source IN ('review_history','review_ui','simulated')),
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON human_overrides (field, created_at DESC);   -- the learning signal

CREATE TABLE learning_proposals (
    proposal_id  text PRIMARY KEY,
    kind         text NOT NULL,
    status       text NOT NULL DEFAULT 'PENDING_HUMAN_APPROVAL'
                 CHECK (status IN ('PENDING_HUMAN_APPROVAL','APPROVED_AND_APPLIED','REJECTED')),
    target_file  text NOT NULL,
    current_value jsonb, proposed_value jsonb,
    evidence     jsonb NOT NULL,
    approved_by  text, approved_at timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now(),
    -- Approval REQUIRES a named human. The non-negotiable boundary, in the schema.
    CONSTRAINT approval_needs_a_person
        CHECK (status <> 'APPROVED_AND_APPLIED' OR (approved_by IS NOT NULL AND approved_at IS NOT NULL))
);

CREATE TABLE escalations (
    escalation_id text PRIMARY KEY,
    clause_id     text NOT NULL,
    agent         text NOT NULL,
    reason        text NOT NULL,
    detail        text NOT NULL,
    status        text NOT NULL DEFAULT 'OPEN',
    assigned_to   text, resolved_by text, resolved_at timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON escalations (status, created_at) WHERE status = 'OPEN';  -- queue depth
```

**Two design points worth defending in review:**

1. **`REVOKE UPDATE, DELETE ON audit_records`.** Today append-only is a property of
   how we write the file. In Postgres it becomes a permission the application
   cannot bypass even with a bug. That is the difference between a convention and
   a control.
2. **`approval_needs_a_person`.** The human-in-the-loop requirement becomes a
   `CHECK` constraint. A proposal cannot reach `APPROVED_AND_APPLIED` without a
   named approver and a timestamp — enforced by the database, not by us
   remembering.

**TODO-9** — Schema + Alembic migrations. *~1 day.*
**TODO-10** — Repository layer behind the current file I/O, so the swap is one implementation. *~2 days.*
**TODO-11** — Monthly partition maintenance (`pg_partman`) on `audit_records`. *~2 hours.*

---

## 4. Object storage — S3

### Layout

```
s3://contract-intelligence-prod/
  bronze/ingest_date=2026-08-16/run_id=<name>__<sha8>/
      _source_snapshot.json        raw bytes as delivered
      part-0000.jsonl              enveloped records
      _manifest.json
  audit/year=2026/month=08/day=16/audit-<run_id>.jsonl.gz
  telemetry/year=.../month=.../    traces + logs, after the collector
  exports/gold/run_id=.../clause_risk_register.json
```

Partition prefixes match today's local layout on purpose, so migration is a
`sync` rather than a rewrite.

### Bucket policy

| Prefix | Storage | Lock | Retention | Why |
|---|---|---|---|---|
| `bronze/` | Standard → IA @90d → Glacier @1y | **Object Lock, compliance mode** | **7 years** | system of record; a contract dispute can surface years later |
| `audit/` | Standard → IA @90d | **Object Lock, compliance mode** | **7 years** | legal evidence. Must survive a mistaken `DELETE` and a malicious one |
| `telemetry/` | Standard | none | **30 days** | operational, samplable, expirable |
| `exports/` | Standard | none | 90 days | regenerable from Bronze |

- SSE-KMS everywhere; a separate CMK for `audit/` with a tighter key policy.
- Versioning on. `bronze/` and `audit/` deny `s3:DeleteObject` at the bucket policy for every role including admin.
- **Never lifecycle `audit/` into Glacier Deep Archive** — 12-hour restore during a compliance request is not acceptable.

**This is exactly the distinction from AD-9.** Telemetry expires in 30 days;
audit is locked for 7 years. Merging them into one log stream means a retention
policy can quietly delete evidence, which is why the audit ledger is a
deliverable and not telemetry.

### Log shipping

```
app ──OTLP──► OpenTelemetry Collector ──┬──► Tempo / Jaeger      (traces, 30d)
                                        ├──► Prometheus          (metrics, 90d)
                                        ├──► Loki                (logs, 30d)
                                        └──► S3 telemetry/       (raw, 30d)

audit ledger ──► Postgres (queryable) ──nightly──► S3 audit/ (locked, 7y)
```

The application already emits OTLP-shaped spans and honours
`OTEL_EXPORTER_OTLP_ENDPOINT`, so **this is a deployment change, not a code
change** — set one environment variable and the shim is replaced by the real SDK
(AD-8).

**TODO-12** — Collector deployment + S3 sink. *~1 day.*
**TODO-13** — Bronze/audit writes to S3 behind the repository layer. *~1 day.*
**TODO-14** — Object Lock and bucket policies, reviewed by whoever owns compliance. *~4 hours.* Do this **before** the first production write; retroactively locking objects is not possible.

---

## 5. Also required before production

| # | Item | Effort | Why it blocks |
|---|---|---|---|
| **TODO-15** | **PII handling.** Client names, project names and reviewer names are cleartext. Classify fields, encrypt or tokenise, add access control | 3 days | our own data contract §3.5 says this must not leave the platform. It currently could |
| **TODO-16** | Quarantine path for malformed records (`bronze/_quarantine/` + reason) | 4 hours | today a single bad record fails the whole run |
| **TODO-17** | Secret management for `ANTHROPIC_API_KEY` (Vault / Secrets Manager, not env) | 2 hours | — |
| **TODO-18** | CI: tests + eval + schema validation on every PR, failing the build on regression | 4 hours | the eval harness only has value if something runs it |
| **TODO-19** | Expand ground truth to a few hundred counsel-reviewed labels, versioned alongside `firm_standards.json` | ongoing | 20 labels from one annotator is a smoke test, not a measurement |
| **TODO-20** | Alert rules wired from the runbook's routing table into the paging tool | 4 hours | routing documented but not configured |
| **TODO-21** | Backfill/replay command: re-review a date range under a new standards version | 1 day | after every approved proposal, someone will ask "what changes retroactively?" |

---

## 6. Suggested order

Dependencies matter more than effort here.

| Order | Work | Why this position |
|---|---|---|
| 1 | TODO-4 idempotency key · TODO-1 generated schema · TODO-3 rule/standard validation | preconditions. Cheap now, expensive to retrofit |
| 2 | TODO-9/10 database + repository layer | everything else assumes a shared store |
| 3 | TODO-13/14 object storage **with Object Lock** | must precede the first production write |
| 4 | TODO-6/7/8 **distributed guardrails** | **must land before any horizontal scaling.** Ship workers first and the budget silently multiplies |
| 5 | TODO-5 queue + DLQ + workers | now safe to scale out |
| 6 | TODO-15 PII · TODO-17 secrets · TODO-20 alerts | before real client data |
| 7 | TODO-12 collector · TODO-18 CI · TODO-21 replay | operability |
| 8 | TODO-19 ground truth | continuous |

**The trap to avoid:** step 5 before step 4. Queue and workers are the visible,
satisfying work; the distributed guardrails are invisible until the month the
model bill is ten times what the budget said it would be.

## 7. What must not change

Whatever else moves, these are load-bearing:

- **Bronze is never mutated.** Immutable once complete, whatever the storage.
- **The audit ledger is append-only** — and in Postgres it becomes a permission rather than a promise.
- **`config/` stays human-owned.** No worker, task or agent may write to it. The test that asserts this must survive every refactor.
- **An override changes one clause; only an approved proposal changes the rule.**
- **Guardrails must never silently skip work.** Not processed is a status that gets written, never an absence.
- **Telemetry and the audit ledger stay separate stores** with separate retention.
