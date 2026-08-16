# Contract Intelligence Pipeline

Governed ingestion and multi-agent review of AEC contract clause data, on a
medallion architecture (Bronze → Silver → Gold).

**All three phases complete.** 65 tests, no network calls, LLM calls stubbed.

---

## If you have five minutes

Read these four files, in this order:

| File | Why |
|---|---|
| [`phase1_ingestion/output/drift/drift_report.md`](phase1_ingestion/output/drift/drift_report.md) | the schema changed between batches; here is what we did about it |
| [`phase2_agents/output/gold/clause_risk_register.json`](phase2_agents/output/gold/clause_risk_register.json) | every clause, its risk, and who decided — model or human |
| [`phase2_agents/output/learning_proposals.json`](phase2_agents/output/learning_proposals.json) | the system asking permission to change itself, and not changing itself |
| [`docs/architecture_note.md`](docs/architecture_note.md) | one page on why it is built this way |

Full documentation map, with the four core documents separated from
supporting material: [`docs/README.md`](docs/README.md).

All outputs are committed, so nothing needs running to be read.

## Running it

Phase 1 needs **Python 3.10+** and nothing else. Phase 2 adds three packages.

```bash
pip install -r requirements.txt     # langchain-core, pydantic, PyYAML
python run.py all                   # everything, from scratch (~30s)
```

One entry point, same on every platform — no shell scripts, no `.bat` files.

```bash
python run.py phase1     # ingest -> drift -> normalize -> supersede  (no deps)
python run.py phase2     # classify -> flag risk -> audit -> gold
python run.py eval       # accuracy against human labels
python run.py test       # 65 tests
python run.py clean      # delete generated outputs
```

Or in a pinned environment:

```bash
docker build -t contract-intelligence .
docker run --rm -v "$PWD/output:/app/output" contract-intelligence
```

`python run.py` is the developer path — clone, install, run. The image exists
for a different reason: it answers *which* Python and *which* langchain produced
these outputs, and it is where the production services in
[`docs/production_readiness.md`](docs/production_readiness.md) attach (see the
commented services in `docker-compose.yml`).

**`run.py all` deliberately runs the failure paths**, not the happy path — a
crash inside the commit window, a budget halt, a refused resume. Each step
asserts its expected exit code, so a guardrail that quietly stopped working
fails the run instead of turning it green.

Both scripts deliberately run the **failure** paths, not the happy path — a
simulated crash mid-ingest, then a budget halt and a refused resume — so the
committed outputs are evidence rather than claims.

---

## The problems this solves

Four problems are planted in the data. Three are known; one is not.

**1. The schema changed without warning.** Batch 1 is API v2.1, batch 2 is v2.3.
`clause_type` became `category`, `modified_by` disappeared, `review_history`
appeared, and `status` gained a value (`under_review`) that a key-set diff would
miss. The naive pipeline either writes 8 records with a null category, or rejects
the batch. **Every record lands regardless**, and the change is reported in
`output/drift/drift_report.md`. The suspected rename is *proposed*, never applied —
a human confirms it in `config/schema_aliases.json`.

**2. The job can die partway through.** Checkpointed after every record.
`--simulate-crash-after 5 --crash-window` dies inside the unacknowledged window,
so the committed manifest genuinely shows 21 Bronze rows → 20 Silver, one replay
duplicate deduped.

**3. Downstream doesn't know what it can rely on.**
[`phase1_ingestion/data_contract.md`](phase1_ingestion/data_contract.md) — fields,
guarantees, and twelve known limitations.

**4. Not strictly required: `CLZ-2025-0013` silently replaced `CLZ-2025-0001`.**
Same contract, "Section 8.1 (Amended)", indemnity narrowed to a negligence
standard. Different clause IDs, both `active`. Left alone, the risk agent
confidently flags a clause the legal team fixed a month ago. `supersede.py`
proposes these for review, and the Gold register carries `superseded_by`.

## Architecture

```
data/*.json                    exactly as the API delivered them
      |
      v  ingest.py ----------- output/bronze/ingest_date=.../run_id=<name>__<sha8>/
      |                          _source_snapshot.json   raw bytes, hash-verified
      |                          part-0000.jsonl         {_bronze: meta, payload: raw}
      |                          _checkpoint.json        resume bookmark
      +- drift.py ------------ output/drift/drift_report.{json,md}   never filters
      v  normalize.py -------- output/silver/clauses.jsonl    THE PUBLISHED DATASET
      v  supersede.py -------- output/proposals/              advisory only
---------------------------------------------------------------------------------
      v  phase2_agents/run.py
         |- classifier      rule first, model for the ambiguous middle
         |- risk_flagger    rules.py vs firm_standards.json, model for the residue
         +- auditor         ledger . overrides . learning PROPOSALS
      v                     output/gold/clause_risk_register.json
                            output/audit_log.json . escalations . proposals

every stage ---------------- output/logs/{traces,pipeline,metrics}.jsonl
```

