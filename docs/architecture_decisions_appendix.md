# Architecture Decisions — Full Appendix

> **This is the working record, not the submitted deliverable.** The requirements caps
> the architecture note at one page: see [`architecture_note.md`](architecture_note.md).
> This file holds the full reasoning behind each decision, written as it was made.


> Written as each decision was made, not reconstructed afterwards.
>
> For *what exists* — requirement traceability, telemetry reference and the
> corrections made during implementation — see
> [`engineering_log_ingestion.md`](engineering_log_ingestion.md) and
> [`engineering_log_agents.md`](engineering_log_agents.md).

## Priorities, in order

1. **Don't lose data.** 2. **Make change visible.** 3. **Be resumable.** 4. **Be legible.**

Every tradeoff below resolves in that order. Where two options were otherwise
close, the one that fails more loudly won.

## The through-line: the machine proposes, a human disposes

Phase 2 makes this a non-negotiable requirement for agent learning. Phase 1 applies the
same rule one layer earlier, to *schema meaning*, and it shows up three times:

| Where | Machine does | Human does |
|---|---|---|
| Field rename (`clause_type` → `category`) | detects it, scores confidence 0.75, writes a proposal | confirms it in `config/schema_aliases.json` |
| Clause supersession (`CLZ-0013` replaces `CLZ-0001`) | proposes candidates with evidence | confirms upstream |
| Config itself | reads it | owns it — nothing in the pipeline writes to `config/`, and a test enforces it |

The mechanism is structural, not procedural: **the machine writes to `output/`,
humans own `config/`.** That is why the Phase 2 guarantee will be demonstrable
rather than merely promised.

---

## AD-1 — Ingestion performs zero transformation; normalization is a separate stage

**Options:** normalize during ingest (one pass, one file) · Bronze → Silver (two stages).
**Chose:** separate stages.
**Why:** the data contract mandates raw preservation, and the tempting one-liner — "just
map `category` to `clause_type` on the way in" — destroys the evidence of what
actually arrived, which is the only reason a Bronze layer exists. Giving
normalization its own obvious home removes the temptation.
**Tradeoff:** an extra file and an extra pass over 20 records. Irrelevant here.
**Revisit:** never. This matters more as data grows, not less.

## AD-2 — Metadata wraps the payload; it is never merged into it

**Options:** flatten `_ingest_*` keys alongside clause fields · `{_bronze, payload}` envelope.
**Chose:** envelope.
**Why:** exact round-trip (a test asserts byte-equivalence), and no collision risk
when the API adds a field matching one of ours — which, given it already renamed
one field and added another, is a live risk.
**Tradeoff:** consumers write `record["payload"]["clause_id"]`. Silver hides this.
**Revisit:** if we move to a columnar format where a top-level struct column costs
more than it earns.

## AD-3 — At-least-once, not at-most-once

**Options:** write → fsync → checkpoint (replay risk) · checkpoint → write (loss risk).
**Chose:** write first; dedupe on `record_hash` at Silver.
**Why:** the crash window between fsync and checkpoint is unavoidable without a
transaction. The question is only which way it fails. Replay produces a duplicate
we can detect and collapse; the inverse produces a clause that silently vanishes.
In contract compliance, a detectable duplicate beats an undetectable disappearance.
**Tradeoff:** consumers inherit a dedupe obligation — so it is a published contract
term (§2.3), not a hidden assumption. Bronze is at-least-once; **Silver is
exactly-once.**
**Demonstrated, not claimed:** `--simulate-crash-after N --crash-window` crashes
inside that exact window. The committed outputs show 21 Bronze rows → 20 Silver.
**Revisit:** with a transactional sink (Delta/Iceberg/Postgres). At that point this
protocol is **deleted**, not extended.

## AD-4 — Drift is auto-detected; renames are human-confirmed

**Options:** auto-apply a high-confidence rename map · propose and wait.
**Chose:** propose.
**Why:** 0.75 value-set overlap is strong evidence, not proof. A wrong auto-mapping
corrupts every downstream classification silently, and the corruption is invisible
because the field is populated and plausible. An unconfirmed rename instead leaves
`clause_category: null` plus `_unmapped_fields: ["category"]` — a gap anyone can see.
**Tradeoff:** a human is in the critical path before Silver is complete for a new
API version. Correct for a governed platform; would be painful at high frequency.
**Revisit:** when the source publishes a machine-readable schema or changelog we can
bind to. Then confirmation is automated against an authority, not a guess.

## AD-5 — Drift warns; it does not block the landing

**Options:** validate-then-write · write-then-report.
**Chose:** write-then-report, with `--fail-on-drift` gating the *downstream* stage.
**Why:** the hard requirement is that changed data is never silently dropped, and
any pre-write validation gate is a mechanism for dropping it. The safest guarantee
is having no code path that can reject a record.
**Tradeoff:** Bronze can hold records no consumer understands yet — which is
correct for a raw layer.
**Revisit:** if a source ever emits genuinely malformed data, add
`bronze/_rejected/` with a reason. Quarantine, still never delete.

