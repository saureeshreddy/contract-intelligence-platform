# Engineering Log — Agents

**Status: complete.** Every number below comes from a run, not from memory.

Companion to `engineering_log_ingestion.md`. `architecture_note.md` records *why*; this records *what exists*, *what it emits*, and *what changed during the
build*. §4 is the input for the runbook's monitoring section.

---

## 1. What this covers

Capability, where it lives, and the test that proves it. Every behaviour described here is asserted by a test; nothing below is a claim.

Run `python run.py test -v` for the full list.

---

## 2. What exists

| File | Lines | Role |
|---|---|---|
| `models.py` | 319 | pydantic schemas for everything crossing an agent boundary |
| `llm.py` | 371 | `StubChatModel`, rate limiter, decision cache, **the swap seam** |
| `rules.py` | 407 | deterministic policy checks — the part that does *not* use an LLM |
| `guardrails.py` | 293 | budget / scope / kill switch |
| `callbacks.py` | 207 | LangChain callbacks → our OTel telemetry |
| `agents/base.py` | 240 | prompt loading, call path, parse-with-one-retry |
| `agents/classifier.py` | 181 | Agent 1 |
| `agents/risk_flagger.py` | 240 | Agent 2 |
| `agents/auditor.py` | 486 | Agent 3 — ledger, overrides, learning proposals |
| `phase2_agents/run.py` | 605 | orchestrator, checkpointing, ledger rehydration, Gold register |
| `apply_proposal.py` | 288 | **the only writer to `config/`** |
| `tests/test_phase2.py` | 687 | 41 tests |

**Human-owned config** (no agent writes here; a test proves it):

| File | Contents |
|---|---|
| `agents.yml` | model, active prompt versions, budgets, rate limit, thresholds, prices |
| `firm_standards.json` | the policy risk is judged against, with `version_history` |
| `prompts/*.v1.json` | versioned prompt templates |
| `simulated_reviews.json` | demo scaffolding for batch 1, which has no `review_history` |

---

## 3. Verified run results

```
20/20 clauses processed        status: complete
decided_by      rule 20  |  model 14  |  human 8
llm             14 calls, 4,616 tokens, $0.0206, 0 errors
severity        high 3  |  medium 6  |  low 2  |  none 9
audit           42 records, 2 overrides, 1 escalation, 1 learning proposal
review_pending  2 (high severity, unreviewed)
budget          2.3% of tokens, 2.1% of cost
tests           65 across both phases
```

**`rule 20 / model 14` is the number to look at.** More than half of all agent
decisions were made deterministically, at zero cost, with a policy citation
attached. That is the cost lever and the audit story in one figure.

**Named findings.** 18 rule findings across 11 clauses, each citing a standard.
Examples: `CLZ-2025-0018` 2x liability cap vs the 1x standard (high);
`CLZ-2025-0004` 60-day payment and unilateral withholding (medium + high);
`CLZ-2025-0001` indemnity covering the Owner's own negligence (high), with
`CLZ-2025-0007` and `CLZ-2025-0013` cited as accepted alternatives — the latter
being the amendment that actually replaced it.

**The HITL loop, end to end.** Two overrides on the same standard
(`indemnification.duty_to_defend_permitted`) → one learning proposal,
`PENDING_HUMAN_APPROVAL` → `apply_proposal.py --approved-by "K. Chen"` bumps
`firm_standards.json` 1.0.0 → 1.1.0, appends to `version_history`, writes
`approvals.jsonl`, and appends an `apply_learning_proposal` record to the ledger.
Nothing else in the system can do that.

---

## 4. Observability reference — input for the Phase 3 runbook

### Spans

```
agents.run
  ├─ agents.precedent_index
  ├─ agents.process_clause     (per clause; attrs: clause.id)
  ├─ agents.human_review
  ├─ agents.learning
  └─ agents.write_gold
approval.apply                 (only when a human applies a proposal)
```

### Log events

| Severity | Event | Meaning | Normal? |
|---|---|---|---|
| INFO | `run.start`, `run.complete` | lifecycle | yes |
| INFO | `agents.precedent_index_built` | clean clauses available as alternatives | yes |
| INFO | `run.rehydrated` | resumed and replayed the ledger | only after a halt |
| DEBUG | `llm.request` / `llm.response` | one model call, with tokens + cost | yes |
| DEBUG | `audit.recorded` | one ledger entry | yes, 2+ per clause |
| DEBUG | `risk.duplicate_finding_dropped` | model repeated a rule finding | occasional |
| WARN | `budget.warning` | 80% of budget consumed | **watch** |
| WARN | `audit.escalated` | a clause needs a human | **queue depth matters** |
| WARN | `audit.human_override` | a reviewer disagreed | expected; a spike is a signal |
| WARN | `audit.learning_proposals` | a proposal is waiting | **action required** |
| WARN | `config.changed_by_human` | **system behaviour changed** | rare, always deliberate |
| WARN | `llm.parse_failure` | malformed model output, retrying | occasional; a rise means prompt drift |
| ERROR | `run.halted` | a guardrail stopped the run | **page someone** |
| ERROR | `run.halt_unacknowledged` | resume refused | expected after a halt |
| ERROR | `llm.error` | provider failure | **page if sustained** |
| ERROR | `run.failed` | unhandled | **page someone** |

### Metrics