**Run order:** `ingest` (per file) → `normalize` → `supersede` → `agents` → `eval`.
Each stage reads files and writes files, so any one can be re-run alone and
inspected by hand. Silver and Gold are rebuilt from scratch every time.

### The idea the whole design rests on

**The machine proposes; a human disposes.** It appears five times:

| Where | Machine | Human |
|---|---|---|
| Field rename `clause_type`→`category` | detects it, scores 0.75 confidence | confirms in `schema_aliases.json` |
| Clause supersession | proposes with evidence | confirms upstream |
| Low-confidence / unmappable clause | escalates to a queue | works the queue |
| Risk finding | flags, cites the standard | may override that clause |
| **Learning** | writes a proposal | `apply_proposal.py --approved-by` |

Two gates, kept separate. **Gate A** (decision review) is advisory — 20 approval
prompts to process 20 clauses would make the system useless. **Gate B**
(behaviour change) is blocking, no exceptions.

> **An override changes the outcome for one clause. Only an approved proposal
> changes the rule.**

Enforced structurally: machine state → `output/`, human decisions → `config/`,
and `test_no_agent_process_writes_to_config` fingerprints that directory across a
full run. That is the platform's non-negotiable governance rule, asserted rather than promised.

### How corrections are incorporated over time

1. A new engineer overrides a finding. It changes **that clause only**; the original model output is never edited — a new ledger record is appended.
2. The auditor watches for the same standard being overridden repeatedly. One override is an exception (contracts are individual); two in the same direction says the *standard* is wrong.
3. It writes a `LearningProposal` — a **diff to a readable policy file**, with evidence, rationale, and what could go wrong if applied. Status `PENDING_HUMAN_APPROVAL`.
4. A person runs `apply_proposal.py --id … --approved-by "K. Chen"`. Without `--approved-by` it prints the diff and changes nothing.
5. The change bumps `firm_standards.json` to a new version, appends to its `version_history`, writes `approvals.jsonl`, and appends to the audit ledger.
6. Every future decision records `firm_standards_version`, so any decision can be replayed against the policy that produced it.

**What changes is policy text, not model weights or a silently mutated prompt.**
That is what makes it reviewable by the people accountable for it — and why the
system can never perform step 4 itself.

Live example: two reviewers accepted duty-to-defend clauses (`CLZ-2025-0019`
mutual indemnity, `CLZ-2025-0010` federal work). The system noticed, and asked.

---

## Results

```
Phase 1   20 clauses from 2 API versions - 6 drift events (1 BREAKING,
          1 needs-human) - 21 Bronze rows -> 20 Silver (1 replay duplicate)
          Silver is byte-identical to the provided clauses_ingested_fallback.json

Phase 2   20/20 processed - decided_by: rule 20 | model 14 | human 8
          18 policy-referenced findings across 11 clauses
          $0.0206 - 14 LLM calls - 0 errors - 42 audit records
          2 overrides - 1 escalation - 1 learning proposal - 2 pending review

Eval      classification 20/20 - risk detection 1.00 F1 at standard level
          (20 clauses, one annotator: a regression detector, not a measurement)
```

**`rule 20 / model 14`** is the number worth looking at: more than half of all
decisions were deterministic — zero cost, reproducible, each citing the policy it
applied.

## Verification

```bash
python run.py test          # 65 tests, stdlib unittest
```

Covering: Bronze payload byte-equivalence · drift detection (and that identifiers
are *not* reported as drift) · crash, resume and replay dedupe · Silver equalling
the provided reference dataset · Silver conforming to its published JSON Schema ·
budget halt accounting for every clause · kill switch · scope refusal · audit
model/human separation · ledger append-only · **no agent writes to `config/`** ·
proposals never self-applied · and three tests that break the system on purpose
to prove the eval harness detects regressions rather than sitting at 100%.

---

## Where to extend it

**Modular — change freely:**

