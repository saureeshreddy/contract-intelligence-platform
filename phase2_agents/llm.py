"""
phase2_agents/llm.py
====================
The LLM seam: where a real model would be called, and what stands in for it.

THE SWAP
--------
One config value (`model.provider` in agents.yml) decides whether calls are
real. Nothing else in the codebase changes:

    provider: stub       -> StubChatModel      deterministic, offline, free
    provider: anthropic  -> ChatAnthropic      real calls

Both are `BaseChatModel`, so the agents build identical chains either way:

    chain = prompt | model | parser

WHY THE STUB IS BUILT THE WAY IT IS
-----------------------------------
A fake model that returns a hardcoded string proves nothing. This one is
built so that every surface the production system depends on is REAL, and
only the intelligence is simulated:

  * it populates `AIMessage.usage_metadata` exactly as ChatAnthropic does, so
    BudgetGuard performs production arithmetic on production fields
  * it goes through the same rate limiter and the same retry wrapper
  * it emits the same callbacks, so the telemetry path is identical
  * it is deterministic -- same input, same output, always. In an audited
    pipeline that is not a convenience, it is the product: a decision you
    cannot reproduce cannot be defended.

The simulated part is narrow and honest: token counts are estimated from
character length (~4 chars/token, the usual English approximation), and the
"reasoning" comes from keyword matching rather than a language model.

WHAT IS DELIBERATELY ABSENT
---------------------------
No conversation memory. These 20 clauses are independent; carrying state
between them would let clause 5's analysis contaminate clause 6, and the same
clause would then classify differently depending on what preceded it. That
destroys reproducibility, which destroys auditability.

Caching is a different thing and we do use it: identical input -> reuse the
decision, logged as `decided_by=cache` so an engineer can tell a reused
decision from a recomputed one.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.rate_limiters import InMemoryRateLimiter

from phase2_agents.models import TokenUsage

# English text averages ~4 characters per token. Good enough for budget
# planning; the real model reports exact counts through the same field.
CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


def price(usage: Dict[str, int], pricing: Dict[str, float]) -> float:
    """Cost from a config price table. Never hardcoded -- prices change."""
    return round(
        usage.get("input_tokens", 0) / 1_000_000 * pricing.get("input_per_1m", 0.0)
        + usage.get("output_tokens", 0) / 1_000_000 * pricing.get("output_per_1m", 0.0),
        8,
    )


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------
class DecisionCache:
    """Content-addressed cache of model decisions.

    Keyed on (prompt text + model name + prompt_version), so promoting a new
    prompt version automatically invalidates -- you can never serve a decision
    made by a prompt that is no longer active. That is a correctness property,
    not an optimisation.

    A plain JSON file rather than LangChain's SQLiteCache: it is inspectable,
    diffable, and a reviewer can open it. Same reasoning as Phase 1's JSONL.
    """

    def __init__(self, path: Optional[Path], enabled: bool = True) -> None:
        self.path = path
        self.enabled = enabled and path is not None
        self.hits = 0
        self.misses = 0
        self._store: Dict[str, Any] = {}
        if self.enabled and path is not None and path.exists():
            try:
                self._store = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self._store = {}  # a corrupt cache is a miss, never an error

    @staticmethod
    def key(prompt: str, model_name: str, prompt_version: str) -> str:
        material = f"{model_name}|{prompt_version}|{prompt}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        if not self.enabled:
            return None
        hit = self._store.get(key)
        if hit is None:
            self.misses += 1
        else:
            self.hits += 1
        return hit

    def put(self, key: str, value: Dict[str, Any]) -> None:
        if self.enabled:
            self._store[key] = value

    def flush(self) -> None:
        if self.enabled and self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._store, indent=2), encoding="utf-8")

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return round(self.hits / total, 4) if total else 0.0


# --------------------------------------------------------------------------
# The stub model
# --------------------------------------------------------------------------
class StubChatModel(BaseChatModel):
    """Deterministic stand-in for a real chat model.

    Returns JSON matching the schema the prompt asks for. The prompt carries a
    `RESPOND WITH JSON MATCHING:` marker naming which schema is wanted; the
    stub reads that and produces a well-formed instance, using keyword
    heuristics over the clause text so the output is plausible rather than
    constant.

    >>> REPLACE WITH, for production: <<<
    >>>     from langchain_anthropic import ChatAnthropic                    <<<
    >>>     model = ChatAnthropic(                                           <<<
    >>>         model="claude-sonnet-5",                                     <<<
    >>>         temperature=0.0,                                             <<<
    >>>         max_tokens=1024,                                             <<<
    >>>         rate_limiter=rate_limiter,     # same object, same config    <<<
    >>>     )                                                                <<<
    >>> The agents' chains, prompts, schemas, budget accounting, callbacks    <<<
    >>> and audit records are unchanged.                                      <<<
    """

    model_name: str = "stub-claude-sonnet-5"
    simulated_latency_ms: float = 0.0

    @property
    def _llm_type(self) -> str:
        return "stub_chat_model"

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        prompt_text = "\n".join(str(m.content) for m in messages)

        if self.simulated_latency_ms:
            time.sleep(self.simulated_latency_ms / 1000.0)

        content = self._respond(prompt_text)

        input_tokens = estimate_tokens(prompt_text)
        output_tokens = estimate_tokens(content)
        message = AIMessage(
            content=content,
            # The production-real part: identical field, identical shape to
            # what ChatAnthropic returns. BudgetGuard never knows the
            # difference.
            usage_metadata={
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            },
            response_metadata={"model_name": self.model_name, "finish_reason": "stop"},
        )
        return ChatResult(generations=[ChatGeneration(message=message)])

    # -- the simulated "intelligence" --------------------------------------
    def _respond(self, prompt: str) -> str:
        schema = "unknown"
        match = re.search(r"RESPOND WITH JSON MATCHING:\s*(\w+)", prompt)
        if match:
            schema = match.group(1)

        clause = ""
        clause_match = re.search(r"CLAUSE TEXT:\s*\"\"\"(.*?)\"\"\"", prompt, re.DOTALL)
        if clause_match:
            clause = clause_match.group(1).strip()

        if schema == "ClauseClassification":
            return json.dumps(self._classify(clause))
        if schema == "RiskAssessment":
            return json.dumps(self._assess(clause, prompt))
        return json.dumps({"error": f"stub has no response for schema {schema!r}"})

    @staticmethod
    def _classify(clause: str) -> Dict[str, Any]:
        text = clause.lower()
        # Ordered most-specific first: an indemnification clause frequently
        # mentions liability, so 'liability' must not win by appearing earlier.
        rules: List[Tuple[str, List[str]]] = [
            ("indemnification", ["indemnify", "indemnification", "hold harmless", "duty to defend"]),
            ("insurance", ["insurance", "coverage", "per occurrence", "per claim", "aggregate"]),
            ("payment_terms", ["payment", "invoice", "retainage", "late payment", "prompt payment"]),
            ("termination", ["terminate", "termination", "for convenience", "material breach"]),
            ("liability", ["liability", "consequential", "punitive damages", "shall not exceed"]),
            ("scope_of_work", ["design services", "scope", "deliverables", "construction documents"]),
        ]
        best_category, best_hits = "other", []
        for category, keywords in rules:
            hits = [k for k in keywords if k in text]
            if len(hits) > len(best_hits):
                best_category, best_hits = category, hits

        # Confidence reflects evidence strength, not a random number: more
        # distinct matched signals means more confidence. `other` is
        # deliberately low so it escalates rather than being accepted.
        confidence = 0.35 if best_category == "other" else min(0.95, 0.55 + 0.12 * len(best_hits))
        return {
            "category": best_category,
            "confidence": round(confidence, 2),
            "reasoning": (
                f"Matched {len(best_hits)} {best_category} signal(s): {', '.join(best_hits)}."
                if best_hits
                else "No category signals matched; routing to 'other' for human review."
            ),
            "signals": best_hits,
        }

    @staticmethod
    def _assess(clause: str, prompt: str) -> Dict[str, Any]:
        """Narrative risk assessment.

        Deliberately thin. The deterministic, policy-referenced findings come
        from rules.py, which is more auditable than any model output. The
        model's job here is the ambiguous residue -- language that needs
        interpretation rather than comparison against a number.
        """
        text = clause.lower()
        findings: List[Dict[str, Any]] = []

        if "sole discretion" in text or "at its discretion" in text:
            findings.append(
                {
                    "risk": "Grants the Owner unilateral discretion with no stated standard of reasonableness.",
                    "severity": "medium",
                    "confidence": 0.7,
                    "standard_reference": None,
                    "observed": "discretionary language",
                    "expected": "an objective, reviewable standard",
                    "suggested_alternative": "Replace discretionary language with an objective standard, e.g. 'acting reasonably and in good faith'.",
                    "precedent_clause_ids": [],
                }
            )
        if "regardless of" in text and "negligence" in text:
            findings.append(
                {
                    "risk": "Obligation applies regardless of fault, sweeping in the Owner's own negligence.",
                    "severity": "high",
                    "confidence": 0.75,
                    "standard_reference": "indemnification.must_be_limited_to_own_negligence",
                    "observed": "'regardless of ... negligence'",
                    "expected": "obligation limited to the Consultant's own negligent acts",
                    "suggested_alternative": "Limit the obligation to claims arising from the Consultant's negligent acts, errors, or omissions.",
                    "precedent_clause_ids": [],
                }
            )

        severity_rank = {"high": 3, "medium": 2, "low": 1, "none": 0}
        overall = max((f["severity"] for f in findings), key=lambda s: severity_rank[s], default="none")
        return {
            "findings": findings,
            "overall_severity": overall,
            "confidence": 0.7 if findings else 0.85,
            "notes": "Narrative pass only; policy-based findings are produced deterministically by rules.py.",
        }


# --------------------------------------------------------------------------
# Construction
# --------------------------------------------------------------------------
def build_rate_limiter(config: Dict[str, Any]) -> Optional[InMemoryRateLimiter]:
    """Provider throttle. NOT the budget -- this waits, the budget stops.

    Against StubChatModel there is nothing to throttle, and pretending
    otherwise would be theatre. It is wired into the real call path anyway so
    that switching to a real provider needs no code change, and so that the
    429-handling story is already in place rather than discovered in
    production.
    """
    settings = config.get("rate_limit") or {}
    if not settings:
        return None
    return InMemoryRateLimiter(
        requests_per_second=float(settings.get("requests_per_second", 2.0)),
        check_every_n_seconds=float(settings.get("check_every_n_seconds", 0.1)),
        max_bucket_size=float(settings.get("max_bucket_size", 5)),
    )


def build_model(config: Dict[str, Any]) -> BaseChatModel:
    """Return the configured chat model. THE swap point."""
    provider = str(config.get("provider", "stub")).lower()
    rate_limiter = build_rate_limiter(config)

    if provider == "stub":
        model = StubChatModel(
            model_name=f"stub-{config.get('name', 'model')}",
            rate_limiter=rate_limiter,
        )
    elif provider == "anthropic":  # pragma: no cover - needs a key and network
        try:
            from langchain_anthropic import ChatAnthropic
        except ImportError as exc:
            raise RuntimeError(
                "model.provider is 'anthropic' but langchain-anthropic is not installed.\n"
                "  pip install langchain-anthropic   and set ANTHROPIC_API_KEY"
            ) from exc
        model = ChatAnthropic(
            model=config.get("name", "claude-sonnet-5"),
            temperature=float(config.get("temperature", 0.0)),
            max_tokens=int(config.get("max_tokens", 1024)),
            rate_limiter=rate_limiter,
        )
    else:
        raise ValueError(f"Unknown model.provider {provider!r}. Use 'stub' or 'anthropic'.")

    retry = config.get("retry") or {}
    if retry.get("max_attempts", 0) > 1:
        # Transient provider failures (429, 5xx) get retried with backoff.
        # A parse failure is NOT retried here -- that is handled in the agent,
        # where we can reword the request rather than repeat it verbatim.
        model = model.with_retry(
            stop_after_attempt=int(retry["max_attempts"]),
            exponential_jitter_params={"initial": float(retry.get("exponential_base", 2.0))},
        )
    return model


def usage_from_message(message: Any, pricing: Dict[str, float]) -> TokenUsage:
    """Read usage off any LangChain message, stub or real. One code path."""
    raw = getattr(message, "usage_metadata", None) or {}
    usage = {
        "input_tokens": int(raw.get("input_tokens", 0)),
        "output_tokens": int(raw.get("output_tokens", 0)),
        "total_tokens": int(raw.get("total_tokens", 0)),
    }
    return TokenUsage(**usage, cost_usd=price(usage, pricing))
