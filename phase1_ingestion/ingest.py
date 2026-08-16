#!/usr/bin/env python3
"""
phase1_ingestion/ingest.py
==========================
Land contract clause data into the Bronze layer.  This is the Phase 1 entry
point; everything else in Phase 1 is called from here or reads its output.

WHAT IT DOES
------------
1. Reads a source JSON file exactly as the contract API delivered it and
   snapshots the raw bytes                                        (P1-R1)
2. Writes each clause into a Bronze run directory partitioned by ingestion
   date, wrapped in a metadata envelope, payload untouched        (P1-R1)
3. Commits a checkpoint after every record, so the job survives a crash, a
   kill, or a deliberate pause and resumes exactly where it stopped (P1-R3)
4. Hands the parsed records to drift.py and writes a drift report  (P1-R2)

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
* No transformation, renaming or type coercion. That is normalize.py's job,
  in a separate stage, on purpose -- the moment ingestion "just fixes" a
  renamed field, the audit trail of what actually arrived is gone.
* No validation gate. The platform requires that changed data is never silently
  dropped, and the most reliable way to guarantee that is to have no code
  path capable of dropping a record.

DURABILITY MODEL
----------------
Per record:   append line -> flush + fsync -> advance checkpoint (atomic)

fsync before advancing the checkpoint is the whole point. Without it the OS
may still be holding the write in memory, the checkpoint would claim a record
is safe when it is not, and a resume would skip a record that was never
written -- silent data loss.

There is a deliberate crash window *between* the fsync and the checkpoint
advance. Crash there and the record is on disk but unacknowledged, so resume
writes it a second time. That makes Bronze **at-least-once**, with
`_bronze.record_hash` as the documented dedupe key applied by normalize.py.
The inverse ordering (checkpoint first) would be at-most-once and could lose
a clause entirely. In a contract compliance system a duplicate you can detect
beats a disappearance you cannot, so we chose replay over loss. See
docs/architecture_note.md, AD-3.

USAGE
-----
  python phase1_ingestion/ingest.py --source data/clauses_batch_1.json
  python phase1_ingestion/ingest.py --source data/clauses_batch_2.json --fail-on-drift
  python phase1_ingestion/ingest.py --source data/clauses_batch_2.json --simulate-crash-after 5
  python phase1_ingestion/ingest.py --source data/clauses_batch_2.json     # resumes

EXIT CODES
  0  success
  1  unrecoverable error
  2  landed successfully, but drift needs human attention (--fail-on-drift)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# The repo root, so `common` imports work no matter where you run this from.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.observability import Telemetry, default_log_dir, utc_now  # noqa: E402
from phase1_ingestion import drift  # noqa: E402

PHASE_DIR = ROOT / "phase1_ingestion"
OUTPUT_DIR = PHASE_DIR / "output"
BRONZE_ROOT = OUTPUT_DIR / "bronze"
DRIFT_DIR = OUTPUT_DIR / "drift"
LOG_DIR = default_log_dir()  # repo-root output/logs/ -- telemetry is cross-phase
REGISTRY_PATH = DRIFT_DIR / "schema_registry.json"
STOP_FILE = OUTPUT_DIR / "STOP"

CONTRACT_VERSION = "1.0.0"


class SimulatedCrash(RuntimeError):
    """Raised by --simulate-crash-after so resume is demonstrable, not claimed."""


class PausedByOperator(RuntimeError):
    """Raised when the STOP file appears. Same mechanism as a crash, on purpose."""


# --------------------------------------------------------------------------
# Hashing helpers
# --------------------------------------------------------------------------
def canonical_json(obj: Any) -> str:
    """Deterministic serialisation, so the same payload always hashes the same."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def display_path(path: Path) -> str:
    """Repo-relative when possible, absolute otherwise (tests use temp dirs)."""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def record_hash(payload: Dict[str, Any]) -> str:
    return "sha256:" + sha256_hex(canonical_json(payload).encode("utf-8"))


def compute_run_id(source_path: Path, source_sha: str) -> str:
    """Run id is derived from file *content*, not from the clock.

    Consequence, and the reason for the choice: re-running the same file
    resumes the same run instead of creating a second copy, while a genuinely
    different export gets a different run id automatically. "The source file
    changed underneath a half-finished run" therefore cannot happen -- it is
    structurally a different run. (We still verify the full hash below as
    defence against an 8-character prefix collision.)
    """
    return f"{source_path.stem}__{source_sha[:8]}"