| | |
|---|---|
| `common/observability.py` | one telemetry facade; `CI_LOG_DIR` and `OTEL_EXPORTER_OTLP_ENDPOINT` are the only knobs |
| `phase2_agents/config/firm_standards.json` | the policy risk is judged against; versioned, human-owned |
| `phase2_agents/config/agents.yml` | model, budgets, thresholds, prices, **active** prompt versions |
| `phase2_agents/config/prompts/*.json` | add a `v2` file; it does nothing until promoted in `agents.yml` |
| `phase1_ingestion/config/schema_aliases.json` | confirm a rename, re-run `normalize.py` |
| `rules.py` | each check is independent; add one and add a ground-truth label |
| `llm.py::build_model` | **the swap seam** — `provider: anthropic` and nothing else changes |

**Hardcoded, deliberately:**

- source records at `document["clauses"]`; one part file per run, no rollover
- Silver's 13 fields are enumerated in `normalize.py` — adding one is a contract change, so it *should* need code
- `SOURCE_CATEGORY_MAP` in `classifier.py` — a taxonomy mapping, not a business tunable
- `drift.ENUM_MIN_REPETITION=4`, `RENAME_CONFIDENCE_THRESHOLD=0.5` — tuned against these two batches
- severity ranking, and "worst finding wins" for overall severity

**Needs work before production** — full designs and a sequenced TODO list in
[`docs/production_readiness.md`](docs/production_readiness.md):

- **no PII redaction** — client and reviewer names are cleartext; this dataset must not leave the platform
- no quarantine path for malformed records (today they fail the run)
- no retention policy; Bronze grows unbounded
- single-writer assumption, no locking
- rate limiting and retry are wired but never exercised against a real provider
- `eval/` has 20 labels from one annotator; production needs counsel-reviewed labels versioned alongside the standards

## Assumptions

- Files stand in for API responses. A real client adds pagination, auth and retries; the landing logic is unchanged.
- `clause_id` is globally unique and stable. A collision logs at ERROR, never resolved silently.
- LLM calls are stubbed. The stub populates `AIMessage.usage_metadata` exactly as `ChatAnthropic` does, so budget arithmetic, callbacks, caching and audit records are production-real — only the intelligence is simulated.
- Firm standards are calibrated from the corpus, and from J. Martinez's review note on `CLZ-2025-0018` ("2x exceeds firm standard — cap at 1x"). In a real engagement they come from the contract playbook.
- Human overrides are replayed from `review_history` where the data has it (batch 2) and simulated where it does not (batch 1 has no such field). Every record carries its `source`.
- Data files were renamed from dashes to underscores to match the platform's layout conventions. Contents unmodified.

## Known limitations

Full list in [`data_contract.md` §3](phase1_ingestion/data_contract.md). The four that matter most:

1. **`modified_by` is unrecoverable** for v2.3+ records — upstream data loss, faithfully represented.
2. **Supersession is proposed, not modelled.** A consumer reviewing every `active` clause will review one no longer in force.
3. **Rule extractors are regexes over legal prose.** Precise about what they match, silent about what they do not. Not a substitute for legal review.
4. **Nothing validates that the firm standards themselves are right.** That is a legal judgement; the override rate is its only signal.

---

## Command reference

Every entry point and every flag. Each script's `--help` and module docstring
carry the same information.

### Phase 1 — ingestion

```bash
python phase1_ingestion/ingest.py --source data/clauses_batch_1.json
```

| Flag | Effect |
|---|---|
| `--source PATH` | **required** — source clause JSON file |
| `--fail-on-drift` | exit 2 if drift needs a human. Data lands either way; this only lets an orchestrator gate the *downstream* stage |
| `--simulate-crash-after N` | crash after committing N records, to demonstrate resume |
| `--crash-window` | with the above, crash *inside* the commit window (record durable, checkpoint not yet advanced) to demonstrate at-least-once replay and dedupe |
| `--restart` | archive the previous attempt and start over. Never deletes |

```bash
python phase1_ingestion/normalize.py     # Bronze -> Silver. No flags; idempotent
python phase1_ingestion/supersede.py     # supersession proposals. No flags; advisory only
```

Exit codes: `0` success · `1` error, crash or pause · `2` landed, but drift needs a human (`--fail-on-drift`).
Pause anytime with `touch phase1_ingestion/output/STOP`.

### Phase 2 — agents

```bash
python phase2_agents/run.py
```

