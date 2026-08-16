"""
phase2_agents/models.py
=======================
Every structure that crosses an agent boundary, as a pydantic model.

WHY SCHEMAS FIRST
-----------------
An LLM returns text. Text is not a decision. The moment we parse that text into
a validated object we have something we can log, compare, override and audit --
and something that fails loudly when the model returns nonsense instead of
quietly propagating a malformed field into the Gold register.

These models are also the contract for the real LLM: `with_structured_output`
takes them directly, so swapping StubChatModel for ChatAnthropic requires no
schema work.

THE SHAPE THAT MATTERS MOST
---------------------------
`AuditRecord` keeps three fields deliberately separate:

    model_output      what the machine proposed
    human_decision    what a person decided, if anyone did
    effective_value   what the system actually used

This is the required separation between model output and human decision,
made structural. You can always answer "who decided this?" by looking at one
field (`decided_by`), and you can always reconstruct the model's original
opinion even after a human overruled it, because nothing is ever overwritten.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


# --------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------
class ClauseCategory(str, Enum):
    """The seven categories the interface names, plus `other`.

    Deliberately NOT the same as Silver's `clause_category`. Silver carries what
    the source system said (`consequential_damages`, `security_clearance`);
    this is our review taxonomy. Where the source category has no home here,
    the classifier answers `other` and escalates rather than force-fitting --
    a wrong category silently changes which risk rubric gets applied.
    """

    LIABILITY = "liability"
    INSURANCE = "insurance"
    INDEMNIFICATION = "indemnification"
    PAYMENT_TERMS = "payment_terms"
    TERMINATION = "termination"
    SCOPE_OF_WORK = "scope_of_work"
    OTHER = "other"


class Severity(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"


class DecidedBy(str, Enum):
    """How a decision was reached. This is a cost lever AND an audit answer.

    A deterministic rule beats a model wherever it applies: it is free,
    instant, reproducible, and easier to defend. `RULE` is not a fallback --
    it is the preferred path.
    """

    RULE = "rule"
    MODEL = "model"
    HUMAN = "human"
    CACHE = "cache"


class DecisionStatus(str, Enum):
    PROPOSED = "proposed"           # the model's opinion, nothing more
    ACCEPTED = "accepted"           # no human disagreed
    OVERRIDDEN = "overridden"       # a human replaced it
    ESCALATED = "escalated"         # routed to a human queue, unresolved
    NOT_PROCESSED = "not_processed"  # budget/kill-switch stopped us first


class HaltReason(str, Enum):
    BUDGET_EXCEEDED = "budget_exceeded"
    KILL_SWITCH_FILE = "kill_switch_file"
    ERROR_RATE = "error_rate"
    CONSECUTIVE_FAILURES = "consecutive_failures"


# --------------------------------------------------------------------------
# Agent 1 -- Classification
# --------------------------------------------------------------------------
class ClauseClassification(BaseModel):
    """Agent 1's structured output. Also the LLM's response schema."""

    model_config = ConfigDict(use_enum_values=False)

    category: ClauseCategory = Field(description="Review category for this clause.")
    confidence: float = Field(ge=0.0, le=1.0, description="0-1. Below the escalation threshold routes to a human.")
    reasoning: str = Field(description="One sentence. Why this category.")
    signals: List[str] = Field(
        default_factory=list,
        description="Concrete phrases from the clause that drove the decision. This is what makes the classification checkable by a human.",
    )


# --------------------------------------------------------------------------
# Agent 2 -- Risk flagging
# --------------------------------------------------------------------------
class RiskFinding(BaseModel):
    """One risk. Note what is required: not just 'this is bad'.

    `standard_reference` is the difference between an auditable finding and an
    opinion. "Payment terms of 60 days exceed the firm standard of 30
    (payment.max_days_to_pay)" can be defended to a regulator. "The model
    thought this was risky" cannot.
    """

    risk: str = Field(description="What the risk actually is, in one sentence.")
    severity: Severity
    confidence: float = Field(ge=0.0, le=1.0)
    standard_reference: Optional[str] = Field(
        default=None,
        description="Which firm standard this breaches, e.g. 'liability.max_cap_multiple_of_fees'. Null means judgement, not policy.",
    )
    observed: Optional[str] = Field(default=None, description="What the clause says.")
    expected: Optional[str] = Field(default=None, description="What the firm standard requires.")
    suggested_alternative: str = Field(description="Replacement language. Required by the requirements.")
    precedent_clause_ids: List[str] = Field(
        default_factory=list,
        description="Clauses in our own corpus that already use acceptable language. An alternative we have successfully negotiated beats invented boilerplate.",
    )


class RiskAssessment(BaseModel):
    """Agent 2's structured output for one clause."""

    findings: List[RiskFinding] = Field(default_factory=list)
    overall_severity: Severity = Severity.NONE
    confidence: float = Field(ge=0.0, le=1.0, default=1.0)
    notes: Optional[str] = None


