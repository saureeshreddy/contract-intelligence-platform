"""
phase2_agents/agents/base.py
============================
What the agents share: config loading, prompt versioning, the LLM call path,
and structured-output parsing with a bounded retry.

Only genuinely common machinery lives here. Each agent's prompt, schema,
escalation rule and rule-vs-model split stays in its own file, because those
are the parts a person actually changes.

THE CALL PATH
-------------
    render prompt (versioned)
      -> cache lookup           hit  -> decided_by=cache, no spend
      -> model.invoke           miss -> callbacks record tokens/cost/latency
      -> parse + validate       fail -> ONE reworded retry
      -> still failing          -> escalate. Never fabricate, never default.

The retry is deliberately not `model.with_retry`. That handles transport
failures (429, 5xx) by repeating the identical request, which is correct for a
network blip and useless for a malformed response. A parse failure needs a
*different* request -- so we append an explicit correction and try once. Twice
would be optimism.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Type

from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, ValidationError

from common.observability import Telemetry, utc_now
from phase2_agents.callbacks import TelemetryCallbackHandler
from phase2_agents.llm import DecisionCache, usage_from_message
from phase2_agents.models import AuditRecord, DecidedBy, DecisionStatus, TokenUsage

ROOT = Path(__file__).resolve().parents[2]
PROMPT_DIR = ROOT / "phase2_agents" / "config" / "prompts"


class ParseFailure(RuntimeError):
    """The model returned something we could not turn into a decision."""


@dataclass
class PromptTemplate:
    """A versioned prompt, loaded from config/prompts/<version>.json."""

    version: str
    template: str
    output_schema: str
    author: str
    created: str

    @classmethod
    def load(cls, version: str) -> "PromptTemplate":
        path = PROMPT_DIR / f"{version}.json"
        if not path.exists():
            raise FileNotFoundError(
                f"Active prompt version {version!r} not found at {path}. "
                f"agents.yml names the active version; the file must exist."
            )
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            version=data["version"],
            template=data["template"],
            output_schema=data["output_schema"],
            author=data.get("author", "unknown"),
            created=data.get("created", "unknown"),
        )

    def render(self, **values: Any) -> str:
        return self.template.format(**values)


@dataclass
class AgentResult:
    """One agent's answer for one clause, plus how it was reached."""

    output: Optional[BaseModel]
    decided_by: DecidedBy
    status: DecisionStatus
    usage: TokenUsage
    latency_ms: float
    detail: Optional[str] = None
    escalation_reason: Optional[str] = None


