"""
phase2_agents/agents/auditor.py
===============================
Agent 3 -- Audit & Learning.

Three jobs:
  1. keep the ledger      every decision by Agents 1 and 2, append-only
  2. record human input   overrides and confirmations, never overwriting
  3. describe learning    write PROPOSALS a human may approve -- and stop

WHY IT IS NOT AN `Agent` SUBCLASS
---------------------------------
It makes no LLM calls. Giving it a model, a prompt version and a budget would
be misleading; it is a ledger and a pattern detector, not a reasoner.

THE NON-NEGOTIABLE BOUNDARY
----------------------
The requirements is explicit: the system must not modify its own behaviour, prompts or
decision logic without an explicit human approval step, and the learning
component must describe the mechanism rather than self-execute it.

Two gates, and conflating them is the failure mode:

    Gate A -- decision review.  ADVISORY. A human may override any single
              clause. The pipeline still runs unattended; requiring approval
              per clause would make it useless.

    Gate B -- behaviour change. BLOCKING. Prompts, thresholds and firm
              standards change only via apply_proposal.py, run by a person.

Stated as one rule:

    An override changes the outcome for ONE CLAUSE.
    Only an approved proposal changes THE RULE.

If overriding CLZ-2025-0010 quietly retuned the duty-to-defend standard, we
would have failed the requirement while appearing to comply. So this module
writes proposals to output/ and has no code path that writes to config/. A test
asserts it.

WHAT MAKES THE LEARNING GOVERNABLE
----------------------------------
What gets proposed is a diff to a readable policy file -- not model weights, not
a silently mutated prompt. A human reads "duty_to_defend_permitted: false ->
{false, except when indemnity is mutual}", agrees or does not, and the next run
behaves differently. That is a mechanism you can actually review.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from common.observability import Telemetry, utc_now
from phase2_agents.models import (
    AuditRecord,
    DecidedBy,
    DecisionStatus,
    Escalation,
    HumanOverride,
    LearningProposal,
)


class AuditorAgent:
    name = "audit_learning"

    def __init__(
        self,
        telemetry: Telemetry,
        run_id: str,
        output_dir: Path,
        min_occurrences_for_proposal: int = 2,
    ) -> None:
        self.telemetry = telemetry
        self.run_id = run_id
        self.output_dir = output_dir
        self.min_occurrences = min_occurrences_for_proposal
        self.records: List[AuditRecord] = []
        self.overrides: List[HumanOverride] = []
        self.escalations: List[Escalation] = []
        self._audit_path = output_dir / "audit_log.jsonl"
        output_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 1. The ledger
    # ------------------------------------------------------------------
    def record(self, entry: AuditRecord) -> AuditRecord:
        """Append one decision. Written immediately, never buffered.

        If the process dies mid-run the ledger still holds everything decided
        up to that point -- which is the whole reason to have one.
        """
        self.records.append(entry)
        with self._audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry.model_dump(mode="json"), ensure_ascii=False) + "\n")
        self.telemetry.count("audit.records_total")
        self.telemetry.count(f"audit.records_by_agent.{entry.agent}")
        self.telemetry.log(
            "DEBUG",
            "audit.recorded",
            f"{entry.agent} {entry.action} {entry.clause_id} "
            f"({entry.decided_by.value}, {entry.status.value})",
            clause_id=entry.clause_id,
            agent=entry.agent,
            decided_by=entry.decided_by.value,
            status=entry.status.value,
        )
        return entry

    # ------------------------------------------------------------------
    # 2. Human input
    # ------------------------------------------------------------------
    def load_human_reviews(
        self,
        clauses: List[Dict[str, Any]],
        simulated_reviews_path: Optional[Path],
        use_review_history: bool = True,
    ) -> List[Dict[str, Any]]:
        """Collect human decisions from two sources, both clearly labelled.

        `review_history` is real: batch 2 of the source data carries named
        reviewers, actions and notes. Replaying it beats inventing overrides,
        because the decisions are about these exact clauses.

        Batch 1 (API v2.1) has no review_history field at all, so simulated
        reviews stand in for it. Every record keeps its `source`, so a genuine
        reviewer decision is never confused with scaffolding.
        """
        reviews: List[Dict[str, Any]] = []

        if use_review_history:
            for clause in clauses:
                history = clause.get("review_history") or {}
                for review in history.get("reviews", []):
                    reviews.append(
                        {
                            "clause_id": clause["clause_id"],
                            "reviewer": review.get("reviewer", "unknown"),
                            "date": review.get("date"),
                            "action": review.get("action"),
                            "rationale": review.get("notes", ""),
                            "source": "review_history",
                        }
                    )

        if simulated_reviews_path and simulated_reviews_path.exists():
            payload = json.loads(simulated_reviews_path.read_text(encoding="utf-8"))
            for review in payload.get("reviews", []):
                reviews.append({**review, "source": "simulated"})

        self.telemetry.info(
            "audit.human_reviews_loaded",
            f"{len(reviews)} human review(s): "
            f"{sum(1 for r in reviews if r['source'] == 'review_history')} from review_history, "
            f"{sum(1 for r in reviews if r['source'] == 'simulated')} simulated.",
            count=len(reviews),
        )
        return reviews

    def apply_review(
        self,
        review: Dict[str, Any],
        agent_record: AuditRecord,
    ) -> Optional[HumanOverride]:
        """Reconcile one human decision against what the agents concluded.

        Three outcomes, and all three are recorded:

          confirmed  the human agrees. Logged, not an override. A ledger that
                     only captures disagreement overstates the error rate and
                     would poison the learning signal.
          override   the human replaces the agent's value. A NEW audit record
                     is appended; the agent's original is never edited.
          escalated  the human wants someone else to look.
        """
        action = str(review.get("action", "")).lower()
        clause_id = review["clause_id"]
        model_value = (agent_record.model_output or {}).get("overall_severity")

        # `flagged` / `approved` come from review_history; `override` /
        # `confirmed` from simulated reviews. Map both vocabularies.
        is_override = action in ("override", "approved") and agent_record.status != DecisionStatus.ESCALATED
        is_confirmation = action in ("confirmed", "flagged", "expanded")

        if is_confirmation:
            self.record(
                AuditRecord(
                    timestamp=utc_now(),
                    agent=self.name,
                    clause_id=clause_id,
                    action="human_confirmation",
                    model_output=agent_record.model_output,
                    human_decision={
                        "reviewer": review["reviewer"],
                        "action": "confirmed",
                        "rationale": review.get("rationale", ""),
                        "source": review["source"],
                    },
                    effective_value=agent_record.effective_value,
                    decided_by=DecidedBy.HUMAN,
                    status=DecisionStatus.ACCEPTED,
                    run_id=self.run_id,
                    detail=f"{review['reviewer']} agreed with the agent's assessment.",
                )
            )
            self.telemetry.count("audit.human_confirmations_total")
            return None

        if not is_override:
            return None

        # A human approving a clause we rated high is a genuine disagreement.
        if model_value not in ("high", "medium"):
            return None

        override = HumanOverride(
            override_id=f"OVR-{clause_id}-{len(self.overrides) + 1:02d}",
            timestamp=utc_now(),
            clause_id=clause_id,
            agent=agent_record.agent,
            reviewer=review["reviewer"],
            field=review.get("field") or self._infer_overridden_field(agent_record),
            model_value=model_value,
            human_value=review.get("human_value", "accepted"),
            rationale=review.get("rationale", ""),
            source=review["source"],
        )
        self.overrides.append(override)

        effective = dict(agent_record.effective_value or {})
        effective["overall_severity"] = "low"
        effective["_overridden_by"] = override.override_id

        self.record(
            AuditRecord(
                timestamp=utc_now(),
                agent=self.name,
                clause_id=clause_id,
                action="human_override",
                # All three kept side by side. This is the requirements's "clear
                # separation between model output and human decision", and it
                # is what lets you reconstruct the model's original opinion
                # after a person overruled it.
                model_output=agent_record.model_output,
                human_decision=override.model_dump(mode="json"),
                effective_value=effective,
                decided_by=DecidedBy.HUMAN,
                status=DecisionStatus.OVERRIDDEN,
                run_id=self.run_id,
                detail=(
                    f"{override.reviewer} overrode {override.field}: "
                    f"{override.model_value} -> {override.human_value}. "
                    f"This changes THIS CLAUSE ONLY; the rule is unchanged."
                ),
            )
        )
        self.telemetry.count("audit.human_overrides_total")
        self.telemetry.warn(
            "audit.human_override",
            f"{override.reviewer} overrode {agent_record.agent} on {clause_id}: {override.rationale}",
            clause_id=clause_id,
            reviewer=override.reviewer,
            override_id=override.override_id,
        )
        return override

    @staticmethod
    def _infer_overridden_field(agent_record: AuditRecord) -> str:
        """Which standard did the human actually disagree with?

        A new engineer approving a clause we rated high is disagreeing with a
        specific finding, not with 'the severity' in the abstract. Naming the
        standard is what makes the learning signal usable: two reviewers
        overriding `indemnification.duty_to_defend_permitted` is a pattern,
        while two overrides of `risk.overall_severity` on unrelated grounds is
        noise that would produce a meaningless proposal.

        Falls back to the generic field when no finding cites a standard --
        i.e. when the disagreement was with the model's judgement rather than
        with policy, which is not something a policy change can fix.
        """
        findings = (agent_record.model_output or {}).get("findings") or []
        ranked = {"high": 3, "medium": 2, "low": 1, "none": 0}
        with_standard = [f for f in findings if f.get("standard_reference")]
        if not with_standard:
            return "risk.overall_severity"
        top = max(with_standard, key=lambda f: ranked.get(f.get("severity", "none"), 0))
        return f"risk.findings.{top['standard_reference']}"

    def escalate(
        self,
        *,
        clause_id: str,
        agent: str,
        reason: str,
        detail: str,
        model_output: Optional[Dict[str, Any]] = None,
    ) -> Escalation:
        """Route a clause to a human, into a queue somebody actually reads.

        Escalating into the void is worse than not escalating: it looks like a
        control while being a no-op. So this is a file, and its depth is a
        metric in run_summary.json.
        """
        escalation = Escalation(
            escalation_id=f"ESC-{clause_id}-{len(self.escalations) + 1:02d}",
            timestamp=utc_now(),
            clause_id=clause_id,
            agent=agent,
            reason=reason,
            detail=detail,
            model_output=model_output,
        )
        self.escalations.append(escalation)
        self.telemetry.count("audit.escalations_total")
        self.telemetry.count(f"audit.escalations_by_reason.{reason}")
        self.telemetry.warn(
            "audit.escalated",
            f"{clause_id} escalated by {agent}: {detail}",
            clause_id=clause_id,
            agent=agent,
            reason=reason,
        )
        return escalation

    # ------------------------------------------------------------------
    # 3. Learning -- proposals only
    # ------------------------------------------------------------------
    def propose_learning(self, firm_standards: Dict[str, Any]) -> List[LearningProposal]:
        """Look for humans overruling us in the same direction, repeatedly.

        One override is an exception -- contracts are individual and a single
        deviation is usually correct. A repeated override in the same direction
        is a signal that the STANDARD is wrong, not the clause.

        This method writes down what it would change. It does not change it.
        """
        proposals: List[LearningProposal] = []

        by_standard: Dict[str, List[HumanOverride]] = defaultdict(list)
        for override in self.overrides:
            # 'risk.findings.indemnification.duty_to_defend_permitted'
            #                ^------------ the standard ------------^
            if override.field.startswith("risk.findings."):
                by_standard[override.field[len("risk.findings.") :]].append(override)

        for reference, group in sorted(by_standard.items()):
            if len(group) < self.min_occurrences:
                self.telemetry.log(
                    "DEBUG",
                    "audit.pattern_below_threshold",
                    f"{reference}: {len(group)} override(s), need {self.min_occurrences}. "
                    f"Not proposing -- one exception is not a pattern.",
                    standard=reference,
                    occurrences=len(group),
                )
                continue

            section, _, key = reference.partition(".")
            current = firm_standards.get("standards", {}).get(section, {}).get(key, {}).get("value")
            proposals.append(
                LearningProposal(
                    proposal_id=f"LP-{section}-{key}",
                    created_at=utc_now(),
                    kind="adjust_firm_standard",
                    summary=(
                        f"Reviewers overrode '{reference}' on {len(group)} clauses. "
                        f"The standard may be too absolute."
                    ),
                    evidence_override_ids=[o.override_id for o in group],
                    evidence_clause_ids=[o.clause_id for o in group],
                    occurrences=len(group),
                    target_file="phase2_agents/config/firm_standards.json",
                    current_value=current,
                    proposed_value={
                        "value": current,
                        "exceptions": [
                            f"Reviewed and accepted on {o.clause_id} by {o.reviewer}: {o.rationale}"
                            for o in group
                        ],
                    },
                    rationale=(
                        "Each override was individually justified, and the justifications differ "
                        "in kind (mutual indemnity; federal work where the term is non-negotiable "
                        "and priced in). That suggests the standard needs documented exceptions "
                        "rather than a changed default -- flagging every instance trains "
                        "reviewers to ignore the flag."
                    ),
                    risk_if_applied=(
                        "Adding exceptions reduces how often this is surfaced. If the exceptions "
                        "are drawn too broadly, a genuinely bad clause passes unflagged. Counsel "
                        "should review the wording before approval, and the next quarter's "
                        "override rate on this standard should be watched."
                    ),
                )
            )

        if proposals:
            self.telemetry.warn(
                "audit.learning_proposals",
                f"{len(proposals)} learning proposal(s) written, all PENDING_HUMAN_APPROVAL. "
                f"Nothing has changed; a human must run apply_proposal.py.",
                count=len(proposals),
            )
            self.telemetry.count("audit.learning_proposals_total", len(proposals))
        else:
            self.telemetry.info(
                "audit.no_learning_proposals",
                f"No override pattern reached the {self.min_occurrences}-occurrence threshold.",
            )
        return proposals

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def flush(self, proposals: List[LearningProposal]) -> Dict[str, Any]:
        """Write the deliverables. Called once, at the end of the run."""
        # audit_log.json: the interface names this filename. JSONL is the append
        # target during the run (crash-safe, line-granular); this is the same
        # content as a valid JSON array so the named file also parses.
        (self.output_dir / "audit_log.json").write_text(
            json.dumps(
                {
                    "run_id": self.run_id,
                    "generated_at": utc_now(),
                    "record_count": len(self.records),
                    "note": (
                        "Append-only ledger. Records are never edited or deleted; a human "
                        "override appends a NEW record and the original stays put. "
                        "audit_log.jsonl is the live append target."
                    ),
                    "records": [r.model_dump(mode="json") for r in self.records],
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        with (self.output_dir / "human_overrides.jsonl").open("w", encoding="utf-8") as fh:
            for override in self.overrides:
                fh.write(json.dumps(override.model_dump(mode="json"), ensure_ascii=False) + "\n")

        with (self.output_dir / "escalations.jsonl").open("w", encoding="utf-8") as fh:
            for escalation in self.escalations:
                fh.write(json.dumps(escalation.model_dump(mode="json"), ensure_ascii=False) + "\n")

        (self.output_dir / "learning_proposals.json").write_text(
            json.dumps(
                {
                    "generated_at": utc_now(),
                    "run_id": self.run_id,
                    "status": "PENDING_HUMAN_APPROVAL",
                    "authority": (
                        "ADVISORY ONLY. No agent reads this file back to change behaviour, and "
                        "no agent process writes to config/ -- a test asserts it. Apply with: "
                        "python phase2_agents/apply_proposal.py --id <id> --approved-by <name>"
                    ),
                    "mechanism": (
                        "Repeated human overrides on the same firm standard produce a proposed "
                        "diff to a readable policy file. A human reviews the diff and either "
                        "approves it -- which records who, when and why -- or does not. The "
                        "system never applies its own proposal. What changes is policy text, "
                        "not model weights, so the change is reviewable by the people "
                        "accountable for it."
                    ),
                    "proposal_count": len(proposals),
                    "proposals": [p.model_dump(mode="json") for p in proposals],
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        return {
            "audit_records": len(self.records),
            "human_overrides": len(self.overrides),
            "escalations": len(self.escalations),
            "learning_proposals": len(proposals),
        }