## AD-6 — Plain JSONL on local disk; no database, no Parquet

**Options:** SQLite/DuckDB (transactional, queryable) · Parquet (compact, typed) · JSONL.
**Chose:** JSONL.
**Why:** our constraints favour local file I/O and warns against framework ambition.
JSONL is append-friendly, crash-tolerant at line granularity, diffable in git, and
`cat`-able. Since the deliverable requires committing outputs, human-readability is
a real requirement here, not a nicety.
**Tradeoff:** no typing, no compression, no query engine.
**Revisit:** past ~1M records, or the first request for a filtered scan. Parquet for
Silver/Gold; JSONL stays in Bronze.

## AD-7 — Partition by ingest date; sub-partition by content-derived run id

**Options:** timestamp-only paths (always unique, unresumable) · `ingest_date` + `sha256[:8]`.
**Chose:** the latter — `ingest_date=2026-08-16/run_id=clauses_batch_2__6a33ab9b`.
**Why:** satisfies "partition by ingestion timestamp" while making the run id
**stable across restarts**, which is what makes resume possible at all. It also
makes "the source file changed underneath a half-finished run" structurally
impossible: different content is a different run id, so the two can never
interleave in one part file. Rollback becomes a directory-level operation.
**Tradeoff:** a re-export with byte-identical content resumes or no-ops rather than
creating a fresh run. That is the correct behaviour, and it is documented.
**Revisit:** at high volume, add an hour partition.

## AD-8 — Telemetry is OTel-shaped, with a dependency-free fallback

**Options:** `logging` module · hard dependency on `opentelemetry-sdk` · facade with both.
**Chose:** a `Telemetry` facade emitting traces, correlated logs, and metrics. Uses
the real OTel SDK when installed (and honours `OTEL_EXPORTER_OTLP_ENDPOINT`, so
production export is one env var and zero code changes); falls back to a ~100-line
shim writing byte-identical files when it is not.
**Why:** on-call has to answer whether the system is healthy, and that is unanswerable
without telemetry. But the operator must be able to clone and run with no installs.
The fallback is the same principle used everywhere else here: **degrade visibly** —
the active backend is stamped into `metrics.jsonl` and onto the root span.
**Tradeoff:** ~100 lines of shim, and metrics are an in-process snapshot rather than
true OTLP metrics (documented in the module).
**Revisit:** the moment a collector exists. Delete the shim, keep the facade.
**See also:** AD-9 for *where* the telemetry lands.

## AD-9 — Telemetry lives at the repo root; deliverables live in their phase

**Options:** telemetry under `<phase>/output/logs/` (co-located with what produced
it) · telemetry at repo-root `output/logs/`.
**Chose:** repo root, overridable with `CI_LOG_DIR`.
**Why:** observability is cross-cutting. The join that matters is "clause
CLZ-2025-0018 landed at Bronze record 5" → "the risk agent flagged it" → "a human
overrode the flag", and that trace spans two phases. Splitting spans across
`phase1_ingestion/output/logs/` and `phase2_agents/output/logs/` would break the
exact query the telemetry exists to answer. The rule:

    telemetry     cross-cutting, samplable, expirable   -> output/logs/
    deliverables  owned by one phase, durable           -> <phase>/output/

**Not the same as the audit log.** Phase 2's `audit_log.json` is a *deliverable*,
not telemetry — a legal record with different retention, different access rules,
and no sampling. It stays in `phase2_agents/output/` and carries `trace_id` as a
join key rather than being merged into this stream. Conflating the two would mean
a log-retention policy could quietly delete evidence.
**Tradeoff:** one output location that does not sit under a phase directory, which
is a small deviation from the platform's layout conventions. Documented in the README.
**Revisit:** immediately, once a collector exists — then the destination is an
endpoint and this decision evaporates.

## AD-10 — Supersession detection, though nobody asked for it

**Options:** document the gap only · detect and propose.
**Chose:** detect and propose, in a separate script that cannot modify Silver.
**Why:** `CLZ-2025-0013` replaces `CLZ-2025-0001` and both look `active`. Without
this, Phase 2 will confidently flag a clause the legal team fixed a month ago, and
nothing in the output would reveal the error. Cost is ~120 lines.
**Tradeoff:** section numbering is a convention, not a guarantee — so the output is
`PENDING_HUMAN_REVIEW` evidence, never a decision.
**Revisit:** if the source system ever exposes a real supersession link, delete this
heuristic immediately in favour of the authoritative field.

---

## What I would change

**With more time:** contract tests that fail CI when Silver violates
`silver_schema.json`; quarantine + replay for malformed records; supersession as a
real Silver dimension once confirmed.

