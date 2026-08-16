"""
common/observability.py
=======================
OpenTelemetry-shaped observability for the Contract Intelligence Pipeline.

WHY THIS EXISTS
---------------
On-call needs to answer "is this healthy, what do I watch, and what is normal
vs concerning?"  That is unanswerable unless the pipeline emits telemetry while
it runs.  This module is the single place that
decides *how* we emit it, so that every stage (ingest, drift, normalize,
supersede, and later the Phase 2 agents) produces telemetry in one shape.

THE THREE SIGNALS
-----------------
We follow the OpenTelemetry data model.  Everything lands under
``output/logs/`` at the repo root (see default_log_dir below):

  traces  -> traces.jsonl    one line per span: name, ids, duration, attributes
  logs    -> pipeline.jsonl  one line per event, carrying trace_id/span_id so a
                             log line can be joined to the span that emitted it
  metrics -> metrics.jsonl   counters + histograms, one snapshot per process

TWO BACKENDS, ONE OUTPUT
------------------------
Pipeline code only ever touches the ``Telemetry`` facade.  Underneath:

  * If ``opentelemetry-sdk`` is installed we use the **real SDK** for tracing.
    Spans are exported through ``JsonlSpanExporter`` below, and if the
    environment variable ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set we *also*
    attach an OTLP exporter -- so shipping traces to a real collector in
    production is one env var and zero code changes.

  * If it is not installed we fall back to a small shim that implements the
    same surface and writes byte-identical files.

The fallback is a deliberate design decision, not laziness.  The requirements
requires an engineer to clone the repo and run it with no setup; a hard
dependency on the OTel SDK would break that.  It is also the same principle
used everywhere else in this pipeline: **degrade visibly, never silently.**
The active backend is recorded in ``metrics.jsonl`` and on the root span, so
you can always tell which path produced a given artifact.

KNOWN LIMITATION (documented, not hidden)
-----------------------------------------
Metrics are aggregated in-process and written as a snapshot at exit rather
than exported through the OTel metrics SDK.  The metrics SDK's exporter
interface has moved between minor versions and pinning it would add fragility
for no operator-visible benefit.  In production these counters become OTLP
metrics; the aggregation keys are already named to match.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

SERVICE_VERSION = "0.1.0"

# --------------------------------------------------------------------------
# Optional real OpenTelemetry SDK
# --------------------------------------------------------------------------
try:  # pragma: no cover - exercised only when the SDK is installed
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import (
        BatchSpanProcessor,
        SimpleSpanProcessor,
        SpanExporter,
        SpanExportResult,
    )

    OTEL_AVAILABLE = True
except Exception:  # ImportError, or a partially installed SDK
    OTEL_AVAILABLE = False
    SpanExporter = object  # type: ignore[assignment,misc]


def utc_now() -> str:
    """RFC3339 UTC timestamp. One helper so every artifact agrees on format."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def default_log_dir() -> Path:
    """Telemetry lands at the REPO ROOT, not inside a phase directory.

    Observability is cross-cutting: the whole point is to follow one trace from
    "clause CLZ-2025-0018 landed at Bronze record 5" through to "the risk agent
    flagged it and a human overrode the flag". Splitting traces across
    phase1_ingestion/output/logs/ and phase2_agents/output/logs/ would break
    exactly the join the telemetry exists to support.

    Phase *deliverables* still live in their own phase directory (Bronze,
    Silver, drift reports, the Phase 2 audit log). The distinction that matters:

        telemetry    cross-cutting, samplable, expirable  -> output/logs/
        deliverables owned by one phase, durable          -> <phase>/output/

    The Phase 2 audit log is a deliverable, not telemetry. It is a legal record,
    so it stays in phase2_agents/output/audit_log.json and carries `trace_id` as
    a join key rather than being merged into this stream.

    Override with CI_LOG_DIR when running under an orchestrator that collects
    logs from a fixed path.
    """
    override = os.environ.get("CI_LOG_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[1] / "output" / "logs"


# --------------------------------------------------------------------------
# File writers
# --------------------------------------------------------------------------
class JsonlWriter:
    """Thread-safe append-only JSONL writer.

    Append-only matters: telemetry for a run that later crashed is still
    evidence, so nothing here ever truncates or rewrites.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._fh = self.path.open("a", encoding="utf-8")

    def write(self, record: Dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, sort_keys=False)
        with self._lock:
            self._fh.write(line + "\n")
            self._fh.flush()

    def close(self) -> None:
        with self._lock:
            if not self._fh.closed:
                self._fh.close()


def span_to_record(
    *,
    name: str,
    trace_id: str,
    span_id: str,
    parent_span_id: Optional[str],
    start_ns: int,
    end_ns: int,
    attributes: Dict[str, Any],
    status: str,
    resource: Dict[str, Any],
    events: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """The single definition of a span line. Both backends emit exactly this."""
    return {
        "name": name,
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_span_id": parent_span_id,
        "start_time": datetime.fromtimestamp(start_ns / 1e9, timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
        "end_time": datetime.fromtimestamp(end_ns / 1e9, timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
        "duration_ms": round((end_ns - start_ns) / 1e6, 3),
        "status": status,
        "attributes": attributes,
        "events": events,
        "resource": resource,
    }


if OTEL_AVAILABLE:  # pragma: no cover - only with the SDK installed

    class JsonlSpanExporter(SpanExporter):  # type: ignore[misc]
        """Exports real OTel spans into the same traces.jsonl format."""

        def __init__(self, writer: JsonlWriter, resource: Dict[str, Any]) -> None:
            self._writer = writer
            self._resource = resource

        def export(self, spans) -> "SpanExportResult":  # type: ignore[override]
            for span in spans:
                ctx = span.get_span_context()
                parent = span.parent.span_id if span.parent else None
                self._writer.write(
                    span_to_record(
                        name=span.name,
                        trace_id=format(ctx.trace_id, "032x"),
                        span_id=format(ctx.span_id, "016x"),
                        parent_span_id=format(parent, "016x") if parent else None,
                        start_ns=span.start_time,
                        end_ns=span.end_time or span.start_time,
                        attributes=dict(span.attributes or {}),
                        status=span.status.status_code.name if span.status else "UNSET",
                        resource=self._resource,
                        events=[
                            {"name": e.name, "attributes": dict(e.attributes or {})}
                            for e in span.events
                        ],
                    )
                )
            return SpanExportResult.SUCCESS

        def shutdown(self) -> None:
            return None


# --------------------------------------------------------------------------
# Fallback shim: same surface, same output, no dependencies
# --------------------------------------------------------------------------
_current_span: contextvars.ContextVar[Optional["ShimSpan"]] = contextvars.ContextVar(
    "ci_current_span", default=None
)


@dataclass
class ShimSpan:
    name: str
    trace_id: str
    span_id: str
    parent_span_id: Optional[str]
    start_ns: int
    attributes: Dict[str, Any] = field(default_factory=dict)
    events: List[Dict[str, Any]] = field(default_factory=list)
    status: str = "UNSET"

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def add_event(self, name: str, attributes: Optional[Dict[str, Any]] = None) -> None:
        self.events.append({"name": name, "attributes": attributes or {}})

    def set_status_error(self, description: str) -> None:
        self.status = "ERROR"
        self.attributes["error.message"] = description

    def record_exception(self, exc: BaseException) -> None:
        self.add_event(
            "exception",
            {"exception.type": type(exc).__name__, "exception.message": str(exc)},
        )


class ShimTracer:
    """Minimal tracer: nested spans, contextvar-based parenting, JSONL export."""

    def __init__(self, writer: JsonlWriter, resource: Dict[str, Any]) -> None:
        self._writer = writer
        self._resource = resource

    @contextlib.contextmanager
    def start_span(self, name: str, attributes: Optional[Dict[str, Any]] = None) -> Iterator[ShimSpan]:
        parent = _current_span.get()
        span = ShimSpan(
            name=name,
            trace_id=parent.trace_id if parent else os.urandom(16).hex(),
            span_id=os.urandom(8).hex(),
            parent_span_id=parent.span_id if parent else None,
            start_ns=time.time_ns(),
            attributes=dict(attributes or {}),
        )
        token = _current_span.set(span)
        try:
            yield span
        except BaseException as exc:
            span.record_exception(exc)
            span.set_status_error(str(exc))
            raise
        finally:
            _current_span.reset(token)
            if span.status == "UNSET":
                span.status = "OK"
            self._writer.write(
                span_to_record(
                    name=span.name,
                    trace_id=span.trace_id,
                    span_id=span.span_id,
                    parent_span_id=span.parent_span_id,
                    start_ns=span.start_ns,
                    end_ns=time.time_ns(),
                    attributes=span.attributes,
                    status=span.status,
                    resource=self._resource,
                    events=span.events,
                )
            )


# --------------------------------------------------------------------------
# In-process metric aggregation
# --------------------------------------------------------------------------
class Metrics:
    """Counters and histograms, snapshotted to metrics.json at shutdown.

    Deliberately tiny.  The names here are the ones a production dashboard
    would chart, so the runbook can reference them by name today and they
    keep working when this is swapped for OTLP metrics later.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.counters: Dict[str, float] = {}
        self.histograms: Dict[str, List[float]] = {}

    def count(self, name: str, value: float = 1.0) -> None:
        with self._lock:
            self.counters[name] = self.counters.get(name, 0.0) + value

    def observe(self, name: str, value: float) -> None:
        with self._lock:
            self.histograms.setdefault(name, []).append(value)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            hist: Dict[str, Any] = {}
            for name, values in self.histograms.items():
                ordered = sorted(values)
                hist[name] = {
                    "count": len(ordered),
                    "sum": round(sum(ordered), 3),
                    "min": round(ordered[0], 3),
                    "max": round(ordered[-1], 3),
                    "avg": round(sum(ordered) / len(ordered), 3),
                    "p95": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 3),
                }
            return {"counters": dict(self.counters), "histograms": hist}