| Flag | Effect |
|---|---|
| `--max-cost-usd N` | override the configured cost budget. `--max-cost-usd 0.005` forces a halt |
| `--max-total-tokens N` | override the configured token budget |
| `--acknowledge-halt "REASON"` | **required to resume after a guardrail halt.** Recorded in the audit log |
| `--restart` | ignore the checkpoint and start over |
| `--no-cache` | bypass the decision cache |

Exit codes: `0` complete · `1` failed · `2` halted by a guardrail (all clauses still accounted for) · `3` complete, escalations open.
Kill switch: `touch phase2_agents/output/STOP`.

### The human approval step

The only command that can change system behaviour.

```bash
python phase2_agents/apply_proposal.py --list                    # what is pending
python phase2_agents/apply_proposal.py --id LP-...               # show the diff, change nothing
python phase2_agents/apply_proposal.py --id LP-...        --approved-by "K. Chen" --rationale "Counsel reviewed the exceptions."
```

| Flag | Effect |
|---|---|
| `--list` | list pending proposals |
| `--id ID` | review one. **Without `--approved-by`, prints the diff and exits without touching anything** |
| `--approved-by NAME` | apply it. Bumps `firm_standards.json`, appends to `version_history`, writes `approvals.jsonl` and the audit ledger |
| `--rationale TEXT` | why you approve. Recorded in the config file itself |

### Evaluation

```bash
python eval/evaluate.py                  # as configured (rules first)
python eval/evaluate.py --model-only     # force the LLM path, bypassing rule shortcuts
```

### Switching on real LLM calls

One value in `phase2_agents/config/agents.yml`:

```yaml
model:
  provider: anthropic        # was: stub
  name: claude-sonnet-5
```

Then `pip install langchain-anthropic` and `export ANTHROPIC_API_KEY=...`.
Chains, prompts, schemas, budget accounting, callbacks and audit records are
unchanged — only the model object differs (`phase2_agents/llm.py::build_model`).

### Environment variables

| Variable | Default | Effect |
|---|---|---|
| `CI_LOG_DIR` | `output/logs/` | where telemetry is written |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | unset | if set *and* `opentelemetry-sdk` is installed, spans also ship to a collector. No code change |
| `PYTHON` | `python` | interpreter used by the run scripts |
| `ANTHROPIC_API_KEY` | unset | required only when `model.provider: anthropic` |

---

## Layout

```
├── run.py                             THE entry point: all | phase1 | phase2 | eval | test | clean
├── Dockerfile / docker-compose.yml    pinned environment; production services stubbed in
├── data/                              the three provided files (renamed only)
├── common/observability.py            traces, logs, metrics — all phases
├── phase1_ingestion/
│   ├── ingest.py drift.py normalize.py supersede.py
│   ├── data_contract.md  silver_schema.json      THE INTERFACE
│   ├── config/schema_aliases.json                human-owned
│   └── output/                                   bronze · silver · drift · proposals
├── phase2_agents/
│   ├── run.py models.py llm.py rules.py guardrails.py callbacks.py
│   ├── agents/{base,classifier,risk_flagger,auditor}.py
│   ├── apply_proposal.py                         the ONLY writer to config/
│   ├── config/                                   human-owned: standards, prompts, agents.yml
│   └── output/                                   gold · audit_log · escalations · proposals
├── eval/                              ground truth + accuracy harness
├── output/logs/                       telemetry, shared across phases
├── docs/
│   ├── README.md                                 documentation index
│   ├── architecture_note.md                      1 page — start here
│   ├── architecture_decisions_appendix.md        all 17 decisions, full reasoning
│   ├── runbook.md                                monitoring · failure · rollback
│   ├── buy_vs_build.md                           the audit ledger + what we'd buy next
│   ├── production_readiness.md                   RabbitMQ/Celery, Postgres DDL, S3, TODO-1..21
│   └── engineering_log_{ingestion,agents}.md           what exists, what it emits, what changed
└── tests/                             65 tests, stdlib unittest
```

## Notes on layout

- **Telemetry lives at the repo root** (`output/logs/`) rather than inside a phase folder — a trace must be followable from ingestion through to an agent decision. Phase deliverables stay where the platform's layout conventions puts them. `audit_log.json` is deliberately *not* telemetry: it is a compliance record, and a log-retention policy must not be able to delete evidence. *(AD-9)*
- **Phase 2 reads our own Silver output**, not the provided fallback — they are byte-identical, and a test asserts it.
- Judgement calls I made rather than asking are recorded in
  [`docs/architecture_decisions_appendix.md`](docs/architecture_decisions_appendix.md).
  Where something is missing, it is listed above rather than left to be discovered.
