# Documentation index

## Start here

The four documents that matter. Read in this order; ~15 minutes total.

| Document | Lines | Covers |
|---|---|---|
| [`../README.md`](../README.md) | 382 | architecture, how to run it, where to extend it, assumptions and limitations |
| [`architecture_note.md`](architecture_note.md) | 41 | key decisions and tradeoffs, what was prioritised and why, what would change with more time / data / production |
| [`runbook.md`](runbook.md) | 225 | monitoring · agent failure · rollback |
| [`buy_vs_build.md`](buy_vs_build.md) | 155 | one component — the audit ledger — with revisit triggers and a replace signal |

## Supporting material — read only if you want the detail

Deliberately kept out of the deliverables above so those stay short. Nothing
here is required to understand or run the system.

| Document | What it is |
|---|---|
| [`architecture_decisions_appendix.md`](architecture_decisions_appendix.md) | the full 17 decisions, each with options considered, the tradeoff accepted, and the trigger that would reverse it. `architecture_note.md` is the one-page summary of this |
| [`production_readiness.md`](production_readiness.md) | what it takes to run this for real: schema-coupling inventory, RabbitMQ/Celery topology, Postgres DDL, S3 retention, and 21 sequenced TODOs |
| [`engineering_log_ingestion.md`](engineering_log_ingestion.md) | requirement→test→artifact traceability for Phase 1, the telemetry catalogue, and the corrections made mid-build |
| [`engineering_log_agents.md`](engineering_log_agents.md) | the same for Phase 2 |
| [`../phase1_ingestion/data_contract.md`](../phase1_ingestion/data_contract.md) | the Phase 1 deliverable: what downstream consumers can rely on, and twelve documented limitations |

## Where the reasoning actually lives

Much of it is in the code. Every module opens with a docstring explaining not
only what it does but **what it deliberately does not do, and why** — which is
usually the more useful half. Worth reading directly:

| File | Why |
|---|---|
| [`../phase2_agents/rules.py`](../phase2_agents/rules.py) | why most risk analysis does not use an LLM |
| [`../phase2_agents/llm.py`](../phase2_agents/llm.py) | the swap seam, and why there is no conversation memory |
| [`../phase2_agents/agents/auditor.py`](../phase2_agents/agents/auditor.py) | the two human-in-the-loop gates, and why conflating them fails the requirements |
| [`../phase1_ingestion/ingest.py`](../phase1_ingestion/ingest.py) | the durability protocol and the deliberate crash window |
| [`../common/observability.py`](../common/observability.py) | why telemetry is repo-root and the audit ledger is not |

## Config files are documentation too

Each carries a `_doc` block explaining what it controls and who owns it:

- [`../phase2_agents/config/firm_standards.json`](../phase2_agents/config/firm_standards.json) — the policy risk is judged against, with `version_history`
- [`../phase2_agents/config/agents.yml`](../phase2_agents/config/agents.yml) — the operator control surface
- [`../phase1_ingestion/config/schema_aliases.json`](../phase1_ingestion/config/schema_aliases.json) — human-confirmed field renames
- [`../eval/ground_truth.json`](../eval/ground_truth.json) — human labels, and why they are *not* derived from the source data