# --------------------------------------------------------------------------
# The facade the pipeline actually uses
# --------------------------------------------------------------------------
class Telemetry:
    """One object per process. Owns the tracer, the log writer and metrics."""

    def __init__(self, service_name: str, log_dir: Path, run_id: Optional[str] = None) -> None:
        self.service_name = service_name
        self.run_id = run_id
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)

        self.resource: Dict[str, Any] = {
            "service.name": service_name,
            "service.version": SERVICE_VERSION,
            "telemetry.sdk.language": "python",
        }
        if run_id:
            self.resource["run.id"] = run_id

        self._trace_writer = JsonlWriter(log_dir / "traces.jsonl")
        self._log_writer = JsonlWriter(log_dir / "pipeline.jsonl")
        self.metrics = Metrics()
        self.backend = "opentelemetry-sdk" if OTEL_AVAILABLE else "builtin-shim"
        # Files are always written; only the console mirror is optional.
        # Telemetry must never go dark just because output is noisy.
        self.console = os.environ.get("CI_TELEMETRY_QUIET", "") not in ("1", "true", "yes")

        if OTEL_AVAILABLE:  # pragma: no cover
            provider = TracerProvider(resource=Resource.create(self.resource))
            provider.add_span_processor(
                SimpleSpanProcessor(JsonlSpanExporter(self._trace_writer, self.resource))
            )
            endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
            if endpoint:
                # Production path: one env var, no code change.
                try:
                    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                        OTLPSpanExporter,
                    )

                    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
                    self.resource["otlp.endpoint"] = endpoint
                except Exception as exc:
                    self.log("WARN", "otel.otlp_exporter_unavailable", error=str(exc))
            self._provider = provider
            self._otel_tracer = otel_trace.get_tracer(service_name, SERVICE_VERSION, provider)
        else:
            self._provider = None
            self._shim_tracer = ShimTracer(self._trace_writer, self.resource)

    # -- correlation --------------------------------------------------------
    def current_ids(self) -> Dict[str, Optional[str]]:
        """trace_id/span_id of the active span, for log correlation."""
        if OTEL_AVAILABLE:  # pragma: no cover
            span = otel_trace.get_current_span()
            ctx = span.get_span_context()
            if ctx and ctx.is_valid:
                return {
                    "trace_id": format(ctx.trace_id, "032x"),
                    "span_id": format(ctx.span_id, "016x"),
                }
            return {"trace_id": None, "span_id": None}
        span = _current_span.get()
        if span is None:
            return {"trace_id": None, "span_id": None}
        return {"trace_id": span.trace_id, "span_id": span.span_id}

    # -- spans --------------------------------------------------------------
    @contextlib.contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[Any]:
        """Start a span. Yields an object with .set_attribute()/.add_event()."""
        attrs = {k: v for k, v in attributes.items() if v is not None}
        if OTEL_AVAILABLE:  # pragma: no cover
            with self._otel_tracer.start_as_current_span(name, attributes=attrs) as span:
                yield _OtelSpanAdapter(span)
        else:
            with self._shim_tracer.start_span(name, attrs) as span:
                yield span

    # -- logs ---------------------------------------------------------------
    def log(self, severity: str, event: str, message: Optional[str] = None, **attributes: Any) -> None:
        """Emit one OTel-shaped log record, correlated to the active span.

        `event` is a stable machine-readable key (e.g. "record.committed").
        Alerts and dashboards key off `event`, never off the human message.
        """
        ids = self.current_ids()
        record = {
            "timestamp": utc_now(),
            "severity": severity,
            "event": event,
            "body": message or event,
            "trace_id": ids["trace_id"],
            "span_id": ids["span_id"],
            "attributes": {k: v for k, v in attributes.items() if v is not None},
            "resource": self.resource,
        }
        self._log_writer.write(record)
        if self.console:
            # Human-readable mirror so an operator sees progress. Suppressed
            # under CI_TELEMETRY_QUIET, which the test suite sets: 60 tests
            # each running the pipeline would otherwise bury the results
            # under a few hundred lines of their own logs.
            prefix = {"DEBUG": "  ", "INFO": "  ", "WARN": "! ", "ERROR": "X "}.get(severity, "  ")
            print(f"{prefix}{severity:<5} {event:<34} {message or ''}".rstrip(), flush=True)

    def info(self, event: str, message: Optional[str] = None, **attrs: Any) -> None:
        self.log("INFO", event, message, **attrs)

    def warn(self, event: str, message: Optional[str] = None, **attrs: Any) -> None:
        self.log("WARN", event, message, **attrs)

    def error(self, event: str, message: Optional[str] = None, **attrs: Any) -> None:
        self.log("ERROR", event, message, **attrs)

    # -- metrics ------------------------------------------------------------
    def count(self, name: str, value: float = 1.0) -> None:
        self.metrics.count(name, value)

    def observe(self, name: str, value: float) -> None:
        self.metrics.observe(name, value)

    # -- lifecycle ----------------------------------------------------------
    def shutdown(self) -> None:
        if OTEL_AVAILABLE and self._provider is not None:  # pragma: no cover
            self._provider.shutdown()
        # Appended, not overwritten: the pipeline is several processes and the
        # last one to exit must not erase the others' numbers. Same append-only
        # rule as traces and logs.
        metrics_writer = JsonlWriter(self.log_dir / "metrics.jsonl")
        try:
            metrics_writer.write(
                {
                    "service": self.service_name,
                    "run_id": self.run_id,
                    "telemetry_backend": self.backend,
                    "snapshot_at": utc_now(),
                    **self.metrics.snapshot(),
                }
            )
        finally:
            metrics_writer.close()
        self._trace_writer.close()
        self._log_writer.close()


class _OtelSpanAdapter:  # pragma: no cover - only with the SDK installed
    """Gives real OTel spans the same tiny surface as ShimSpan."""

    def __init__(self, span: Any) -> None:
        self._span = span

    def set_attribute(self, key: str, value: Any) -> None:
        self._span.set_attribute(key, value)

    def add_event(self, name: str, attributes: Optional[Dict[str, Any]] = None) -> None:
        self._span.add_event(name, attributes or {})

    def set_status_error(self, description: str) -> None:
        from opentelemetry.trace import Status, StatusCode

        self._span.set_status(Status(StatusCode.ERROR, description))

    def record_exception(self, exc: BaseException) -> None:
        self._span.record_exception(exc)
