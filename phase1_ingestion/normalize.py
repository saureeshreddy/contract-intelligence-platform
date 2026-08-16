#!/usr/bin/env python3
"""
phase1_ingestion/normalize.py
=============================
Bronze -> Silver.  The only stage in Phase 1 that is allowed to change a
record's shape.

WHY THIS IS A SEPARATE STAGE
----------------------------
The platform requires Bronze to preserve raw data "as-is -- no transformation at
this stage".  The tempting shortcut is for ingest.py to "just fix" the
renamed field on the way in.  That single line would destroy the evidence of
what actually arrived, which is the whole reason a Bronze layer exists.
Giving normalization its own obvious home removes the temptation, and Phase 2
needs a unified view regardless.  See docs/architecture_note.md, AD-1.

WHAT IT DOES
------------
* Reads every completed Bronze run.
* Drops replay duplicates using `_bronze.record_hash` -- this is where the
  at-least-once guarantee from ingest.py is settled.
* Applies HUMAN-CONFIRMED field aliases from config/schema_aliases.json so
  v2.1's `clause_type` and v2.3's `category` both become `clause_category`.
* Fills version-conditional fields (`modified_by`, `review_history`) with an
  explicit null when the source version did not carry them.
* Carries lineage on every row, so any Silver value can be traced back to the
  exact Bronze record and run that produced it.

WHAT IT REFUSES TO DO
---------------------
It will not guess an alias.  If a source field has no confirmed mapping the
target field stays null and the source field name is listed in the record's
`_unmapped_fields`.  A visible gap is recoverable; a confident wrong value is
not.

OUTPUT
------
  output/silver/clauses.jsonl            one record per line, lineage included
  output/silver/clauses.json             same records with a metadata header
  output/silver/normalization_report.json  field coverage + what was dropped

USAGE
  python phase1_ingestion/normalize.py
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.observability import Telemetry, default_log_dir, utc_now  # noqa: E402

PHASE_DIR = ROOT / "phase1_ingestion"
OUTPUT_DIR = PHASE_DIR / "output"
BRONZE_ROOT = OUTPUT_DIR / "bronze"
SILVER_DIR = OUTPUT_DIR / "silver"
LOG_DIR = default_log_dir()  # repo-root output/logs/ -- telemetry is cross-phase
ALIAS_PATH = PHASE_DIR / "config" / "schema_aliases.json"

CONTRACT_VERSION = "1.0.0"

# Fields that carried the same name in every API version seen so far.
STABLE_FIELDS = [
    "clause_id",
    "contract_id",
    "client_name",
    "project_name",
    "clause_text",
    "section_ref",
    "effective_date",
    "expiration_date",
    "status",
    "last_modified",
]

# Present in some API versions only. Explicit null elsewhere -- and null here
# means "this API version did not send it", NOT "the value is empty".
VERSION_CONDITIONAL_FIELDS = ["modified_by", "review_history"]


def load_aliases(path: Path) -> Dict[str, str]:
    """Read the human-confirmed alias map. Missing file = no aliases confirmed."""
    if not path.exists():
        return {}
    config = json.loads(path.read_text(encoding="utf-8"))
    return {
        source_field: entry["target"]
        for source_field, entry in config.get("aliases", {}).items()
        if entry.get("confirmed_by")  # unapproved entries are inert by design
    }


def iter_bronze_records(bronze_root: Path) -> List[Dict[str, Any]]:
    """Read every completed Bronze run, oldest first.

    Runs still `in_progress` or `paused` are skipped: Silver is only ever
    built from batches that finished landing, so a half-ingested file can
    never produce a partial Silver that looks complete.
    """
    records: List[Dict[str, Any]] = []
    if not bronze_root.exists():
        return records
    for part in sorted(bronze_root.glob("ingest_date=*/run_id=*/part-0000.jsonl")):
        checkpoint_path = part.parent / "_checkpoint.json"
        if checkpoint_path.exists():
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            if checkpoint.get("status") != "complete":
                continue
        with part.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
    return records


def to_silver(envelope: Dict[str, Any], aliases: Dict[str, str]) -> Dict[str, Any]:
    """Map one Bronze envelope onto the Silver contract shape."""
    payload = envelope["payload"]
    bronze = envelope["_bronze"]

    silver: Dict[str, Any] = {field: payload.get(field) for field in STABLE_FIELDS}

    # Resolve aliased fields. A field already carrying the target name wins;
    # otherwise we look for a confirmed alias.
    unmapped: List[str] = []
    aliased_targets: Dict[str, Any] = {}
    known_source_fields = set(STABLE_FIELDS) | set(VERSION_CONDITIONAL_FIELDS) | set(aliases)

    for key, value in payload.items():
        if key in aliases:
            aliased_targets[aliases[key]] = value
        elif key not in known_source_fields:
            # Arrived in Bronze, understood by nobody yet. Never dropped
            # silently -- it is named on the record and counted in the report.
            unmapped.append(key)

    silver["clause_category"] = payload.get("clause_category", aliased_targets.get("clause_category"))
    for field in VERSION_CONDITIONAL_FIELDS:
        silver[field] = payload.get(field)

    # Reorder to the published contract order for readability/diffability.
    ordered = {
        "clause_id": silver["clause_id"],
        "contract_id": silver["contract_id"],
        "client_name": silver["client_name"],
        "project_name": silver["project_name"],
        "clause_category": silver["clause_category"],
        "clause_text": silver["clause_text"],
        "section_ref": silver["section_ref"],
        "effective_date": silver["effective_date"],
        "expiration_date": silver["expiration_date"],
        "status": silver["status"],
        "last_modified": silver["last_modified"],
        "modified_by": silver["modified_by"],
        "review_history": silver["review_history"],
    }
    ordered["_unmapped_fields"] = sorted(unmapped)
    ordered["_lineage"] = {
        "run_id": bronze["run_id"],
        "source_file": bronze["source_file"],
        "source_api_version": bronze["source_api_version"],
        "record_index": bronze["record_index"],
        "record_hash": bronze["record_hash"],
        "ingested_at": bronze["ingested_at"],
    }
    return ordered


def normalize(telemetry: Telemetry) -> Dict[str, Any]:
    aliases = load_aliases(ALIAS_PATH)
    telemetry.info(
        "normalize.aliases_loaded",
        f"{len(aliases)} human-confirmed alias(es): "
        + (", ".join(f"{k}->{v}" for k, v in sorted(aliases.items())) or "none"),
        alias_count=len(aliases),
    )

    with telemetry.span("normalize.read_bronze") as span:
        envelopes = iter_bronze_records(BRONZE_ROOT)
        span.set_attribute("bronze.records_read", len(envelopes))

    if not envelopes:
        telemetry.error("normalize.no_bronze", "No completed Bronze runs found. Run ingest.py first.")
        raise SystemExit(1)

    # ---- dedupe: this is where at-least-once is settled -------------------
    seen: set = set()
    unique: List[Dict[str, Any]] = []
    duplicates = 0
    for envelope in envelopes:
        rh = envelope["_bronze"]["record_hash"]
        if rh in seen:
            duplicates += 1
            telemetry.log(
                "DEBUG",
                "normalize.duplicate_dropped",
                f"replay duplicate {rh[:19]}...",
                record_hash=rh,
            )
            continue
        seen.add(rh)
        unique.append(envelope)

    if duplicates:
        telemetry.warn(
            "normalize.duplicates_dropped",
            f"Dropped {duplicates} replay duplicate(s) from Bronze (expected after a crash+resume).",
            duplicates=duplicates,
        )
    telemetry.count("normalize.duplicates_dropped_total", duplicates)

    # ---- map --------------------------------------------------------------
    with telemetry.span("normalize.map_to_silver") as span:
        silver_records = [to_silver(envelope, aliases) for envelope in unique]
        silver_records.sort(key=lambda r: (r["clause_id"] or ""))
        span.set_attribute("silver.records", len(silver_records))

    # ---- integrity + coverage --------------------------------------------
    ids = Counter(r["clause_id"] for r in silver_records)
    collisions = {cid: n for cid, n in ids.items() if n > 1}
    if collisions:
        # Different content under the same clause_id: a genuine upstream
        # update. Not present in the supplied data, but if it ever happens we
        # say so loudly rather than letting one row win at random.
        telemetry.error(
            "normalize.clause_id_collision",
            f"{len(collisions)} clause_id(s) appear more than once: {sorted(collisions)}",
            clause_ids=sorted(collisions),
        )
        telemetry.count("normalize.clause_id_collisions_total", len(collisions))

    null_counts = {
        field: sum(1 for r in silver_records if r[field] is None)
        for field in list(STABLE_FIELDS) + ["clause_category"] + VERSION_CONDITIONAL_FIELDS
    }
    unmapped_records = [r["clause_id"] for r in silver_records if r["_unmapped_fields"]]
    if unmapped_records:
        telemetry.warn(
            "normalize.unmapped_fields",
            f"{len(unmapped_records)} record(s) carry source fields with no confirmed alias. "
            f"Their target fields are null by design; see _unmapped_fields.",
            clause_ids=unmapped_records[:10],
        )
    telemetry.count("normalize.unmapped_records_total", len(unmapped_records))

    missing_category = [r["clause_id"] for r in silver_records if r["clause_category"] is None]
    if missing_category:
        telemetry.error(
            "normalize.missing_clause_category",
            f"{len(missing_category)} record(s) have no clause_category. Confirm the rename "
            f"in config/schema_aliases.json.",
            clause_ids=missing_category[:10],
        )
    telemetry.count("normalize.records_total", len(silver_records))

    # ---- write ------------------------------------------------------------
    SILVER_DIR.mkdir(parents=True, exist_ok=True)
    with (SILVER_DIR / "clauses.jsonl").open("w", encoding="utf-8") as fh:
        for record in silver_records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    api_versions = sorted({r["_lineage"]["source_api_version"] for r in silver_records})
    run_ids = sorted({r["_lineage"]["run_id"] for r in silver_records})
    document = {
        "ingestion_metadata": {
            "contract_version": CONTRACT_VERSION,
            "normalized_at": utc_now(),
            "source": "contract_management_api",
            "source_api_versions": api_versions,
            "bronze_run_ids": run_ids,
            "total_records": len(silver_records),
            "duplicates_dropped": duplicates,
            "schema_notes": (
                "Normalized from Bronze. 'clause_type' (v2.1) and 'category' (v2.3) unified as "
                "'clause_category' via human-confirmed aliases. 'modified_by' is present in v2.1 "
                "only; 'review_history' in v2.3 only. null in those fields means the source API "
                "version did not carry the field, not that the value was empty."
            ),
        },
        "clauses": silver_records,
    }
    (SILVER_DIR / "clauses.json").write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    report = {
        "generated_at": utc_now(),
        "bronze_records_read": len(envelopes),
        "duplicates_dropped": duplicates,
        "silver_records": len(silver_records),
        "source_api_versions": api_versions,
        "bronze_run_ids": run_ids,
        "confirmed_aliases": aliases,
        "null_counts_by_field": null_counts,
        "records_with_unmapped_fields": unmapped_records,
        "clause_id_collisions": sorted(collisions),
        "contract": "phase1_ingestion/data_contract.md",
    }
    (SILVER_DIR / "normalization_report.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )

    telemetry.info(
        "normalize.complete",
        f"{len(silver_records)} Silver record(s) written "
        f"(from {len(envelopes)} Bronze rows, {duplicates} duplicate(s) dropped).",
        records=len(silver_records),
    )
    return report


def main(argv: Optional[List[str]] = None) -> int:
    argparse.ArgumentParser(description="Normalize Bronze into the Silver contract shape.").parse_args(
        argv
    )
    telemetry = Telemetry("contract-intelligence.normalize", LOG_DIR)
    try:
        with telemetry.span("normalize.run") as root:
            root.set_attribute("telemetry.backend", telemetry.backend)
            normalize(telemetry)
        return 0
    except SystemExit as exc:
        return int(exc.code or 1)
    except Exception as exc:  # noqa: BLE001
        telemetry.error("normalize.failed", f"{type(exc).__name__}: {exc}")
        return 1
    finally:
        telemetry.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
