# Architecture Decision Note

*Full reasoning for all 17 decisions: [`architecture_decisions_appendix.md`](architecture_decisions_appendix.md).*

## What I prioritised, in order

**1. Don't lose data. 2. Make change visible. 3. Be resumable. 4. Be legible.**
Every tradeoff below resolves in that order. Where two options were close, the one that fails *louder* won.

## The three decisions that shaped everything

**Ingestion transforms nothing.** The tempting one-liner — "just map `category` to `clause_type` on the way in" — destroys the evidence of what actually arrived, which is the only reason a Bronze layer exists. Normalisation is a separate stage; a test asserts the Bronze payload is byte-identical to the source. *(AD-1, AD-2)*

**The machine proposes; a human disposes.** Applied five times: schema renames, clause supersession, low-confidence classifications, risk overrides, and learning proposals. Enforced **structurally, not procedurally** — machine state goes to `output/`, human decisions live in `config/`, and `test_no_agent_process_writes_to_config` fingerprints that directory across a full run. Two gates, deliberately separate: *an override changes the outcome for one clause; only an approved proposal changes the rule.* Conflating them is how you either block everything (unusable) or block nothing (non-compliant). *(AD-4, AD-14)*

**Rules before the model.** 20 of 34 agent decisions never touch an LLM. "60 days exceeds `payment_terms.max_days_to_pay` (30)" is a comparison, not a judgement — and a rule is free, instant, reproducible, **and cites the policy it applied**. That is the difference between a finding a lawyer can argue with and one that says "the model thought so". The LLM handles only what a rule cannot measure. *(AD-12, AD-13)*

## Tradeoffs I accepted

| Decision | Gave up | Why it was right here |
|---|---|---|
| At-least-once Bronze, dedupe at Silver | consumers inherit a dedupe obligation | a detectable duplicate beats an undetectable disappearance |
| Drift warns, never blocks the landing | Bronze holds records nobody understands yet | any pre-write gate is a mechanism for dropping data |
| Plain JSONL, no database | typing, compression, query engine | outputs are committed for review, so readability is a requirement |
| LangChain core only, no LangSmith | a polished trace UI | prompts and clause text may not leave the boundary |
| Built the audit ledger | no UI, no annotation workflow | it is a compliance record with different retention rules — and it turned out to be our recovery mechanism |
| Single-threaded | throughput | a decision you cannot reproduce cannot be defended |

## What I'd change

**More time:** contract tests in CI against `silver_schema.json`; quarantine + replay for malformed records; clause supersession as a real Silver dimension rather than a proposal.

**More data:** Parquet for Silver/Gold; batched fsync; hour-level partitions; the precedent lookup becomes a vector store somewhere past a few thousand clauses (a dict beats embeddings at 20).

**Production:** designs and a sequenced TODO list are in [`production_readiness.md`](production_readiness.md). Replace the hand-rolled checkpoint with a transactional sink — at that point it is deleted, not extended. Rate limiting and retry are already wired but untested against a real provider. PII redaction is **required before this data leaves the platform**; today there is none. Drift `BREAKING` events route to on-call rather than a file.

**Cost at scale** — measured, not guessed: $0.0206 per 20 clauses ≈ **$0.001/clause**, and rules cover 59% of decisions at zero cost. 100k clauses ≈ $100 per full pass. Cost is not the constraint; review capacity is. The lever that matters is the escalation rate, not the token price.

## What I'd want to know before going further

The eval harness scores 1.00 F1 on risk detection against 20 human-labelled clauses — a regression detector, not a measurement. The real question is whether `firm_standards.json` is *correct*, and nothing in this system can answer that: it is a legal judgement. The override rate per standard is the signal, which is why it drives the learning proposals. *(AD-17)*
