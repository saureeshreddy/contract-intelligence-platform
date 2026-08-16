#!/usr/bin/env python3
"""
phase2_agents/run.py
====================
Phase 2 orchestrator. Reads Silver, runs the agents, writes the ledger and the
Gold risk register.

ORDER, AND WHY
--------------
  0. deterministic sweep   rules over every clause, no model calls. Cheap, and
                           it builds the precedent index the risk agent needs
                           (clauses of the same category that breach nothing).
  1. per clause:
        guardrails         kill switch, then budget. Before any work.
        scope              refuse and escalate rather than guess.
        classify           rule first; model only for the ambiguous middle.
        gate               skip the expensive pass for non-risk categories.
        flag risk          rules + model residue.
        checkpoint         after each clause, so a halt is resumable.
  2. human reviews         replayed from review_history + simulated reviews.
  3. learning proposals    written, never applied.
  4. Gold register         effective view, with provenance back to Bronze.

WHAT A HALT LOOKS LIKE
----------------------
Budget exhausted or kill switch tripped -> processing STOPS. Every unprocessed
clause is still written to the register as `not_processed`. An operator can
always account for all 20 clauses; silent truncation is the failure these
controls exist to prevent, so they must not cause it.

Resuming after a halt requires --acknowledge-halt "<reason>", recorded in the
ledger. If the kill switch fired on an error rate, re-running blind is exactly
the wrong response.

USAGE
  python phase2_agents/run.py
  python phase2_agents/run.py --max-cost-usd 0.001        # force a budget halt
  python phase2_agents/run.py --acknowledge-halt "reviewed parse errors, safe"
  python phase2_agents/run.py --restart

EXIT CODES
  0  complete
  1  failed
  2  halted by a guardrail (data written; a human must look)
  3  complete, but escalations are open
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.observability import Telemetry, default_log_dir, utc_now  # noqa: E402
from phase2_agents.agents.auditor import AuditorAgent  # noqa: E402
from phase2_agents.agents.base import PromptTemplate  # noqa: E402
from phase2_agents.agents.classifier import SOURCE_CATEGORY_MAP, ClassifierAgent  # noqa: E402
from phase2_agents.agents.risk_flagger import RiskFlaggerAgent  # noqa: E402
from phase2_agents.callbacks import TelemetryCallbackHandler  # noqa: E402
from phase2_agents.guardrails import Guardrails, HaltProcessing  # noqa: E402
from phase2_agents.llm import DecisionCache, build_model  # noqa: E402
from phase2_agents.models import (  # noqa: E402
    ClauseCategory,
    ClauseRiskEntry,
    DecidedBy,
    DecisionStatus,
    RiskFinding,
    Severity,
    TokenUsage,
)
from phase2_agents.rules import evaluate as evaluate_rules  # noqa: E402

PHASE_DIR = ROOT / "phase2_agents"
CONFIG_DIR = PHASE_DIR / "config"
OUTPUT_DIR = PHASE_DIR / "output"
LOG_DIR = default_log_dir()  # repo-root output/logs/ -- telemetry is cross-phase

CONTRACT_VERSION = "1.0.0"


def load_config() -> Dict[str, Any]:
    import yaml  # PyYAML arrives with langchain; see AD-11

    return yaml.safe_load((CONFIG_DIR / "agents.yml").read_text(encoding="utf-8"))


def load_silver(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        raise SystemExit(
            f"Silver dataset not found at {path}.\n"
            f"Run Phase 1 first:  python run.py phase1\n"
            f"(or point input.silver at data/clauses_ingested_fallback.json)"
        )
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return json.loads(path.read_text(encoding="utf-8"))["clauses"]


def load_supersessions(path: Path) -> Dict[str, str]:
    """Phase 1's proposals: which clauses are probably no longer in force.

    Without this the risk agent confidently flags CLZ-2025-0001, a clause the
    legal team replaced a month ago via CLZ-2025-0013. Still a proposal, so it
    annotates rather than suppresses.
    """
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        p["superseded_clause_id"]: p["superseding_clause_id"]
        for p in payload.get("proposals", [])
        if p.get("kind") == "SUPERSESSION_CANDIDATE"
    }


def rehydrate_from_ledger(
    entries: Dict[str, "ClauseRiskEntry"],
    audit_path: Path,
    done: set,
) -> Dict[str, Any]:
    """Rebuild the state of already-completed clauses from the audit log.

    WHY THE LEDGER AND NOT THE CHECKPOINT
    The checkpoint records *which* clauses finished. It does not record *what
    was decided* -- and it should not, because that would duplicate the ledger
    and give us two sources of truth that can disagree.

    So on resume we replay the append-only ledger, which already holds every
    decision with its clause id, action and effective value. Without this, a
    clause processed before a budget halt reappears in the register as
    `not_processed` with its findings silently dropped -- the exact
    failure mode the guardrails exist to prevent, reintroduced by the recovery
    path. (It shipped that way once; `test_resume_preserves_earlier_findings`
    guards it now.)

    This is the practical argument for an append-only ledger: it is not only
    an audit artifact, it is the recovery mechanism.
    """
    risk_records: Dict[str, Any] = {}
    if not audit_path.exists() or not done:
        return risk_records

    for line in audit_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        clause_id = record.get("clause_id")
        if clause_id not in done or clause_id not in entries:
            continue
        entry = entries[clause_id]
        value = record.get("effective_value") or {}

        # Values come back from JSON as plain strings and dicts. They must be
        # coerced to their model types, not assigned raw: pydantic will accept
        # the assignment and then emit serializer warnings when the register is
        # written, and downstream code comparing to an enum silently fails.
        if record.get("action") == "classify_clause":
            if value.get("category"):
                entry.review_category = ClauseCategory(value["category"])
                entry.classification_confidence = value.get("confidence")
                entry.classification_decided_by = DecidedBy(record["decided_by"])
            entry.status = DecisionStatus(record["status"])
        elif record.get("action") == "assess_risk":
            entry.findings = [RiskFinding.model_validate(f) for f in value.get("findings", [])]
            entry.overall_severity = Severity(value.get("overall_severity", "none"))
            if entry.status != DecisionStatus.ESCALATED:
                entry.status = DecisionStatus(record["status"])
            risk_records[clause_id] = _AuditRecordView(record)
    return risk_records


class _AuditRecordView:
    """Attribute access over a ledger row, so replayed records behave like the
    in-memory AuditRecord the human-review step expects."""

    def __init__(self, row: Dict[str, Any]) -> None:
        self._row = row
        self.agent = row.get("agent", "")
        self.model_output = row.get("model_output")
        self.effective_value = row.get("effective_value")
        self.status = DecisionStatus(row.get("status", "proposed"))


def checkpoint_write(path: Path, data: Dict[str, Any]) -> None:
    data["updated_at"] = utc_now()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Phase 2 multi-agent contract review.")
    parser.add_argument("--max-cost-usd", type=float, help="Override the configured cost budget.")
    parser.add_argument("--max-total-tokens", type=int, help="Override the configured token budget.")
    parser.add_argument(
        "--acknowledge-halt",
        metavar="REASON",
        help="Required to resume after a guardrail halt. Recorded in the audit log.",
    )
    parser.add_argument("--restart", action="store_true", help="Ignore the checkpoint and start over.")
    parser.add_argument("--no-cache", action="store_true", help="Bypass the decision cache.")
    args = parser.parse_args(argv)

    config = load_config()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    telemetry = Telemetry("contract-intelligence.agents", LOG_DIR)

    exit_code = 0
    try:
        with telemetry.span("agents.run") as root:
            root.set_attribute("telemetry.backend", telemetry.backend)

            # ---- inputs ----------------------------------------------------
            silver_path = ROOT / config["input"]["silver"]
            clauses = load_silver(silver_path)
            supersessions = load_supersessions(ROOT / config["input"]["supersession_proposals"])
            firm_standards = json.loads(
                (CONFIG_DIR / "firm_standards.json").read_text(encoding="utf-8")
            )
            run_id = f"agents__{utc_now().replace(':', '').replace('-', '')[:15]}"
            root.set_attribute("run.id", run_id)

            telemetry.info(
                "run.start",
                f"{len(clauses)} clause(s) from {silver_path.name}; "
                f"firm standards v{firm_standards['version']}; "
                f"model provider {config['model']['provider']}",
                run_id=run_id,
                clauses=len(clauses),
            )

            # ---- checkpoint ------------------------------------------------
            checkpoint_path = ROOT / config["output"]["checkpoint"]
            checkpoint = {}
            if checkpoint_path.exists() and not args.restart:
                checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            done: set = set(checkpoint.get("completed_clause_ids", []))

            if checkpoint.get("status") == "halted":
                required = (
                    config["guardrails"]["kill_switch"].get("require_acknowledgement_to_resume", True)
                )
                if required and not args.acknowledge_halt:
                    telemetry.error(
                        "run.halt_unacknowledged",
                        f"Previous run halted: {checkpoint.get('halt_detail')}. "
                        f"Re-run with --acknowledge-halt \"<reason>\" once you have looked at why. "
                        f"Resuming blind after an error-rate halt is exactly the wrong move.",
                    )
                    return 2
                telemetry.warn(
                    "run.halt_acknowledged",
                    f"Operator acknowledged the previous halt: {args.acknowledge_halt}",
                    previous_halt=checkpoint.get("halt_reason"),
                    acknowledgement=args.acknowledge_halt,
                )

            # ---- guardrails ------------------------------------------------
            guardrails = Guardrails.from_config(config, ROOT)
            if args.max_cost_usd is not None:
                guardrails.budget.max_cost_usd = args.max_cost_usd
            if args.max_total_tokens is not None:
                guardrails.budget.max_total_tokens = args.max_total_tokens

            # ---- model + agents --------------------------------------------
            model_config = config["model"]
            pricing = config["pricing"].get(model_config["name"], {})
            model = build_model(model_config)
            handler = TelemetryCallbackHandler(
                telemetry, pricing, model_name=f"{model_config['provider']}:{model_config['name']}"
            )
            cache = DecisionCache(
                ROOT / config["cache"]["path"],
                enabled=config["cache"]["enabled"] and not args.no_cache,
            )

            classifier_config = config["agents"]["classifier"]
            risk_config = config["agents"]["risk_flagger"]
            classifier = ClassifierAgent(
                model,
                PromptTemplate.load(classifier_config["prompt_version"]),
                telemetry,
                handler,
                cache,
                pricing,
                classifier_config,
            )
            risk_flagger = RiskFlaggerAgent(
                model,
                PromptTemplate.load(risk_config["prompt_version"]),
                telemetry,
                handler,
                cache,
                pricing,
                risk_config,
                firm_standards=firm_standards,
            )
            auditor = AuditorAgent(
                telemetry,
                run_id,
                OUTPUT_DIR,
                min_occurrences_for_proposal=config["agents"]["auditor"]["min_occurrences_for_proposal"],
            )

            # ---- 0. deterministic sweep -> precedent index -----------------
            with telemetry.span("agents.precedent_index"):
                clean_by_category: Dict[str, List[str]] = {}
                for clause in clauses:
                    category = SOURCE_CATEGORY_MAP.get(clause.get("clause_category"), "other")
                    if not evaluate_rules(category, clause.get("clause_text", ""), firm_standards):
                        clean_by_category.setdefault(category, []).append(clause["clause_id"])
                risk_flagger.set_precedent_index(clean_by_category)
                telemetry.info(
                    "agents.precedent_index_built",
                    "Clauses breaching no firm standard, usable as suggested alternatives: "
                    + ", ".join(f"{k}={len(v)}" for k, v in sorted(clean_by_category.items())),
                )

            # ---- 1. per clause ---------------------------------------------
            # Every clause gets a register entry BEFORE any processing, so a
            # halt partway through still accounts for all 20. Building these
            # inside the loop would leave the clauses after the break with no
            # entry at all -- which is the silent-drop failure these guardrails
            # exist to prevent.
            entries: Dict[str, ClauseRiskEntry] = {
                clause["clause_id"]: ClauseRiskEntry(
                    clause_id=clause["clause_id"],
                    contract_id=clause.get("contract_id", ""),
                    client_name=clause.get("client_name", ""),
                    section_ref=clause.get("section_ref", ""),
                    source_category=clause.get("clause_category"),
                    superseded_by=supersessions.get(clause["clause_id"]),
                    lineage=clause.get("_lineage"),
                    status=DecisionStatus.NOT_PROCESSED,
                )
                for clause in clauses
            }
            classification_records: Dict[str, Any] = {}
            # Replay the ledger so clauses finished before a halt keep their
            # findings instead of reappearing as not_processed.
            risk_records: Dict[str, Any] = rehydrate_from_ledger(
                entries, OUTPUT_DIR / "audit_log.jsonl", done
            )
            if done:
                telemetry.info(
                    "run.rehydrated",
                    f"Restored {len(risk_records)} clause result(s) from the audit ledger; "
                    f"{len(done)} clause(s) will be skipped.",
                    restored=len(risk_records),
                    skipped=len(done),
                )
            halted: Optional[HaltProcessing] = None

            for clause in clauses:
                clause_id = clause["clause_id"]
                entry = entries[clause_id]

                if clause_id in done:
                    telemetry.log("DEBUG", "clause.skipped_resume", f"{clause_id} already done")
                    continue

                try:
                    guardrails.check_all(telemetry)
                except HaltProcessing as halt:
                    halted = halt
                    break

                with telemetry.span("agents.process_clause", **{"clause.id": clause_id}):
                    # scope
                    in_scope, reason, detail = guardrails.scope.check_clause(clause)
                    if not in_scope:
                        auditor.escalate(
                            clause_id=clause_id, agent="scope_guard", reason=reason or "out_of_scope",
                            detail=detail or "",
                        )
                        entry.status = DecisionStatus.ESCALATED
                        guardrails.kill_switch.record_success()
                        continue

                    # classify
                    classification = classifier.classify(clause)
                    record = classifier.audit(
                        clause=clause,
                        action="classify_clause",
                        result=classification,
                        run_id=run_id,
                        firm_standards_version=firm_standards["version"],
                    )
                    auditor.record(record)
                    classification_records[clause_id] = record
                    guardrails.budget.record(classification.usage)

                    if classification.output is None:
                        auditor.escalate(
                            clause_id=clause_id, agent=classifier.name,
                            reason=classification.escalation_reason or "classification_failed",
                            detail=classification.detail or "",
                        )
                        entry.status = DecisionStatus.ESCALATED
                        guardrails.kill_switch.record_failure()
                        continue

                    entry.review_category = classification.output.category
                    entry.classification_confidence = classification.output.confidence
                    entry.classification_decided_by = classification.decided_by
                    if classification.status == DecisionStatus.ESCALATED:
                        auditor.escalate(
                            clause_id=clause_id, agent=classifier.name,
                            reason=classification.escalation_reason or "low_confidence",
                            detail=classification.detail or "",
                            model_output=classification.output.model_dump(mode="json"),
                        )
                        entry.status = DecisionStatus.ESCALATED

                    # risk
                    category = classification.output.category.value
                    assessment = risk_flagger.assess(clause, category)
                    risk_record = risk_flagger.audit(
                        clause=clause,
                        action="assess_risk",
                        result=assessment,
                        run_id=run_id,
                        firm_standards_version=firm_standards["version"],
                    )
                    auditor.record(risk_record)
                    risk_records[clause_id] = risk_record
                    guardrails.budget.record(assessment.usage)

                    if assessment.output is not None:
                        entry.findings = assessment.output.findings
                        entry.overall_severity = assessment.output.overall_severity
                        if risk_flagger.requires_review(assessment.output):
                            entry.review_status = "pending"
                        if assessment.status == DecisionStatus.ESCALATED:
                            auditor.escalate(
                                clause_id=clause_id, agent=risk_flagger.name,
                                reason=assessment.escalation_reason or "low_confidence",
                                detail=assessment.detail or "",
                            )
                            entry.status = DecisionStatus.ESCALATED

                    if entry.status != DecisionStatus.ESCALATED:
                        entry.status = DecisionStatus.PROPOSED
                    guardrails.kill_switch.record_success()
                    done.add(clause_id)
                    checkpoint_write(
                        checkpoint_path,
                        {"run_id": run_id, "status": "in_progress", "completed_clause_ids": sorted(done)},
                    )

            # ---- halt bookkeeping ------------------------------------------
            if halted is not None:
                unprocessed = [c["clause_id"] for c in clauses if c["clause_id"] not in done]
                for clause_id in unprocessed:
                    entries[clause_id].status = DecisionStatus.NOT_PROCESSED
                telemetry.error(
                    "run.halted",
                    f"{halted.reason.value}: {halted.detail} "
                    f"{len(unprocessed)} clause(s) marked not_processed -- nothing was silently dropped.",
                    halt_reason=halted.reason.value,
                    unprocessed=len(unprocessed),
                )
                telemetry.count("run.halts_total")
                checkpoint_write(
                    checkpoint_path,
                    {
                        "run_id": run_id, "status": "halted",
                        "halt_reason": halted.reason.value, "halt_detail": halted.detail,
                        "completed_clause_ids": sorted(done),
                    },
                )
                exit_code = 2

            # ---- 2. human reviews (Gate A) ---------------------------------
            with telemetry.span("agents.human_review"):
                reviews = auditor.load_human_reviews(
                    clauses,
                    CONFIG_DIR / "simulated_reviews.json"
                    if config["human_in_the_loop"].get("simulate_overrides_from_review_history")
                    else None,
                )
                for review in reviews:
                    record = risk_records.get(review["clause_id"])
                    if record is None:
                        continue
                    override = auditor.apply_review(review, record)
                    if override is not None:
                        entry = entries[review["clause_id"]]
                        entry.human_overrides.append(override.override_id)
                        entry.status = DecisionStatus.OVERRIDDEN
                        entry.overall_severity = Severity.LOW
                        entry.review_status = "resolved_by_override"

            # ---- 3. learning (Gate B: proposals only) ----------------------
            with telemetry.span("agents.learning"):
                proposals = auditor.propose_learning(firm_standards)

            # ---- 4. Gold ----------------------------------------------------
            with telemetry.span("agents.write_gold"):
                gold_path = ROOT / config["output"]["gold_register"]
                gold_path.parent.mkdir(parents=True, exist_ok=True)
                register = sorted(entries.values(), key=lambda e: e.clause_id)
                gold_path.write_text(
                    json.dumps(
                        {
                            "contract_version": CONTRACT_VERSION,
                            "run_id": run_id,
                            "generated_at": utc_now(),
                            "firm_standards_version": firm_standards["version"],
                            "clause_count": len(register),
                            "note": (
                                "Effective view after human review. Every entry traces to Bronze "
                                "via `lineage`. `review_status: pending` means high severity that "
                                "no human has signed off. `superseded_by` means Phase 1 believes "
                                "the clause is no longer in force."
                            ),
                            "clauses": [e.model_dump(mode="json") for e in register],
                        },
                        indent=2, ensure_ascii=False,
                    )
                    + "\n",
                    encoding="utf-8",
                )

            # ---- summary ----------------------------------------------------
            audit_summary = auditor.flush(proposals)
            cache.flush()
            severity_counts: Dict[str, int] = {}
            for entry in entries.values():
                severity_counts[entry.overall_severity.value] = (
                    severity_counts.get(entry.overall_severity.value, 0) + 1
                )
            decided_by_counts: Dict[str, int] = {}
            for record in auditor.records:
                decided_by_counts[record.decided_by.value] = (
                    decided_by_counts.get(record.decided_by.value, 0) + 1
                )

            summary = {
                "run_id": run_id,
                "generated_at": utc_now(),
                "status": "halted" if halted else "complete",
                "halt_reason": halted.reason.value if halted else None,
                "clauses_total": len(clauses),
                "clauses_processed": len(done),
                "clauses_not_processed": len(clauses) - len(done),
                "model": {
                    "provider": model_config["provider"],
                    "name": model_config["name"],
                    "prompt_versions": {
                        "classifier": classifier.prompt.version,
                        "risk_flagger": risk_flagger.prompt.version,
                    },
                },
                "llm": handler.summary(),
                "cache": {"hits": cache.hits, "misses": cache.misses, "hit_rate": cache.hit_rate},
                "decided_by": decided_by_counts,
                "severity": severity_counts,
                "guardrails": guardrails.snapshot(),
                "audit": audit_summary,
                "review_pending": sum(1 for e in entries.values() if e.review_status == "pending"),
            }
            (ROOT / config["output"]["run_summary"]).write_text(
                json.dumps(summary, indent=2) + "\n", encoding="utf-8"
            )

            if not halted:
                checkpoint_write(
                    checkpoint_path,
                    {"run_id": run_id, "status": "complete", "completed_clause_ids": sorted(done)},
                )
                telemetry.info(
                    "run.complete",
                    f"{len(done)}/{len(clauses)} clauses; "
                    f"{audit_summary['audit_records']} audit records; "
                    f"{audit_summary['human_overrides']} override(s); "
                    f"{audit_summary['escalations']} escalation(s); "
                    f"{audit_summary['learning_proposals']} proposal(s); "
                    f"${handler.total_cost_usd:.6f}",
                )
                if auditor.escalations:
                    exit_code = 3

    except SystemExit as exc:
        telemetry.error("run.failed", str(exc))
        return int(exc.code or 1)
    except Exception as exc:  # noqa: BLE001
        telemetry.error("run.failed", f"{type(exc).__name__}: {exc}")
        telemetry.count("run.failures_total")
        exit_code = 1
    finally:
        telemetry.shutdown()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
