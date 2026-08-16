"""
phase2_agents/agents/classifier.py
==================================
Agent 1 -- Clause Classifier.

Assigns each clause one of the seven review categories and a confidence.

WHY THIS IS A SEPARATE AGENT
----------------------------
"What kind of clause is this?" is close to objective. "Is it bad for us?" is a
judgement that depends on the firm's risk appetite. They change at different
rates and different people own them, so they get separate prompts, separate
confidence thresholds and separate audit records.

There is also a practical reason: classification is cheap, risk analysis is
expensive. Running the cheap one first lets us skip the expensive one for
categories that do not carry risk -- which is what makes the budget guardrail
meaningful rather than decorative.

RULES FIRST, MODEL SECOND
-------------------------
Silver already carries a `clause_category` from the source system. Where that
maps cleanly onto our review taxonomy, no model call is needed at all -- it is
free, instant, and reproducible. The model handles what the source could not
tell us.

The two mappings worth noting:
  limitation_of_liability -> liability   (same thing, different name)
  consequential_damages   -> liability   (a damages limitation)
  security_clearance      -> other       (real category, outside our taxonomy)

`security_clearance` is exactly why `other` must escalate rather than be
accepted: CLZ-2025-0012 is a genuine compliance clause (NIST SP 800-171, CUI
handling) that our seven-category review taxonomy has no home for. Silently
filing it as `other` and moving on would hide a real gap in coverage.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from phase2_agents.agents.base import Agent, AgentResult, ParseFailure
from phase2_agents.models import (
    ClauseCategory,
    ClauseClassification,
    DecidedBy,
    DecisionStatus,
    TokenUsage,
)

# Source category -> our review taxonomy. Deterministic, so no model call.
# Kept here rather than in config because it is a code-level taxonomy mapping,
# not a policy a business user would tune; changing it changes which rubric
# runs, which is a code change with a test.
SOURCE_CATEGORY_MAP: Dict[str, str] = {
    "indemnification": "indemnification",
    "insurance": "insurance",
    "payment_terms": "payment_terms",
    "termination": "termination",
    "scope_of_work": "scope_of_work",
    "liability": "liability",
    "limitation_of_liability": "liability",
    "consequential_damages": "liability",
}

# Source categories we recognise but that have no home in the review taxonomy.
# Distinguished from "unknown" so the escalation can say which it is.
KNOWN_UNMAPPED = {"security_clearance"}


class ClassifierAgent(Agent):
    name = "clause_classifier"
    output_model = ClauseClassification

    def classify(self, clause: Dict[str, Any]) -> AgentResult:
        started = time.perf_counter()
        source_category = clause.get("clause_category")

        # --- rule path: the source already told us -------------------------
        if self.prefer_rules and source_category in SOURCE_CATEGORY_MAP:
            mapped = SOURCE_CATEGORY_MAP[source_category]
            renamed = mapped != source_category
            classification = ClauseClassification(
                category=ClauseCategory(mapped),
                # Not 1.0. The source system's label is authoritative for what
                # it is, but it is a different taxonomy from ours, so a
                # renamed mapping carries slightly less certainty than an
                # identical one. Confidence should mean something.
                confidence=0.95 if not renamed else 0.9,
                reasoning=(
                    f"Source system classified this as {source_category!r}; "
                    + (
                        f"mapped to review category {mapped!r}."
                        if renamed
                        else "the review taxonomy uses the same name."
                    )
                ),
                signals=[f"silver.clause_category={source_category}"],
            )
            self.telemetry.count("classifier.decided_by_rule_total")
            return AgentResult(
                output=classification,
                decided_by=DecidedBy.RULE,
                status=DecisionStatus.PROPOSED,
                usage=TokenUsage(),
                latency_ms=(time.perf_counter() - started) * 1000,
                detail="Resolved from the Silver category without a model call.",
            )

        # --- known-but-unmapped: escalate, do not guess --------------------
        if source_category in KNOWN_UNMAPPED:
            classification = ClauseClassification(
                category=ClauseCategory.OTHER,
                confidence=0.5,
                reasoning=(
                    f"Source category {source_category!r} is a real clause type with no "
                    f"equivalent in the seven-category review taxonomy."
                ),
                signals=[f"silver.clause_category={source_category}"],
            )
            self.telemetry.count("classifier.decided_by_rule_total")
            return AgentResult(
                output=classification,
                decided_by=DecidedBy.RULE,
                status=DecisionStatus.ESCALATED,
                usage=TokenUsage(),
                latency_ms=(time.perf_counter() - started) * 1000,
                escalation_reason="category_outside_review_taxonomy",
                detail=(
                    f"{source_category!r} needs either a new review category or an explicit "
                    f"decision that it is out of scope. Filing it as 'other' would hide a "
                    f"coverage gap."
                ),
            )

        # --- model path: the ambiguous middle ------------------------------
        try:
            classification, decided_by, usage, latency_ms = self.call_model(
                clause["clause_id"], clause_text=clause.get("clause_text", "")
            )
        except ParseFailure as exc:
            self.telemetry.count("classifier.failures_total")
            return AgentResult(
                output=None,
                decided_by=DecidedBy.MODEL,
                status=DecisionStatus.ESCALATED,
                usage=TokenUsage(),
                latency_ms=(time.perf_counter() - started) * 1000,
                escalation_reason="unparseable_model_output",
                detail=f"Model output could not be validated after one retry: {exc}",
            )

        assert isinstance(classification, ClauseClassification)
        self.telemetry.count(
            "classifier.decided_by_cache_total" if decided_by == DecidedBy.CACHE
            else "classifier.decided_by_model_total"
        )

        status = DecisionStatus.PROPOSED
        escalation_reason: Optional[str] = None
        detail: Optional[str] = None
        if classification.confidence < self.escalate_below_confidence:
            status = DecisionStatus.ESCALATED
            escalation_reason = "low_confidence_classification"
            detail = (
                f"Confidence {classification.confidence:.2f} is below the "
                f"{self.escalate_below_confidence:.2f} threshold. A wrong category applies the "
                f"wrong risk rubric, so this is routed to a human rather than accepted."
            )
            self.telemetry.count("classifier.escalations_total")

        return AgentResult(
            output=classification,
            decided_by=decided_by,
            status=status,
            usage=usage,
            latency_ms=latency_ms,
            escalation_reason=escalation_reason,
            detail=detail,
        )
