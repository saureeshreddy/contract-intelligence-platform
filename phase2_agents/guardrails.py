"""
phase2_agents/guardrails.py
===========================
The three control surfaces. All real code, all enforced before work happens.

The platform requires at least one. We implement all three because they defend
against genuinely different failures, and each is about twenty lines:

    BudgetGuard   spend      -> STOP the run
    ScopeGuard    input      -> REFUSE this clause, escalate it
    KillSwitch    health     -> HALT everything

THE DISTINCTION THAT MATTERS
----------------------------
A rate limiter (in llm.py) and a budget guard are constantly confused. They are
not the same control:

    rate limiter   the provider's requests/sec   -> WAITS, then proceeds
    budget guard   the business's spend          -> STOPS, and does not resume

A guard that waits is not a guard.

WHAT NONE OF THEM DO
--------------------
None silently skips work. A clause the budget could not afford is written out
as `not_processed`; a clause refused for scope is written out as `escalated`.
An operator can always account for all 20 clauses. Silent truncation is the
failure mode these exist to prevent, so it must not be reintroduced by them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from common.observability import Telemetry
from phase2_agents.models import HaltReason, TokenUsage


class HaltProcessing(RuntimeError):
    """Raised to stop the run. Carries why, so the audit log can record it."""

    def __init__(self, reason: HaltReason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


# --------------------------------------------------------------------------
# Budget
# --------------------------------------------------------------------------
@dataclass
class BudgetGuard:
    """Token and cost ceiling for a run.

    Checked *before* each clause, not after: discovering you are over budget
    having already spent the money is an accounting exercise, not a control.
    Because a clause's cost is not known in advance we stop at the boundary
    before the next one, so the ceiling can be exceeded by at most one clause.
    That is stated rather than hidden.
    """

    max_total_tokens: int
    max_cost_usd: float
    warn_at_fraction: float = 0.8
    spent: TokenUsage = field(default_factory=TokenUsage)
    _warned: bool = False

    def record(self, usage: TokenUsage) -> None:
        self.spent = self.spent + usage

    @property
    def token_fraction(self) -> float:
        return self.spent.total_tokens / self.max_total_tokens if self.max_total_tokens else 0.0

    @property
    def cost_fraction(self) -> float:
        return self.spent.cost_usd / self.max_cost_usd if self.max_cost_usd else 0.0

    def check(self, telemetry: Telemetry) -> None:
        """Raise HaltProcessing if the ceiling is reached."""
        if self.spent.total_tokens >= self.max_total_tokens:
            raise HaltProcessing(
                HaltReason.BUDGET_EXCEEDED,
                f"Token budget exhausted: {self.spent.total_tokens:,} of {self.max_total_tokens:,}.",
            )
        if self.spent.cost_usd >= self.max_cost_usd:
            raise HaltProcessing(
                HaltReason.BUDGET_EXCEEDED,
                f"Cost budget exhausted: ${self.spent.cost_usd:.4f} of ${self.max_cost_usd:.4f}.",
            )
        worst = max(self.token_fraction, self.cost_fraction)
        if worst >= self.warn_at_fraction and not self._warned:
            self._warned = True
            telemetry.warn(
                "budget.warning",
                f"Budget {worst:.0%} consumed "
                f"({self.spent.total_tokens:,} tokens, ${self.spent.cost_usd:.4f}).",
                token_fraction=round(self.token_fraction, 4),
                cost_fraction=round(self.cost_fraction, 4),
            )

    def snapshot(self) -> Dict[str, Any]:
        return {
            "tokens_spent": self.spent.total_tokens,
            "tokens_budget": self.max_total_tokens,
            "tokens_fraction": round(self.token_fraction, 4),
            "cost_usd_spent": round(self.spent.cost_usd, 6),
            "cost_usd_budget": self.max_cost_usd,
            "cost_fraction": round(self.cost_fraction, 4),
        }


# --------------------------------------------------------------------------
# Scope
# --------------------------------------------------------------------------
@dataclass
class ScopeGuard:
    """What this system is willing to look at.

    Refusal is not a silent skip. Every refusal produces an Escalation in a
    queue a human reads. The point is that the agent declines to guess rather
    than producing a confident answer about something it was not built for --
    a fabricated classification is worse than an admitted gap.
    """

    max_clause_chars: int
    min_clause_chars: int
    allowed_categories: List[str]

    def check_clause(self, clause: Dict[str, Any]) -> Tuple[bool, Optional[str], Optional[str]]:
        """Returns (in_scope, reason_code, detail)."""
        text = clause.get("clause_text") or ""
        if not text.strip():
            return False, "empty_clause_text", "Clause has no text to review."
        if len(text) < self.min_clause_chars:
            return (
                False,
                "clause_too_short",
                f"Clause text is {len(text)} chars, below the {self.min_clause_chars} minimum. "
                f"Too little context to classify reliably.",
            )
        if len(text) > self.max_clause_chars:
            return (
                False,
                "clause_too_long",
                f"Clause text is {len(text):,} chars, above the {self.max_clause_chars:,} limit. "
                f"Likely a whole document rather than a clause; needs splitting upstream.",
            )
        return True, None, None

    def check_category(self, category: str) -> Tuple[bool, Optional[str], Optional[str]]:
        if category not in self.allowed_categories:
            return (
                False,
                "category_out_of_scope",
                f"Category {category!r} is not in the reviewable set {self.allowed_categories}.",
            )
        return True, None, None


# --------------------------------------------------------------------------
# Kill switch
# --------------------------------------------------------------------------
@dataclass
class KillSwitch:
    """Halts all processing on a defined condition.

    Three triggers:
      1. an operator creates the STOP file  (same mechanism as Phase 1 ingest)
      2. N consecutive agent failures
      3. the failure rate crosses a threshold, once enough records have run

    (3) needs a minimum sample: one failure out of one record is 100% and would
    trip instantly on a transient error.

    Resuming after a trip requires --acknowledge-halt "<reason>". If the switch
    fired on an error rate, re-running blind is precisely the wrong response,
    so a human has to state that they looked. That acknowledgement is written
    into the audit log.
    """

    stop_file: Path
    max_consecutive_failures: int
    max_failure_rate: float
    min_records_before_rate_check: int
    consecutive_failures: int = 0
    total_processed: int = 0
    total_failures: int = 0
    tripped_reason: Optional[HaltReason] = None

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.total_processed += 1

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        self.total_failures += 1
        self.total_processed += 1

    @property
    def failure_rate(self) -> float:
        return self.total_failures / self.total_processed if self.total_processed else 0.0

    def check(self, telemetry: Telemetry) -> None:
        if self.stop_file.exists():
            self.tripped_reason = HaltReason.KILL_SWITCH_FILE
            raise HaltProcessing(
                HaltReason.KILL_SWITCH_FILE,
                f"Kill switch engaged: {self.stop_file.name} exists. "
                f"Delete it and re-run with --acknowledge-halt to continue.",
            )
        if self.consecutive_failures >= self.max_consecutive_failures:
            self.tripped_reason = HaltReason.CONSECUTIVE_FAILURES
            raise HaltProcessing(
                HaltReason.CONSECUTIVE_FAILURES,
                f"{self.consecutive_failures} consecutive agent failures "
                f"(limit {self.max_consecutive_failures}). Something is systematically wrong; "
                f"stopping rather than burning budget on it.",
            )
        if (
            self.total_processed >= self.min_records_before_rate_check
            and self.failure_rate > self.max_failure_rate
        ):
            self.tripped_reason = HaltReason.ERROR_RATE
            raise HaltProcessing(
                HaltReason.ERROR_RATE,
                f"Failure rate {self.failure_rate:.0%} over {self.total_processed} records "
                f"exceeds the {self.max_failure_rate:.0%} limit.",
            )

    def snapshot(self) -> Dict[str, Any]:
        return {
            "processed": self.total_processed,
            "failures": self.total_failures,
            "failure_rate": round(self.failure_rate, 4),
            "consecutive_failures": self.consecutive_failures,
            "tripped_reason": self.tripped_reason.value if self.tripped_reason else None,
        }


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------
@dataclass
class Guardrails:
    """All three, constructed from config and passed around as one object."""

    budget: BudgetGuard
    scope: ScopeGuard
    kill_switch: KillSwitch

    @classmethod
    def from_config(cls, config: Dict[str, Any], repo_root: Path) -> "Guardrails":
        guards = config.get("guardrails", {})
        budget = guards.get("budget", {})
        scope = guards.get("scope", {})
        kill = guards.get("kill_switch", {})
        return cls(
            budget=BudgetGuard(
                max_total_tokens=int(budget.get("max_total_tokens", 200_000)),
                max_cost_usd=float(budget.get("max_cost_usd", 1.0)),
                warn_at_fraction=float(budget.get("warn_at_fraction", 0.8)),
            ),
            scope=ScopeGuard(
                max_clause_chars=int(scope.get("max_clause_chars", 20_000)),
                min_clause_chars=int(scope.get("min_clause_chars", 20)),
                allowed_categories=list(scope.get("allowed_categories", [])),
            ),
            kill_switch=KillSwitch(
                stop_file=repo_root / kill.get("stop_file", "phase2_agents/output/STOP"),
                max_consecutive_failures=int(kill.get("max_consecutive_failures", 3)),
                max_failure_rate=float(kill.get("max_failure_rate", 0.25)),
                min_records_before_rate_check=int(kill.get("min_records_before_rate_check", 8)),
            ),
        )

    def check_all(self, telemetry: Telemetry) -> None:
        """Called before each clause. Order matters: cheapest and most urgent first."""
        self.kill_switch.check(telemetry)
        self.budget.check(telemetry)

    def snapshot(self) -> Dict[str, Any]:
        return {
            "budget": self.budget.snapshot(),
            "kill_switch": self.kill_switch.snapshot(),
            "scope": {
                "max_clause_chars": self.scope.max_clause_chars,
                "min_clause_chars": self.scope.min_clause_chars,
                "allowed_categories": self.scope.allowed_categories,
            },
        }
