"""
phase2_agents/agents/risk_flagger.py
====================================
Agent 2 -- Risk Flagging.

For each classified clause: what the risk is, how severe, what to say instead,
and a confidence score.

THE DESIGN DECISION THAT MATTERS
--------------------------------
Most of the work here is NOT done by a language model.

Measurable breaches of the firm's written standards are found by rules.py --
"60 days exceeds payment_terms.max_days_to_pay (30)". That is a comparison, and
a rule does it better than a model in every way that counts: free, instant,
identical every run, and it cites the policy it applied. A finding that names
its standard can be argued with by a lawyer and defended to a regulator. A
finding that says "the model thought this was risky" can be neither.

The model handles the residue: one-sided discretion, undefined terms,
obligations with no stated limit. Its prompt is given the rule findings so it
does not restate them.

Every finding carries `decided_by`, so the split between rule and model is
visible in the audit log and in the cost report.

SUGGESTING ALTERNATIVES THAT ARE WORTH SOMETHING
------------------------------------------------
Every flag must carry a concrete alternative. Generic redline text is easy and
nearly useless. Instead, where the corpus contains a clause of the
same category that breaches no firm standard, we cite it -- language this firm
has already negotiated successfully on a comparable contract is a far easier
sell to a client than a lawyer's invention.

For CLZ-2025-0001 (indemnity covering the Owner's own negligence) the
precedents are CLZ-2025-0007 and CLZ-2025-0013, both of which limit the
obligation to the Consultant's own negligence. CLZ-2025-0013 is, in fact, the
amended replacement for CLZ-2025-0001 -- the firm already fixed this exact
problem, and we can point at how.

GATING
------
Only risk-bearing categories get a full pass (config: risk_bearing_categories).
Scope-of-work boilerplate rarely earns one. About 13 of our 20 clauses qualify,
so this is a real budget lever, not a token gesture.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from phase2_agents.agents.base import Agent, AgentResult, ParseFailure
from phase2_agents.models import (
    DecidedBy,
    DecisionStatus,
    RiskAssessment,
    RiskFinding,
    Severity,
    TokenUsage,
)
from phase2_agents.rules import evaluate as evaluate_rules
from phase2_agents.rules import find_precedents

SEVERITY_RANK = {Severity.NONE: 0, Severity.LOW: 1, Severity.MEDIUM: 2, Severity.HIGH: 3}


class RiskFlaggerAgent(Agent):
    name = "risk_flagger"
    output_model = RiskAssessment

    def __init__(self, *args: Any, firm_standards: Dict[str, Any], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.firm_standards = firm_standards
        self.risk_bearing = set(self.config.get("risk_bearing_categories", []))
        self.review_required_at = Severity(self.config.get("review_required_at_severity", "high"))
        # Filled by run.py after the deterministic pass over the whole corpus.
        self.clean_by_category: Dict[str, List[str]] = {}

    # ----------------------------------------------------------------------
    def assess(self, clause: Dict[str, Any], category: str) -> AgentResult:
        started = time.perf_counter()
        clause_id = clause["clause_id"]
        clause_text = clause.get("clause_text", "")

        # --- deterministic pass -------------------------------------------
        rule_findings = [
            RiskFinding.model_validate(f)
            for f in evaluate_rules(category, clause_text, self.firm_standards)
        ]
        for finding in rule_findings:
            finding.precedent_clause_ids = find_precedents(
                category, clause_id, self.clean_by_category
            )
        if rule_findings:
            self.telemetry.count("risk.rule_findings_total", len(rule_findings))

        # --- gate: is a model pass worth paying for? ----------------------
        if category not in self.risk_bearing:
            assessment = RiskAssessment(
                findings=rule_findings,
                overall_severity=self._overall(rule_findings),
                confidence=1.0,
                notes=(
                    f"Category {category!r} is outside risk_bearing_categories; deterministic "
                    f"checks were applied but no model pass was purchased."
                ),
            )
            self.telemetry.count("risk.model_pass_skipped_total")
            return AgentResult(
                output=assessment,
                decided_by=DecidedBy.RULE,
                status=self._status(assessment),
                usage=TokenUsage(),
                latency_ms=(time.perf_counter() - started) * 1000,
                detail="Rules only; category not gated for model review.",
            )

        # --- model pass on the residue -------------------------------------
        existing = (
            "\n".join(f"- [{f.severity.value}] {f.risk} ({f.standard_reference})" for f in rule_findings)
            or "(none)"
        )
        try:
            model_assessment, decided_by, usage, latency_ms = self.call_model(
                clause_id,
                clause_text=clause_text,
                category=category,
                existing_findings=existing,
            )
        except ParseFailure as exc:
            # The rule findings are still valid and still worth publishing.
            # Losing them because the model failed would be a worse outcome
            # than an escalation, so we degrade rather than discard.
            self.telemetry.count("risk.failures_total")
            assessment = RiskAssessment(
                findings=rule_findings,
                overall_severity=self._overall(rule_findings),
                confidence=1.0,
                notes="Model pass failed; deterministic findings retained.",
            )
            return AgentResult(
                output=assessment,
                decided_by=DecidedBy.RULE,
                status=DecisionStatus.ESCALATED,
                usage=TokenUsage(),
                latency_ms=(time.perf_counter() - started) * 1000,
                escalation_reason="unparseable_model_output",
                detail=f"Model output could not be validated after one retry: {exc}",
            )

        assert isinstance(model_assessment, RiskAssessment)
        for finding in model_assessment.findings:
            if not finding.precedent_clause_ids:
                finding.precedent_clause_ids = find_precedents(
                    category, clause_id, self.clean_by_category
                )

        # The prompt tells the model not to repeat findings the rules already
        # raised. It sometimes does anyway -- a prompt instruction is a request,
        # not a guarantee, and that is true of real models too. So we enforce
        # it. Rule findings win: they carry a policy reference and confidence
        # 1.0, where the model's restatement is a paraphrase.
        seen_standards = {f.standard_reference for f in rule_findings if f.standard_reference}
        deduped = [
            f for f in model_assessment.findings
            if not (f.standard_reference and f.standard_reference in seen_standards)
        ]
        dropped = len(model_assessment.findings) - len(deduped)
        if dropped:
            self.telemetry.count("risk.duplicate_model_findings_dropped_total", dropped)
            self.telemetry.log(
                "DEBUG",
                "risk.duplicate_finding_dropped",
                f"Dropped {dropped} model finding(s) already covered by a rule on {clause_id}.",
                clause_id=clause_id,
            )
        model_assessment.findings = deduped
        combined = rule_findings + deduped
        assessment = RiskAssessment(
            findings=combined,
            overall_severity=self._overall(combined),
            # Rules are exact; the model is not. The combined confidence is the
            # weaker of the two, because a finding set is only as trustworthy
            # as its least certain member.
            confidence=min(1.0, model_assessment.confidence) if model_assessment.findings else 1.0,
            notes=model_assessment.notes,
        )

        status = self._status(assessment)
        escalation_reason: Optional[str] = None
        detail: Optional[str] = None
        if assessment.confidence < self.escalate_below_confidence and model_assessment.findings:
            status = DecisionStatus.ESCALATED
            escalation_reason = "low_confidence_risk_assessment"
            detail = (
                f"Model confidence {assessment.confidence:.2f} below the "
                f"{self.escalate_below_confidence:.2f} threshold."
            )
            self.telemetry.count("risk.escalations_total")

        self.telemetry.count(
            "risk.decided_by_cache_total" if decided_by == DecidedBy.CACHE
            else "risk.decided_by_model_total"
        )
        return AgentResult(
            output=assessment,
            decided_by=decided_by,
            status=status,
            usage=usage,
            latency_ms=latency_ms,
            escalation_reason=escalation_reason,
            detail=detail,
        )

    # ----------------------------------------------------------------------
    @staticmethod
    def _overall(findings: List[RiskFinding]) -> Severity:
        """Worst finding wins. Averaging severities would let three low-risk
        findings mask one that could bankrupt the firm."""
        if not findings:
            return Severity.NONE
        return max((f.severity for f in findings), key=lambda s: SEVERITY_RANK[s])

    def _status(self, assessment: RiskAssessment) -> DecisionStatus:
        return DecisionStatus.PROPOSED

    def requires_review(self, assessment: RiskAssessment) -> bool:
        """High-severity output publishes, but never as settled.

        Blocking publication would break the pipeline for exactly the clauses
        that most need to be seen. So the register is always written, and
        anything at or above this severity carries review_status=pending.
        """
        return SEVERITY_RANK[assessment.overall_severity] >= SEVERITY_RANK[self.review_required_at]

    # Filled by run.py once the deterministic pass has run over every clause.
    def set_precedent_index(self, clean_by_category: Dict[str, List[str]]) -> None:
        self.clean_by_category = clean_by_category
