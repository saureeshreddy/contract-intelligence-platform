"""
phase2_agents/callbacks.py
==========================
Bridge from LangChain's callback stream into our own telemetry.

WHY NOT LANGSMITH
-----------------
LangChain's built-in observability is LangSmith: hosted, keyed, and it ships
prompts and completions off-box. Our Phase 1 data contract says clause text and
reviewer names must not leave the platform boundary (§3.5), so that is not an
option here regardless of how convenient it is.

It also is not necessary. LangChain's callbacks already hand us everything
LangSmith would display:

    on_llm_start   the fully rendered prompt + serialized model config
    on_llm_end     generations + llm_output/usage_metadata token counts
    on_llm_error   the exception, before it propagates
    on_chain_*     resolved inputs and outputs

Every callback also carries `run_id` and `parent_run_id`, which is LangChain's
own run tree. We record both on each log line, so the LangChain hierarchy is
preserved and joinable even though the OTel spans are opened by our agent code
rather than by this handler.

SPAN ATTRIBUTES
---------------
Named per the OpenTelemetry GenAI semantic conventions (`gen_ai.system`,
`gen_ai.request.model`, `gen_ai.usage.input_tokens`, ...). Those conventions
are still marked experimental upstream, but following them means these traces
drop into any GenAI-aware backend without remapping -- and where they do
change, one file changes.

WHY THIS HANDLER DOES NOT OPEN SPANS
------------------------------------
`Telemetry.span()` is a context manager, which does not map onto a
start/end callback pair without holding the context open across calls. Rather
than fight that, the agent opens the span and this handler logs *into* it --
correlation is automatic because `Telemetry.current_ids()` reads the active
context. At one LLM call per agent invocation the run tree is flat anyway, so
nothing is lost. If chains ever nest deeply, revisit.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult

from common.observability import Telemetry
from phase2_agents.llm import price


class TelemetryCallbackHandler(BaseCallbackHandler):
    """Turns LangChain's callback stream into log events and metrics.

    One instance per run. Thread-safety is not required because the pipeline
    processes clauses in a single thread by design (determinism); if that
    changes, `_started` needs a lock.
    """

    def __init__(
        self,
        telemetry: Telemetry,
        pricing: Dict[str, float],
        model_name: str,
        prompt_version: Optional[str] = None,
    ) -> None:
        self.telemetry = telemetry
        self.pricing = pricing
        self.model_name = model_name
        self.prompt_version = prompt_version
        self.clause_id: Optional[str] = None   # set by the agent before invoking
        self.agent: Optional[str] = None
        self._started: Dict[str, float] = {}
        # Rolled up into run_summary.json
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cost_usd = 0.0
        self.llm_calls = 0
        self.llm_errors = 0

    # -- context set by the calling agent ----------------------------------
    def bind(self, agent: str, clause_id: str, prompt_version: Optional[str] = None) -> None:
        self.agent = agent
        self.clause_id = clause_id
        if prompt_version:
            self.prompt_version = prompt_version

    # -- LLM lifecycle -----------------------------------------------------
    def on_llm_start(
        self,
        serialized: Dict[str, Any],
        prompts: List[str],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        self._started[str(run_id)] = time.perf_counter()
        prompt = prompts[0] if prompts else ""
        self.telemetry.log(
            "DEBUG",
            "llm.request",
            f"{self.agent} -> model for {self.clause_id}",
            **{
                "gen_ai.system": "anthropic" if "anthropic" in self.model_name else "stub",
                "gen_ai.operation.name": "chat",
                "gen_ai.request.model": self.model_name,
                "clause_id": self.clause_id,
                "agent": self.agent,
                "prompt_version": self.prompt_version,
                "prompt_chars": len(prompt),
                "langchain.run_id": str(run_id),
                "langchain.parent_run_id": str(parent_run_id) if parent_run_id else None,
            },
        )

    def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: Any) -> None:
        elapsed_ms = (time.perf_counter() - self._started.pop(str(run_id), time.perf_counter())) * 1000

        usage = self._extract_usage(response)
        cost = price(usage, self.pricing)

        self.llm_calls += 1
        self.total_input_tokens += usage["input_tokens"]
        self.total_output_tokens += usage["output_tokens"]
        self.total_cost_usd = round(self.total_cost_usd + cost, 8)

        self.telemetry.count("llm.calls_total")
        self.telemetry.count("llm.input_tokens_total", usage["input_tokens"])
        self.telemetry.count("llm.output_tokens_total", usage["output_tokens"])
        self.telemetry.count("llm.cost_usd_total", cost)
        self.telemetry.observe("llm.latency_ms", elapsed_ms)
        self.telemetry.observe(f"llm.latency_ms.{self.agent}", elapsed_ms)

        self.telemetry.log(
            "DEBUG",
            "llm.response",
            f"{self.agent} <- model for {self.clause_id} "
            f"({usage['total_tokens']} tok, ${cost:.6f}, {elapsed_ms:.1f}ms)",
            **{
                "gen_ai.response.model": self.model_name,
                "gen_ai.usage.input_tokens": usage["input_tokens"],
                "gen_ai.usage.output_tokens": usage["output_tokens"],
                "llm.cost_usd": cost,
                "llm.latency_ms": round(elapsed_ms, 3),
                "clause_id": self.clause_id,
                "agent": self.agent,
                "prompt_version": self.prompt_version,
                "langchain.run_id": str(run_id),
            },
        )

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        self._started.pop(str(run_id), None)
        self.llm_errors += 1
        self.telemetry.count("llm.errors_total")
        self.telemetry.error(
            "llm.error",
            f"{type(error).__name__} calling model for {self.clause_id}: {error}",
            clause_id=self.clause_id,
            agent=self.agent,
            error_type=type(error).__name__,
        )

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _extract_usage(response: LLMResult) -> Dict[str, int]:
        """Read token usage wherever the provider put it.

        `usage_metadata` on the message is the modern, provider-neutral
        location and is what our stub populates. `llm_output.token_usage` is
        the older per-provider shape. Supporting both means switching
        providers does not silently zero the budget.
        """
        for generations in response.generations:
            for generation in generations:
                message = getattr(generation, "message", None)
                usage = getattr(message, "usage_metadata", None)
                if usage:
                    return {
                        "input_tokens": int(usage.get("input_tokens", 0)),
                        "output_tokens": int(usage.get("output_tokens", 0)),
                        "total_tokens": int(usage.get("total_tokens", 0)),
                    }
        legacy = (response.llm_output or {}).get("token_usage", {})
        input_tokens = int(legacy.get("prompt_tokens", 0))
        output_tokens = int(legacy.get("completion_tokens", 0))
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }

    def summary(self) -> Dict[str, Any]:
        return {
            "llm_calls": self.llm_calls,
            "llm_errors": self.llm_errors,
            "input_tokens": self.total_input_tokens,
            "output_tokens": self.total_output_tokens,
            "total_tokens": self.total_input_tokens + self.total_output_tokens,
            "cost_usd": round(self.total_cost_usd, 6),
        }
