# Runbook

Operating this pipeline. Written for someone on call who did not build it.

**Scope:** the three situations that actually happen — monitoring, agent
failure, rollback. Full event and metric catalogues are in
[`engineering_log_ingestion.md` §4](engineering_log_ingestion.md) and
[`engineering_log_agents.md` §4](engineering_log_agents.md).

**Everything you need is in three places:**

```
output/logs/pipeline.jsonl                     what happened      (alert on `event`)
output/logs/metrics.jsonl                      how much           (one line per process)
phase2_agents/output/run_summary.json          the whole run at a glance
```

---

## 1. Monitoring

### Start here

`run_summary.json` answers "is this run healthy?" in ten seconds:

```json
{ "status": "complete", "clauses_processed": 20, "clauses_not_processed": 0,
  "decided_by": {"rule": 20, "model": 14, "human": 8},
  "llm": {"llm_calls": 14, "cost_usd": 0.0206, "llm_errors": 0},
  "audit": {"escalations": 1, "learning_proposals": 1},
  "review_pending": 2 }
```

### Baselines, from the committed run

| Signal | Normal | Concerning | Why |
|---|---|---|---|
| `clauses_not_processed` | 0 | **> 0** | a guardrail stopped us; work is outstanding |
| `llm.cost_usd` | ~$0.02 / 20 clauses | **> 2× per-clause baseline** | prompt bloat, retry storm, or a model change |
| `llm.errors_total` | 0 | **any sustained** | provider trouble |
| `llm.parse_failures_total` | 0 | **> 5% of calls** | prompt drift or a model upgrade changed output shape |
| `decided_by.rule` share | ≥ 50% | **falling** | cost rising with no accuracy gain — check whether a rule silently stopped matching |
| `cache.hit_rate` | rises on re-runs | 0 on a re-run | cache path wrong, or prompt version churning |
| `audit.escalations_total` | small, and **worked down** | **growing** | nobody is servicing the queue; the control is decorative |
| `audit.human_overrides_total` | steady | **spike on one standard** | the standard is wrong, not the clauses — expect a learning proposal |
| `review_pending` | trends to 0 | **flat and rising** | high-severity findings nobody has signed off |
| `run.halts_total` | 0 | **any** | investigate before resuming |
| `drift.events_total` (Phase 1) | 0 | **any BREAKING** | upstream schema changed |
| `ingest.replay_duplicates_total` | 0 | **> 0** | ingestion crashed and replayed (expected only after a crash) |

### Alert routing

Alerts key off the stable `event` field, never the human-readable message.

| Route | Events | Response |
|---|---|---|
| **Page** (on-call, 24/7) | `run.halted` · `run.failed` · `normalize.clause_id_collision` · `drift.field_removed` at BREAKING · sustained `llm.error` | data or spend is at risk; act now |
| **Ticket** (next business day) | `drift.suspected_rename` · `normalize.unmapped_fields` · `audit.learning_proposals` · `supersede.*` | a human decision is required; nothing is broken |
| **Queue** (owner: contract review team, daily) | `audit.escalated` | clauses waiting on a person |
| **Dashboard only** | `budget.warning` · `llm.parse_failure` · `audit.human_override` · `risk.duplicate_finding_dropped` | watch the trend, not the instance |
| **Deliberately loud, never automatic** | `config.changed_by_human` | system behaviour changed — should be rare and always traceable to an approval |

Escalations and learning proposals are the two queues that can rot quietly.
Both are *counted* in `run_summary.json` precisely so their depth can be
alerted on. **An escalation queue nobody works is worse than no escalation
path**, because it looks like a control while being a no-op.

### Weekly

- Run `python eval/evaluate.py` and diff `eval/output/eval_report.md`. Movement in
  classification accuracy or risk F1 is a regression signal.
- Review `learning_proposals.json`. A proposal sitting for weeks means the
  feedback loop is open at the human end.
- Check the override rate per standard. A standard overridden most times it
  fires is training reviewers to ignore the flag.

---

## 2. An agent fails mid-workflow

### What actually happens

Failures are contained by design. In descending order of severity:

| Failure | Behaviour | Blast radius |
|---|---|---|
| Model returns unparseable output | one reworded retry, then **escalate** | one clause |
| Model returns unparseable output in the risk agent | **rule findings are kept**, clause escalated | one clause, findings preserved |
| 3 consecutive failures | **kill switch trips**, run halts | run stops, prior clauses safe |
| Failure rate > 25% (min 8 records) | kill switch trips | ″ |
| Budget exhausted | run halts, remainder marked `not_processed` | ″ |
| Provider 429 / 5xx | rate limiter waits; `.with_retry` backs off | usually invisible |
| Process killed outright | checkpoint + ledger survive | resume from the last committed clause |

### Who gets alerted

- `run.halted` / `run.failed` → **page the on-call data engineer.**
- Escalations → **the contract review team's queue**, not on-call. A clause needing a lawyer is not an incident.
- `config.changed_by_human` → **notify the standards owner** (`firm_standards.json` `owner:` field, currently `legal_counsel`).

### What happens to in-flight data