**With more data:** Parquet for Silver, batched fsync (accepting an N-record replay
window instead of one), hour-level partitions, and a real dedupe index instead of an
in-memory set.

**In production:** replace the hand-rolled checkpoint with a transactional sink;
route drift `BREAKING` events to the on-call channel rather than a file; move the
schema registry from a local JSON file to a shared service; add retention and PII
controls before this dataset leaves the platform boundary.

## What is deliberately absent

No Spark/Dask, no Parquet/Delta/Iceberg, no database, no orchestrator, no API
client (files stand in for API responses), no PII redaction, no schema *migration* —
only detection and human-confirmed mapping. Each of these is a decision, not an
oversight; the triggers that would reverse them are listed above.

---

# Phase 2 decisions

## AD-11 — LangChain for the model interface; not LangSmith for observability

**Options:** raw SDK calls · LangChain · an agent framework (LangGraph/CrewAI).
**Chose:** `langchain-core` only, for the `BaseChatModel` interface, callbacks and rate limiter.
**Why:** the swap seam. `provider: stub` → `provider: anthropic` is one config
value, and the chains, prompts, schemas, budget accounting and audit records are
untouched. That is a far stronger answer to "show where real calls go" than a
comment. **We explicitly do not use LangSmith:** it ships prompts and completions
off-box, which our own data contract forbids. LangChain's callbacks already carry
the prompt, token usage and a `run_id`/`parent_run_id` tree — we route those into
our existing OTel facade instead.
**Tradeoff:** a dependency, and Phase 1's zero-install property does not extend to Phase 2.
**Revisit:** if the workflow ever branches genuinely, LangGraph earns its place. Today it is linear.

## AD-12 — Rules before the model

**Options:** LLM for all risk analysis · deterministic rules first, model for the residue.
**Chose:** rules first. 20 of 34 agent decisions in the shipped run never touch an LLM.
**Why:** "60 days exceeds `payment_terms.max_days_to_pay` (30)" is a comparison,
not a judgement. A rule is free, instant, identical every run, **and cites the
policy it applied** — which is what makes a finding defensible to a regulator
rather than an opinion. The model handles what a rule cannot measure.
**Tradeoff:** regexes over legal prose are brittle; limits are documented in `rules.py`.
**Revisit:** never wholesale. Individual rules should be retired when the model
demonstrably beats them on the eval set.

## AD-13 — Risk is judged against a versioned policy file

**Chose:** `config/firm_standards.json`, human-owned, with `version_history`.
**Why:** it makes findings arguable and changeable by the people accountable for
them, and it makes the learning loop concrete — what the system proposes is a
diff to readable policy text, not a mutated prompt or model weights. The 1x
liability cap is not invented: J. Martinez's review note on CLZ-2025-0018 states it.
**Tradeoff:** the standards themselves are unvalidated; nothing here proves they
are correct. That is a legal judgement, and the override rate is its signal.

## AD-14 — Two human-in-the-loop gates, kept separate

**Gate A — decision review: advisory.** A human may override any clause; the
pipeline still runs unattended. **Gate B — behaviour change: blocking.** Prompts,
thresholds and standards change only via `apply_proposal.py`, run by a person.
**Why:** conflating them is the classic failure — block everything and the system
is unusable, block nothing and it fails the platform's non-negotiable governance rule. Stated as
one rule: *an override changes the outcome for one clause; only an approved
proposal changes the rule.*
**Enforced structurally:** machine state → `output/`, human decisions → `config/`,
and `test_no_agent_process_writes_to_config` fingerprints the directory across a
full run.

## AD-15 — The decision cache is keyed on prompt version

**Why:** promoting a prompt must not serve decisions made by the previous one.
That is a correctness property, not an optimisation. A cache hit is logged as
`decided_by=cache` so an engineer can distinguish a reused decision from a
recomputed one.

## AD-16 — The audit ledger is the recovery mechanism

**Why:** the checkpoint records *which* clauses finished; the ledger records
*what was decided*. Resume replays the ledger (`rehydrate_from_ledger`).
Discovered the hard way — without it, clauses processed before a halt reappeared
as `not_processed` with their findings dropped, reintroducing the exact
silent-drop failure the guardrails exist to prevent.
**Consequence:** two sources of truth would be able to disagree, so the
checkpoint deliberately does *not* duplicate decision state.

## AD-17 — Evaluation exists, with its limits stated

**Chose:** `eval/` with human-authored ground truth, deliberately not copied from
Silver's `clause_category` (that would score the code against its own input).
**Why:** guardrails prove the system is controllable and the ledger proves it is
accountable; neither says it is *accurate*. Risk detection scores 1.00 F1 at
standard level against independent labels.
**Stated honestly:** 20 clauses, one annotator — a regression detector, not a
measurement. Three tests break the system on purpose to prove the meter is not
stuck at 100%.