# --------------------------------------------------------------------------
# Run directory + checkpoint
# --------------------------------------------------------------------------
def find_existing_run_dir(run_id: str) -> Optional[Path]:
    if not BRONZE_ROOT.exists():
        return None
    matches = sorted(BRONZE_ROOT.glob(f"ingest_date=*/run_id={run_id}"))
    return matches[0] if matches else None


def create_run_dir(run_id: str, ingested_at: str) -> Path:
    ingest_date = ingested_at[:10]  # partition by ingestion timestamp (P1-R1)
    run_dir = BRONZE_ROOT / f"ingest_date={ingest_date}" / f"run_id={run_id}"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def load_checkpoint(run_dir: Path) -> Optional[Dict[str, Any]]:
    path = run_dir / "_checkpoint.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # A torn checkpoint means the atomic replace did not complete, so the
        # previous checkpoint is still authoritative. Treat as "no progress
        # acknowledged" and replay -- safe, because replay is deduped.
        return None


def write_checkpoint(run_dir: Path, checkpoint: Dict[str, Any]) -> None:
    """Atomic checkpoint update: write a temp file, fsync it, then rename.

    os.replace is atomic on POSIX and on Windows, so a reader never sees a
    half-written checkpoint.
    """
    checkpoint["updated_at"] = utc_now()
    path = run_dir / "_checkpoint.json"
    tmp = run_dir / "_checkpoint.json.tmp"
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(checkpoint, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def archive_previous_attempt(run_dir: Path, telemetry: Telemetry) -> None:
    """--restart: set the old attempt aside rather than deleting it.

    Bronze is append-only history. Even a failed attempt is evidence, so
    nothing in this pipeline deletes landed data.
    """
    stamp = utc_now().replace(":", "").replace("-", "")
    for name in ("part-0000.jsonl", "_checkpoint.json", "_manifest.json"):
        src = run_dir / name
        if src.exists():
            dest = run_dir / f"{name}.superseded.{stamp}"
            src.rename(dest)
            telemetry.warn("run.attempt_archived", f"{name} -> {dest.name}", file=name)


# --------------------------------------------------------------------------
# The ingest itself
# --------------------------------------------------------------------------
def read_source(source_path: Path) -> Tuple[bytes, Dict[str, Any], List[Dict[str, Any]], Dict[str, Any]]:
    raw = source_path.read_bytes()
    document = json.loads(raw.decode("utf-8"))
    metadata = document.get("metadata", {})
    clauses = document.get("clauses", [])
    if not isinstance(clauses, list):
        raise ValueError(f"{source_path.name}: expected 'clauses' to be a list")
    return raw, document, clauses, metadata


def build_envelope(
    *,
    payload: Dict[str, Any],
    run_id: str,
    source_path: Path,
    source_sha: str,
    api_version: str,
    ingested_at: str,
    index: int,
    schema_fingerprint: str,
) -> Dict[str, Any]:
    """Metadata wraps the payload; it is never merged into it.

    Merging would (a) transform the record, violating "preserve raw as-is",
    and (b) risk colliding with a future API field of the same name. `payload`
    round-trips byte-equivalent to the source object; a test asserts it.
    """
    return {
        "_bronze": {
            "contract_version": CONTRACT_VERSION,
            "run_id": run_id,
            "source_file": source_path.name,
            "source_sha256": source_sha,
            "source_api_version": api_version,
            "ingested_at": ingested_at,
            "record_index": index,
            "record_hash": record_hash(payload),
            "schema_fingerprint": schema_fingerprint,
        },
        "payload": payload,
    }


def ingest(
    source_path: Path,
    *,
    telemetry: Telemetry,
    simulate_crash_after: Optional[int] = None,
    crash_in_commit_window: bool = False,
    restart: bool = False,
) -> Dict[str, Any]:
    raw, _document, clauses, metadata = read_source(source_path)
    source_sha = sha256_hex(raw)
    run_id = compute_run_id(source_path, source_sha)
    api_version = str(metadata.get("api_version", "unknown"))
    source_name = str(metadata.get("source", "unknown"))

    run_dir = find_existing_run_dir(run_id)
    ingested_at = utc_now()
    if run_dir is None:
        run_dir = create_run_dir(run_id, ingested_at)

    checkpoint = load_checkpoint(run_dir)
    if restart and checkpoint is not None:
        archive_previous_attempt(run_dir, telemetry)
        checkpoint = None

    # ---- decide where to start (the four restart cases) -------------------
    start_index = 0
    if checkpoint is not None:
        if checkpoint.get("source_sha256") != source_sha:
            # Only reachable via an 8-char run-id prefix collision. Refuse
            # rather than interleave two datasets in one part file.
            telemetry.error(
                "resume.source_mismatch",
                "Checkpoint belongs to different file content; refusing to resume.",
                run_id=run_id,
            )
            raise RuntimeError(
                f"Checkpoint in {run_dir} was written for a different source file. "
                f"Use --restart to archive it and start a new attempt."
            )
        if checkpoint.get("status") == "complete":
            telemetry.info(
                "run.already_complete",
                f"{run_id} already complete ({checkpoint['records_in_source']} records); nothing to do.",
                run_id=run_id,
            )
            telemetry.count("ingest.runs_noop_total")
            return {"run_id": run_id, "run_dir": str(run_dir), "status": "already_complete"}
        start_index = int(checkpoint.get("last_committed_index", -1)) + 1
        telemetry.warn(
            "run.resumed",
            f"Resuming {run_id} at record {start_index} "
            f"({len(clauses) - start_index} of {len(clauses)} remaining).",
            run_id=run_id,
            resume_from_index=start_index,
        )
        telemetry.count("ingest.runs_resumed_total")
        telemetry.count("ingest.records_skipped_on_resume_total", start_index)
        ingested_at = checkpoint.get("ingested_at", ingested_at)

    # ---- raw snapshot -----------------------------------------------------
    # The strongest possible reading of "preserve raw data as-is": even if our
    # record parsing has a bug, the original bytes are in the lake and their
    # hash is in the manifest.
    snapshot = run_dir / "_source_snapshot.json"
    if not snapshot.exists():
        snapshot.write_bytes(raw)

    # ---- describe the shape (observation only, gates nothing) -------------
    inventory = drift.build_inventory(clauses)
    schema_fingerprint = drift.fingerprint(inventory)

    if checkpoint is None:
        checkpoint = {
            "run_id": run_id,
            "source_file": source_path.name,
            "source_sha256": source_sha,
            "source_api_version": api_version,
            "schema_fingerprint": schema_fingerprint,
            "records_in_source": len(clauses),
            "last_committed_index": -1,
            "records_written": 0,
            "status": "in_progress",
            "ingested_at": ingested_at,
        }
        write_checkpoint(run_dir, checkpoint)

    part_path = run_dir / "part-0000.jsonl"
    paused = False
    crashed = False

    with telemetry.span(
        "ingest.land_records",
        **{
            "run.id": run_id,
            "source.file": source_path.name,
            "source.api_version": api_version,
            "bronze.records_total": len(clauses),
            "bronze.start_index": start_index,
        },
    ) as span:
        with part_path.open("a", encoding="utf-8") as part:
            for index in range(start_index, len(clauses)):
                # Operator pause. Checked between records so we always stop on
                # a clean boundary. Same mechanism as the Phase 2 kill switch.
                if STOP_FILE.exists():
                    paused = True
                    span.add_event("paused_by_operator", {"at_index": index})
                    telemetry.warn(
                        "run.paused",
                        f"STOP file present; pausing before record {index}. "
                        f"Delete {STOP_FILE.name} and re-run to resume.",
                        run_id=run_id,
                        at_index=index,
                    )
                    break

                payload = clauses[index]
                envelope = build_envelope(
                    payload=payload,
                    run_id=run_id,
                    source_path=source_path,
                    source_sha=source_sha,
                    api_version=api_version,
                    ingested_at=ingested_at,
                    index=index,
                    schema_fingerprint=schema_fingerprint,
                )

                started = time.perf_counter()
                # --- the durability protocol, three lines, in this order ---
                part.write(json.dumps(envelope, ensure_ascii=False) + "\n")
                part.flush()
                os.fsync(part.fileno())

                # THE CRASH WINDOW. The record is durable but unacknowledged.
                # Dying here is what makes Bronze at-least-once: resume will
                # replay this record and normalize.py will dedupe it.
                if crash_in_commit_window and (index + 1) >= (simulate_crash_after or 0):
                    span.add_event("simulated_crash_in_commit_window", {"at_index": index})
                    raise SimulatedCrash(
                        f"Simulated crash INSIDE the commit window at record {index}: the record "
                        f"is on disk but the checkpoint still says {checkpoint['last_committed_index']}. "
                        f"Resuming will replay it, and normalize.py will drop the duplicate."
                    )
                # -----------------------------------------------------------
                checkpoint["last_committed_index"] = index
                checkpoint["records_written"] = int(checkpoint.get("records_written", 0)) + 1
                write_checkpoint(run_dir, checkpoint)

                elapsed_ms = (time.perf_counter() - started) * 1000
                telemetry.observe("ingest.record_commit_duration_ms", elapsed_ms)
                telemetry.count("ingest.records_landed_total")
                telemetry.log(
                    "DEBUG",
                    "record.committed",
                    f"{payload.get('clause_id', '<no id>')} committed at index {index}",
                    clause_id=payload.get("clause_id"),
                    record_index=index,
                    record_hash=envelope["_bronze"]["record_hash"],
                    duration_ms=round(elapsed_ms, 3),
                )

                if simulate_crash_after is not None and (index + 1) >= simulate_crash_after:
                    crashed = True
                    span.add_event("simulated_crash", {"after_records": index + 1})
                    raise SimulatedCrash(
                        f"Simulated crash after {index + 1} records "
                        f"(checkpoint at index {index}). Re-run the same command to resume."
                    )

        span.set_attribute("bronze.records_written", checkpoint["records_written"])

    if paused:
        checkpoint["status"] = "paused"
        write_checkpoint(run_dir, checkpoint)
        telemetry.count("ingest.runs_paused_total")
        raise PausedByOperator(f"Paused at index {checkpoint['last_committed_index'] + 1}.")

    # ---- finalize ---------------------------------------------------------
    written, unique = count_records(part_path)
    checkpoint["status"] = "complete"
    checkpoint["completed_at"] = utc_now()
    write_checkpoint(run_dir, checkpoint)

    duplicates = written - unique
    if duplicates:
        # Expected after a crash in the commit window. Surfaced, never hidden.
        telemetry.warn(
            "bronze.replay_duplicates",
            f"{duplicates} duplicate record(s) from crash replay; normalize.py dedupes on record_hash.",
            run_id=run_id,
            duplicates=duplicates,
        )
        telemetry.count("ingest.replay_duplicates_total", duplicates)

    manifest = {
        "contract_version": CONTRACT_VERSION,
        "run_id": run_id,
        "status": "complete",
        "source_file": source_path.name,
        "source_sha256": source_sha,
        "source_api_version": api_version,
        "source_metadata": metadata,
        "ingested_at": ingested_at,
        "completed_at": checkpoint["completed_at"],
        "records_in_source": len(clauses),
        "records_written": written,
        "unique_record_hashes": unique,
        "duplicates_from_replay": duplicates,
        "schema_fingerprint": schema_fingerprint,
        "delivery_guarantee": "at-least-once; dedupe key is _bronze.record_hash",
        "telemetry": {"trace_ids": sorted(collect_trace_ids(telemetry))},
    }
    (run_dir / "_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    telemetry.info(
        "run.complete",
        f"{run_id}: {unique} unique record(s) landed in {display_path(run_dir)}",
        run_id=run_id,
        records=unique,
    )
    telemetry.count("ingest.runs_completed_total")

    # ---- drift, AFTER the data is safely landed ---------------------------
    with telemetry.span(
        "ingest.detect_drift", **{"run.id": run_id, "source.api_version": api_version}
    ):
        report = drift.detect(
            source_name=source_name,
            api_version=api_version,
            records=clauses,
            registry_path=REGISTRY_PATH,
            run_id=run_id,
            ingested_at=ingested_at,
        )
        DRIFT_DIR.mkdir(parents=True, exist_ok=True)
        (DRIFT_DIR / "drift_report.json").write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8"
        )
        (DRIFT_DIR / "drift_report.md").write_text(drift.render_markdown(report), encoding="utf-8")
        # Per-run copy, so history is not overwritten by the next batch.
        (DRIFT_DIR / f"drift_report__{run_id}.json").write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8"
        )

        if report["drift_detected"]:
            telemetry.count("drift.events_total", len(report["events"]))
            for event in report["events"]:
                severity = {
                    "BREAKING": "ERROR",
                    "NEEDS_HUMAN_CONFIRMATION": "WARN",
                    "WARN": "WARN",
                }.get(event["severity"], "INFO")
                telemetry.log(
                    severity,
                    f"drift.{event['kind'].lower()}",
                    f"{event['path']}: {event['detail']}",
                    field_path=event["path"],
                    drift_severity=event["severity"],
                    confidence=event.get("confidence"),
                )
        else:
            telemetry.info("drift.none", f"No drift against {report['compared_against']}.")

    return {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "status": "complete",
        "records": unique,
        "drift_report": report,
    }