class Agent:
    """Base for the two model-backed agents.

    The auditor does not inherit from this: it makes no LLM calls, so giving
    it a model, a budget and a prompt version would be misleading.
    """

    name: str = "agent"
    output_model: Type[BaseModel]

    def __init__(
        self,
        model: BaseChatModel,
        prompt: PromptTemplate,
        telemetry: Telemetry,
        handler: TelemetryCallbackHandler,
        cache: DecisionCache,
        pricing: Dict[str, float],
        config: Dict[str, Any],
    ) -> None:
        self.model = model
        self.prompt = prompt
        self.telemetry = telemetry
        self.handler = handler
        self.cache = cache
        self.pricing = pricing
        self.config = config
        self.escalate_below_confidence = float(config.get("escalate_below_confidence", 0.6))
        self.prefer_rules = bool(config.get("prefer_rules", True))

    # -- the LLM call ------------------------------------------------------
    def call_model(self, clause_id: str, **prompt_values: Any) -> Tuple[BaseModel, DecidedBy, TokenUsage, float]:
        """Render, cache-check, invoke, parse. Raises ParseFailure if unusable."""
        rendered = self.prompt.render(**prompt_values)
        model_name = getattr(self.model, "model_name", None) or self.handler.model_name
        key = DecisionCache.key(rendered, str(model_name), self.prompt.version)

        cached = self.cache.get(key)
        if cached is not None:
            self.telemetry.count("llm.cache_hits_total")
            self.telemetry.log(
                "DEBUG",
                "llm.cache_hit",
                f"{self.name} reused a decision for {clause_id}",
                clause_id=clause_id,
                agent=self.name,
                prompt_version=self.prompt.version,
            )
            # Zero usage: a cache hit costs nothing, and pretending otherwise
            # would corrupt the budget in the pessimistic direction.
            return self.output_model.model_validate(cached), DecidedBy.CACHE, TokenUsage(), 0.0

        self.telemetry.count("llm.cache_misses_total")
        self.handler.bind(self.name, clause_id, self.prompt.version)

        started = time.perf_counter()
        response = self.model.invoke(rendered, config={"callbacks": [self.handler]})
        latency_ms = (time.perf_counter() - started) * 1000
        usage = usage_from_message(response, self.pricing)

        try:
            parsed = self._parse(response.content)
        except ParseFailure as first_failure:
            # One reworded retry, then give up and escalate.
            self.telemetry.count("llm.parse_failures_total")
            self.telemetry.warn(
                "llm.parse_failure",
                f"{self.name} could not parse the model response for {clause_id}; retrying once.",
                clause_id=clause_id,
                agent=self.name,
                error=str(first_failure),
            )
            correction = (
                f"{rendered}\n\nYour previous response could not be parsed: {first_failure}\n"
                f"Return ONLY valid JSON matching {self.prompt.output_schema}. No prose, no code fences."
            )
            retry = self.model.invoke(correction, config={"callbacks": [self.handler]})
            latency_ms = (time.perf_counter() - started) * 1000
            usage = usage + usage_from_message(retry, self.pricing)
            parsed = self._parse(retry.content)  # a second failure propagates

        self.cache.put(key, parsed.model_dump(mode="json"))
        return parsed, DecidedBy.MODEL, usage, latency_ms

    def _parse(self, content: Any) -> BaseModel:
        """Text -> validated object. Fails loudly rather than defaulting.

        A model that returns nonsense must not produce a plausible-looking
        record; a null category silently becomes a wrong risk rubric.
        """
        text = content if isinstance(content, str) else json.dumps(content)
        text = text.strip()
        # Tolerate a code fence, because real models emit them regularly.
        fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
        if fenced:
            text = fenced.group(1)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ParseFailure(f"response was not valid JSON: {exc}") from exc
        try:
            return self.output_model.model_validate(payload)
        except ValidationError as exc:
            raise ParseFailure(f"JSON did not match {self.output_model.__name__}: {exc}") from exc

    # -- audit -------------------------------------------------------------
    def audit(
        self,
        *,
        clause: Dict[str, Any],
        action: str,
        result: AgentResult,
        run_id: str,
        firm_standards_version: Optional[str] = None,
        extra_detail: Optional[str] = None,
    ) -> AuditRecord:
        """Build the ledger entry for one agent action.

        `model_output` and `effective_value` are both set here and are equal at
        this point -- the model's opinion IS the effective value until a human
        says otherwise. The auditor later fills `human_decision` and rewrites
        `effective_value` on a NEW record; this one is never edited.
        """
        ids = self.telemetry.current_ids()
        payload = result.output.model_dump(mode="json") if result.output else None
        return AuditRecord(
            timestamp=utc_now(),
            agent=self.name,
            clause_id=clause["clause_id"],
            action=action,
            model_output=payload,
            human_decision=None,
            effective_value=payload,
            decided_by=result.decided_by,
            status=result.status,
            run_id=run_id,
            trace_id=ids["trace_id"],
            span_id=ids["span_id"],
            prompt_version=self.prompt.version if result.decided_by != DecidedBy.RULE else None,
            model_name=self.handler.model_name if result.decided_by == DecidedBy.MODEL else None,
            firm_standards_version=firm_standards_version,
            usage=result.usage,
            latency_ms=round(result.latency_ms, 3),
            lineage=clause.get("_lineage"),
            detail=extra_detail or result.detail,
        )
