# Buy vs Build — the Audit & Decision Ledger

**Component:** `phase2_agents/agents/auditor.py` + `models.AuditRecord` — the
append-only record of every agent decision, human override and config change.

**Decision: built.** ~490 lines. Mature products exist for exactly this
(Langfuse, LangSmith, Arize Phoenix, Weights & Biases, Braintrust), so this was
a real choice rather than a default.

---

## Why built

**1. The data cannot leave the boundary.** Our own Phase 1 data contract (§3.5)
says clause text, client names and reviewer names are cleartext PII with no
redaction, and must not leave the platform. Every hosted option ships prompts
and completions to a third party. Self-hosted Langfuse solves that — and turns
a 490-line file into a Postgres instance, a container and an upgrade path we
would own forever.

**2. What we need is an evidence ledger, not an LLM trace.** The observability
products are built around *runs, spans and token counts* — which we already get
from our OTel layer and LangChain's callbacks. What they are not built around
is the thing that actually matters here:

- `model_output` / `human_decision` / `effective_value` as **three separate
  fields** on one record
- a `decided_by` value that includes `rule` — most of our decisions never touch
  an LLM at all, and a trace-shaped tool has nowhere to put them
- `firm_standards_version` and `prompt_version` on every record, so a decision
  can be replayed against the policy that produced it
- config changes (`apply_learning_proposal`) in the same stream as model
  decisions

We would have used a general-purpose product for 30% of the requirement and
built the other 70% alongside it — while still carrying the dependency.

**3. Different retention rules from telemetry.** Traces are samplable and
expirable. This ledger is a compliance record: an AEC firm may need to show why
a clause was flagged years later, potentially in litigation. Storing it in a
tool with sampling and TTL semantics risks a retention policy quietly deleting
evidence. That is why it lives in `phase2_agents/output/`, not in
`output/logs/` (see AD-9).

**4. It turned out to be the recovery mechanism.** Not a planned benefit, and the
strongest one. When a run halts, resume replays the ledger to restore what was
already decided. Had this been an external service, recovery would depend on
querying a third party mid-incident — or we would have built a second, local
checkpoint store duplicating it.

**5. Cost of building was genuinely low.** Append-only JSONL, pydantic
validation, one flush per decision. There is no clever code in it. The
justification for buying is usually the UI and the retention infrastructure —
neither of which we need at 20 clauses per batch and one reviewing team.

## What we gave up

Honestly: a lot of polish.

- No UI. Reviewing decisions means reading JSONL or writing a query.
- No trace waterfall, no side-by-side prompt diffing.
- No built-in eval or annotation workflow — we hand-rolled `eval/` instead.
- No dataset versioning or prompt playground.
- Retention, backup and access control are ours to build; today there are none.

---

## When I would revisit

Any **one** of these, on its own:

| Trigger | Why it changes the answer |
|---|---|
| A second team needs to read the ledger | The moment non-engineers need access, "read the JSONL" stops being an answer and we start building a UI — which is the product we declined to buy |
| Anyone asks for prompt A/B testing or annotation workflows | That is a real product surface, not a file format. Building it is months |
| Volume passes ~100k decisions | Grep over JSONL stops working; we would be building indexing and retention, i.e. a database with a worse API |
| We adopt a hosted model provider **and** get clearance for data egress | Removes the constraint that drove the decision. Worth re-testing rather than assuming |
| Regulators require a certified, tamper-evident store | Our append-only file is convention, not cryptographic. A WORM store or hash-chained ledger is specialist work |

## The signal that says replace it

**Concrete and measurable, so it is not a judgement call:**

> When more than ~20% of a sprint's engineering time goes into the ledger's
> *presentation and querying* rather than its content, we are building an
> observability product instead of a contract pipeline. Buy at that point.

Two earlier, cheaper tells:

- **Someone writes a second reader.** The first ad-hoc script to parse
  `audit_log.jsonl` into a dashboard is the beginning of that product. One is
  fine; a third means the need is real.
- **A question cannot be answered from the ledger.** "Show me every clause where
  the model said high and a human said low, grouped by reviewer, last quarter"
  is a query. If we start denormalising or adding indexes to answer it, we are
  building a database.

## Migration path if we do buy

Deliberately kept cheap, because the decision above is reversible:

1. `AuditRecord` is a pydantic model — a `to_langfuse()` adapter is one function.
2. The ledger is append-only JSONL, so backfilling history is a replay.
3. `trace_id` is already on every record, so ours and a vendor's view join on day one.
4. Keep the local ledger as the system of record; treat the product as an index
   over it. The compliance obligation stays in-house; the convenience is bought.

**That last point is the actual position:** this is not "never buy". It is
"do not outsource the evidence."

---

## What we would buy next, and in what order

This covers one component, and the ledger is it. But "built" here is a
*proof-of-concept* decision, not a standing one — the rest of this system is
local files precisely because that was right for a PoC. For completeness, the
buy decisions already queued up:

| # | Component | Today | Buy | Trigger |
|---|---|---|---|---|
| 1 | **Storage** | JSONL on local disk | **PostgreSQL** | any second process reads the data |
| 2 | **Durability** | local disk | **S3 + Object Lock** | first production write. Retroactive locking is impossible, so this cannot be deferred |
| 3 | **Work distribution** | in-process loop with a checkpoint file | **RabbitMQ + Celery** | data arrives continuously rather than as files, or one worker cannot keep up |
| 4 | **Shared control state** | in-process counters, a local STOP file | **Redis** | **before** #3, not after — see below |
| 5 | **Telemetry backend** | JSONL + a shim | **OTel Collector → Tempo/Prometheus/Loki** | anyone needs to correlate across services. One env var; the code is already OTLP-shaped |
| 6 | **Labelled eval data** | 20 clauses, one annotator | counsel-reviewed labels | before any accuracy claim leaves the team |

**Full designs — queue topology, Celery settings, Postgres DDL, S3 bucket
policies, and the schema-coupling inventory — are in
[`production_readiness.md`](production_readiness.md).**

### The buy decision that is easy to get wrong

**#4 before #3.** Every guardrail we built is process-local:

- `BudgetGuard` is an in-process counter — 10 workers means 10× the budget is spent, silently
- `InMemoryRateLimiter` is a per-process token bucket — 10 workers means 10× the provider's rate limit, then 429s
- the kill switch is a local file only one worker can see

Buying the queue first is the satisfying, visible work. Doing it before the
shared control state means the guardrails keep reporting green while none of them
are actually enforcing anything, and the first symptom is a model bill an order
of magnitude over budget. **Buy Redis before you buy RabbitMQ.**

### What gets deleted, not extended

Worth stating because the instinct is usually to keep both:

- Buying the queue **deletes** `_checkpoint.json` and the resume logic. Broker acks do that job properly, and two sources of resume truth can disagree.
- Buying Postgres **deletes** the hand-rolled `record_hash` dedupe — it becomes a `PRIMARY KEY` and `ON CONFLICT DO NOTHING`.
- Buying the collector **deletes** the ~100-line telemetry shim; the facade stays.

Each of those is a hand-rolled stand-in for something a product does better. The
ledger, above, is the one component where that is not true.