| Metric | Healthy |
|---|---|
| `llm.calls_total`, `llm.input_tokens_total`, `llm.output_tokens_total`, `llm.cost_usd_total` | flat per run; a jump without more clauses means prompt bloat |
| `llm.latency_ms` (+ per-agent) | p95 stable |
| `llm.errors_total`, `llm.parse_failures_total` | **0** |
| `llm.cache_hits_total` / `cache_misses_total` | hit rate rises on re-runs |
| `classifier.decided_by_rule_total` vs `..._model_total` | rule share should not fall — a drop means cost rising for no gain |
| `risk.rule_findings_total` | tracks corpus risk |
| `risk.model_pass_skipped_total` | gating is working |
| `audit.escalations_total` (+ by reason) | **queue must be worked, not just grown** |
| `audit.human_overrides_total` | a spike on one standard is the learning signal |
| `audit.learning_proposals_total` | rare; each needs a human |
| `run.halts_total` | **0** in steady state |

### Exit codes

| Code | Meaning | Orchestrator |
|---|---|---|
| 0 | complete | continue |
| 1 | failed | alert |
| 2 | **halted by a guardrail** — data written, all clauses accounted for | hold; human must `--acknowledge-halt` |
| 3 | complete, escalations open | continue, but the queue needs working |

---

## 5. Assumptions

1. LLM calls are stubbed. The stub is deterministic and populates
   `usage_metadata` exactly as `ChatAnthropic` does, so budget arithmetic,
   callbacks, caching and audit records are production-real.
2. Firm standards are calibrated from the corpus and from J. Martinez's review
   note on CLZ-2025-0018 ("2x exceeds firm standard — cap at 1x"). In a real
   engagement they come from the contract playbook.
3. Human overrides are replayed from `review_history` where it exists (batch 2)
   and simulated where it does not (batch 1 has no such field). Every record
   carries its `source`.
4. Single-threaded by design — determinism over throughput.
5. Rules are regex extractors over English legal prose; limits are listed in
   `rules.py`. They are not a substitute for legal review.

---

## 6. Decisions made *during* implementation

**C-9 — The rate limiter is not a no-op against the stub.** I claimed it would
be. It genuinely throttles (2 req/sec × 17 calls), which pushed the test suite
past two minutes. Tests raise the ceiling and one test asserts the limiter
actually delays. Worth stating plainly: it sits in the real call path, which is
the point of wiring it in.

**C-10 — Nearest number, not first.** `CLZ-2025-0015` states 30 days for cause
and 21 for convenience. The first regex match in the window returned the cause
period and inverted the finding. Now the nearest match to the anchor wins.

**C-11 — A halt left later clauses with no register entry at all.** Entries were
built inside the loop, so a `break` meant clauses after it were never created —
a `KeyError`, and worse, the silent-drop failure the guardrails exist to
prevent, reintroduced by the guardrail. Entries are now created for all clauses
up front.

**C-12 — Resume lost the findings of already-completed clauses.** The checkpoint
knows *which* clauses are done, not *what was decided*. Resumed clauses
reappeared as `not_processed` with empty findings. Fixed by replaying the
append-only ledger (`rehydrate_from_ledger`). **This is the practical argument
for an append-only ledger: it is the recovery mechanism, not only an audit
artifact.** Guarded by `test_resume_preserves_earlier_findings`.

**C-13 — `review_history` overrides needed a target.** They defaulted to
`risk.overall_severity`, so two overrides of different standards looked like one
generic pattern and no proposal fired. Now the overridden field is inferred from
the highest-severity finding that cites a standard — two reviewers overriding
`duty_to_defend_permitted` is a pattern; two overriding "severity" for unrelated
reasons is noise.

**C-14 — The model repeated findings the rules already raised**, despite the
prompt saying not to. A prompt instruction is a request, not a guarantee — true
of real models too. Duplicates are now dropped on `standard_reference`, with the
rule version winning.

---

## 7. Not yet done

| Deliverable | Status | Source |
|---|---|---|
| runbook monitoring section | done | §4 here + §4 of the Phase 1 record |
| `runbook.md` — agent failure | not started | escalation path, `--acknowledge-halt`, ledger rehydration, exit codes |
| `runbook.md` — rollback | partial | Phase 1 primitives + `firm_standards.version_history` + `approvals.jsonl` |
| `buy_vs_build.md` | not started | the checkpoint protocol (Phase 1) or LangChain adoption (Phase 2) — pick one |
| `README.md` all-phase rewrite | Phase 1 only | both completion records |
| `architecture_note.md` **1 page** | 10 ADRs + Phase 2 pending | must be condensed; keep the long version as an appendix |

Phase 2 ADRs still to be written up in `architecture_note.md`: LangChain for the
swap seam but not LangSmith · rules-before-model · firm standards as config ·
two HITL gates · cache keyed on prompt version · ledger as recovery mechanism.

## 8. Deferred

| Item | Why | Revisit when |
|---|---|---|
| Vector store for precedents | 20 clauses; a dict beats embeddings and is inspectable | thousands of clauses |
| Parallel clause processing | determinism over throughput | batch size makes wall-clock hurt |
| LangGraph / tool-calling agents | control flow is linear; a graph would be ceremony | genuinely branching workflows |
| Streaming | nothing consumes tokens incrementally | a UI needs progressive output |
| Real `with_structured_output` | our stub returns JSON directly; the schemas are already pydantic | switching to a real provider |
| Prompt A/B evaluation | needs labelled ground truth we do not have | a gold-standard review set exists |