**Nothing is lost, and nothing is half-written.**

- The audit ledger is appended **per decision**, flushed immediately — never buffered. A process killed mid-run still holds every decision made up to that instant.
- `_checkpoint.json` is written after each clause via atomic rename, so it is never torn.
- The clause being processed when the failure hit is simply **not** in the checkpoint. It is re-processed on resume. At-least-once, deduplicated downstream.
- Phase 1 has the same shape: Bronze is at-least-once, Silver exactly-once, `record_hash` is the dedupe key.

### Recovery

```bash
# 1. What stopped it?
python -c "import json;c=json.load(open('phase2_agents/output/_checkpoint.json'));print(c['status'], c.get('halt_reason'), c.get('halt_detail'))"

# 2. Read the last few events
tail -20 output/logs/pipeline.jsonl

# 3. Fix the cause. Then resume -- acknowledgement is REQUIRED.
python phase2_agents/run.py --acknowledge-halt "Investigated: provider outage, resolved 14:20"
```

Resuming without `--acknowledge-halt` is **refused** (exit 2). This is deliberate:
if the kill switch tripped on an error rate, re-running blind is exactly the
wrong response. The acknowledgement text is written into the audit log.

On resume the run replays the append-only ledger to restore what was already
decided (`run.rehydrated`), so completed clauses keep their findings instead of
reappearing as `not_processed`. **The ledger is the recovery mechanism, not just
an audit artifact.**

### Stopping it yourself

```bash
touch phase2_agents/output/STOP      # halts at the next clause boundary
touch phase1_ingestion/output/STOP   # same mechanism during ingestion
```

Both stop on a clean record boundary, not mid-write.

---

## 3. Rollback

### What is safe to roll back, and how

Every layer is derived from the one below it, so rollback is re-derivation
rather than data surgery. **Nothing is ever deleted.**

| Layer | Rollback | Cost |
|---|---|---|
| Gold register | re-run `phase2_agents/run.py --restart` | seconds |
| Agent decisions | replay the ledger; it is append-only, so history is intact | none |
| Firm standards | revert to a prior `version_history` entry, re-run | seconds |
| Silver | `python phase1_ingestion/normalize.py` — rebuilt from Bronze every time | seconds |
| A bad Bronze batch | move its `run_id=…` directory aside, re-run `normalize.py` | seconds |
| Bronze itself | **never rolled back.** Immutable once `complete`; it is the system of record. |

### Scenario A — a bad standards change went out

Most likely cause: a learning proposal was approved that should not have been.

```bash
# 1. Who changed what, when, and on what evidence?
cat phase2_agents/output/approvals.jsonl
python -c "import json;d=json.load(open('phase2_agents/config/firm_standards.json'));print(json.dumps(d['version_history'],indent=2))"

# 2. Revert the standards file to the previous version (git, or by hand from
#    version_history), then confirm which version is live:
git checkout HEAD~1 -- phase2_agents/config/firm_standards.json

# 3. Re-review under the reverted policy.
python phase2_agents/run.py --restart

# 4. Confirm: every audit record carries firm_standards_version, so you can
#    prove which policy produced which decision.
```

The old decisions are **not** erased — they stay in the ledger, stamped with
the version that produced them. That is the point of versioning the standards
file rather than editing it in place.

### Scenario B — a bad prompt went out

Prompt versions are files; which one is **active** is named in `agents.yml`.
Rollback is pointing `prompt_version` back at the previous file and re-running.
The decision cache is keyed on prompt version, so reverting cannot serve
decisions made by the newer prompt.

### Scenario C — upstream sent us bad data

1. Identify the run: `phase1_ingestion/output/bronze/ingest_date=*/run_id=*/_manifest.json` carries the source SHA-256.
2. Move that `run_id=…` directory to `bronze/_quarantine/` (do not delete — it is evidence).
3. `python phase1_ingestion/normalize.py` — Silver rebuilds without it.
4. `python phase2_agents/run.py --restart`.

### Verifying a rollback worked

```bash
python run.py test                        # 65 tests
python run.py eval                        # accuracy back to baseline?
```

`eval/output/eval_report.md` is the fastest confirmation that a rollback
restored behaviour rather than merely changing it.

---

## Quick reference

| Exit code | Meaning | Do |
|---|---|---|
| 0 | complete | nothing |
| 1 | failed | read `output/logs/pipeline.jsonl`, fix, re-run |
| 2 | **halted by a guardrail** — all clauses accounted for | investigate, then `--acknowledge-halt "<reason>"` |
| 3 | complete, escalations open | work the queue |

| Question | File |
|---|---|
| What changed upstream? | `phase1_ingestion/output/drift/drift_report.md` |
| What did the system decide, and who decided it? | `phase2_agents/output/audit_log.json` |
| What is waiting on a human? | `escalations.jsonl`, `learning_proposals.json` |
| What did it cost? | `run_summary.json` → `llm.cost_usd` |
| Who changed the rules? | `approvals.jsonl` + `firm_standards.json` → `version_history` |
| Is it still accurate? | `eval/output/eval_report.md` |