def count_records(part_path: Path) -> Tuple[int, int]:
    """(lines written, unique record hashes) -- makes replay visible in the manifest."""
    if not part_path.exists():
        return 0, 0
    hashes = set()
    written = 0
    with part_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            written += 1
            hashes.add(json.loads(line)["_bronze"]["record_hash"])
    return written, len(hashes)


def collect_trace_ids(telemetry: Telemetry) -> List[str]:
    ids = telemetry.current_ids()
    return [ids["trace_id"]] if ids["trace_id"] else []


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Land contract clauses into the Bronze layer.")
    parser.add_argument("--source", required=True, help="Path to a source clause JSON file")
    parser.add_argument(
        "--fail-on-drift",
        action="store_true",
        help="Exit 2 if drift needs human attention. Data is landed either way; "
        "this only lets an orchestrator gate the DOWNSTREAM stage.",
    )
    parser.add_argument(
        "--simulate-crash-after",
        type=int,
        metavar="N",
        help="Crash after committing N records, to demonstrate resume.",
    )
    parser.add_argument(
        "--crash-window",
        action="store_true",
        help="With --simulate-crash-after, crash INSIDE the commit window (record written "
        "but checkpoint not yet advanced) to demonstrate at-least-once replay and dedupe.",
    )
    parser.add_argument(
        "--restart",
        action="store_true",
        help="Archive the previous attempt for this run and start over (never deletes).",
    )
    args = parser.parse_args(argv)

    source_path = Path(args.source)
    if not source_path.is_absolute():
        source_path = (Path.cwd() / source_path).resolve()

    # Telemetry is constructed before anything can fail, so that even a bad
    # invocation produces a structured record. Nothing in this pipeline
    # reports an error through a channel the log files cannot see.
    telemetry = Telemetry("contract-intelligence.ingest", LOG_DIR)
    exit_code = 0
    try:
        with telemetry.span("ingest.run", **{"source.file": source_path.name}) as root:
            if not source_path.exists():
                telemetry.error(
                    "run.source_not_found",
                    f"Source file does not exist: {source_path}",
                    source=str(source_path),
                )
                return 1
            root.set_attribute("telemetry.backend", telemetry.backend)
            telemetry.info(
                "run.start",
                f"Ingesting {source_path.name} (telemetry backend: {telemetry.backend})",
                source=source_path.name,
            )
            result = ingest(
                source_path,
                telemetry=telemetry,
                simulate_crash_after=args.simulate_crash_after,
                crash_in_commit_window=args.crash_window,
                restart=args.restart,
            )
            report = result.get("drift_report")
            if args.fail_on_drift and report and report["requires_human_action"]:
                telemetry.warn(
                    "run.gated_on_drift",
                    "Data landed in Bronze. Exiting 2 so the downstream stage stays gated "
                    "until a human reviews drift_report.md.",
                )
                exit_code = 2
    except SimulatedCrash as exc:
        telemetry.error("run.simulated_crash", str(exc))
        telemetry.count("ingest.runs_crashed_total")
        exit_code = 1
    except PausedByOperator as exc:
        telemetry.warn("run.paused_exit", str(exc))
        exit_code = 1
    except Exception as exc:  # noqa: BLE001 - top level, must record before dying
        telemetry.error("run.failed", f"{type(exc).__name__}: {exc}")
        telemetry.count("ingest.runs_failed_total")
        exit_code = 1
    finally:
        telemetry.shutdown()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