# --------------------------------------------------------------------------
# Cost accounting
# --------------------------------------------------------------------------
class TokenUsage(BaseModel):
    """Mirrors LangChain's AIMessage.usage_metadata exactly.

    The stub populates this the same way ChatAnthropic does, so budget
    arithmetic is production-real even though the numbers are simulated.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            cost_usd=round(self.cost_usd + other.cost_usd, 8),
        )


# --------------------------------------------------------------------------
# The audit record -- the Phase 2 deliverable
# --------------------------------------------------------------------------
class AuditRecord(BaseModel):
    """One line of the ledger. Append-only; never edited, never deleted.

    Contains every field the platform requires (timestamp, agent, clause id,
    action, output) plus what makes it usable a year later: which prompt
    version and model produced it, what it cost, and -- critically -- whether
    a human was involved.
    """

    # -- required ---------------------------------------------
    timestamp: str
    agent: str
    clause_id: str
    action: str
    # -- the model/human separation ----------------------------------------
    model_output: Optional[Dict[str, Any]] = None
    human_decision: Optional[Dict[str, Any]] = None
    effective_value: Optional[Dict[str, Any]] = None
    decided_by: DecidedBy = DecidedBy.MODEL
    status: DecisionStatus = DecisionStatus.PROPOSED
    # -- reproducibility ----------------------------------------------------
    run_id: str
    trace_id: Optional[str] = None
    span_id: Optional[str] = None
    prompt_version: Optional[str] = None
    model_name: Optional[str] = None
    rubric_version: Optional[str] = None
    firm_standards_version: Optional[str] = None
    # -- cost ---------------------------------------------------------------
    usage: Optional[TokenUsage] = None
    latency_ms: Optional[float] = None
    # -- provenance back through Phase 1 ------------------------------------
    lineage: Optional[Dict[str, Any]] = None
    # -- free-form ----------------------------------------------------------
    detail: Optional[str] = None


class HumanOverride(BaseModel):
    """A person disagreeing with the machine. Append-only.

    Note what it does NOT do: change a threshold, a prompt, or a rule. An
    override changes the outcome for ONE clause. Only an approved
    LearningProposal changes behaviour. Collapsing those two would fail the
    platform's non-negotiable governance rule while appearing to comply.
    """

    override_id: str
    timestamp: str
    clause_id: str
    agent: str
    reviewer: str
    field: str = Field(description="What was overridden, e.g. 'risk.overall_severity'.")
    model_value: Any
    human_value: Any
    rationale: str
    source: str = Field(
        default="simulated",
        description="'simulated' or 'review_history' when replayed from reviewer records already in the dataset.",
    )


class LearningProposal(BaseModel):
    """The learning mechanism -- as a PROPOSAL, never self-executed.

    The system may notice that humans keep overriding it in the same
    direction, and may write down what it would change. It may not make the
    change. `apply_proposal.py`, run by a person, is the only path that edits
    config/. A test asserts no agent process writes there.

    What gets changed is a readable policy file, not model weights or a
    silently mutated prompt -- so a human approves a diff they can actually
    review. That is what makes this governable rather than aspirational.
    """

    proposal_id: str
    created_at: str
    status: str = "PENDING_HUMAN_APPROVAL"
    kind: str = Field(description="e.g. 'adjust_firm_standard', 'add_rubric_rule', 'revise_prompt'.")
    summary: str
    evidence_override_ids: List[str] = Field(default_factory=list)
    evidence_clause_ids: List[str] = Field(default_factory=list)
    occurrences: int = 1
    target_file: str = Field(description="The config file a human would edit.")
    current_value: Any = None
    proposed_value: Any = None
    rationale: str = ""
    risk_if_applied: str = Field(
        default="",
        description="What could go wrong. A proposal that only argues for itself is not reviewable.",
    )
    authority: str = (
        "ADVISORY ONLY. Nothing in the pipeline reads this file back to change behaviour. "
        "Apply with: python phase2_agents/apply_proposal.py --id <id> --approved-by <name>"
    )


class Escalation(BaseModel):
    """A clause routed to a human, in a queue someone actually reads.

    Escalating into the void is worse than not escalating: it looks like a
    control while being a no-op. So escalations are a file, and their count is
    a metric in run_summary.json.
    """

    escalation_id: str
    timestamp: str
    clause_id: str
    agent: str
    reason: str
    detail: str
    model_output: Optional[Dict[str, Any]] = None
    status: str = "OPEN"


# --------------------------------------------------------------------------
# Gold
# --------------------------------------------------------------------------
class ClauseRiskEntry(BaseModel):
    """One row of the Gold risk register: the effective, current view."""

    clause_id: str
    contract_id: str
    client_name: str
    section_ref: str
    source_category: Optional[str] = Field(default=None, description="What Silver said the category was.")
    review_category: Optional[ClauseCategory] = Field(default=None, description="What Agent 1 decided.")
    classification_confidence: Optional[float] = None
    classification_decided_by: Optional[DecidedBy] = None
    overall_severity: Severity = Severity.NONE
    findings: List[RiskFinding] = Field(default_factory=list)
    status: DecisionStatus = DecisionStatus.PROPOSED
    review_status: str = Field(
        default="not_required",
        description="'pending' for anything high severity. The register always publishes -- blocking it would break the pipeline -- but nothing high-severity may read as settled.",
    )
    human_overrides: List[str] = Field(default_factory=list)
    superseded_by: Optional[str] = Field(
        default=None,
        description="From Phase 1's supersession proposals. Without this we would confidently flag a clause the legal team already fixed.",
    )
    usage: Optional[TokenUsage] = None
    lineage: Optional[Dict[str, Any]] = None
