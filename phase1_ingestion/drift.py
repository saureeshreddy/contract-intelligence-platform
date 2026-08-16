"""
phase1_ingestion/drift.py
=========================
Schema drift detection  (Phase 1 requirement P1-R2).

THE PROBLEM
-----------
Batch 1 arrives as api_version 2.1.  Batch 2 arrives as 2.3 and nobody told
us the shape changed.  Three things actually changed:

    clause_type   was RENAMED to  category
    modified_by   was REMOVED
    review_history was ADDED      (a nested object with an array inside)

...plus a fourth that a key-set diff would miss entirely: the `status` field
gained a new value, "under_review", that did not exist in 2.1.

THE RULE THAT DRIVES THE DESIGN
-------------------------------
Policy says drift "must not silently drop or ignore changed data".  The
only way to *guarantee* that is to have no code path that can drop anything.
So this module is pure observation: it reads records, describes their shape,
compares that to every shape we have seen before, and writes a report.

    It never filters a record.
    It never rewrites a record.
    It never decides what a renamed field means.

That last point matters.  A rename is detected by a *heuristic* (value-set
overlap), and a heuristic that silently rewrote data would corrupt every
downstream classification when it guessed wrong.  So the machine proposes and
a human confirms, by adding one line to config/schema_aliases.json.  Until
that happens, normalize.py leaves the field null and says so out loud.

WHERE STATE LIVES
-----------------
The schema registry is machine-written, so it lives under output/.
The alias file is human-owned, so it lives under config/.
Nothing in this pipeline writes to config/ -- that separation is what makes
the Phase 2 human-in-the-loop guarantee enforceable rather than aspirational.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

# A field with more distinct values than this is treated as free text: we do
# not track its value set (no point, and it would bloat the registry).
ENUM_CARDINALITY_LIMIT = 20

# Long strings are prose, not labels. Skipping them keeps clause_text out of
# the registry (which it would otherwise bloat by an order of magnitude) and
# out of rename matching, where it could never be a meaningful signal.
VALUE_TRACKING_MAX_LENGTH = 120

# Distinct values must repeat at least this often, on average, before we treat
# a field as categorical and report new values as drift.
#
# Without this, every batch "drifts" simply by containing new clauses: new
# clause_ids, new client names, new dates. Those are data, not schema. A real
# categorical field like `status` has 2 distinct values across 12 records; an
# identifier like `clause_id` has 12 across 12. The ratio separates them
# cleanly. Tuned against the supplied batches; revisit if a legitimate
# low-repetition enum ever appears.
ENUM_MIN_REPETITION = 4

# Value-set overlap (Jaccard) above which a removed/added pair is proposed as
# a rename.  0.5 is deliberately conservative: we would rather miss a rename
# and leave a visible null than assert a wrong one and corrupt Silver.
RENAME_CONFIDENCE_THRESHOLD = 0.5

SEVERITY_ORDER = {
    "INFO": 0,
    "ADDITIVE": 1,
    "WARN": 2,
    "NEEDS_HUMAN_CONFIRMATION": 3,
    "BREAKING": 4,
}


# --------------------------------------------------------------------------
# Field inventory
# --------------------------------------------------------------------------
@dataclass
class FieldStat:
    """What we know about one leaf path across a whole batch."""

    path: str
    types: List[str] = field(default_factory=list)
    present_count: int = 0
    null_count: int = 0
    values: Optional[List[str]] = None  # only for low-cardinality strings

    def as_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        out["types"] = sorted(self.types)
        if self.values is not None:
            out["values"] = sorted(self.values)
        return out


def type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def flatten(obj: Any, prefix: str = "") -> Iterator[Tuple[str, Any]]:
    """Walk a record into (path, leaf_value) pairs.

    Arrays collapse to a single `path[]` so that 2 reviews and 200 reviews
    produce the same schema.  We are describing *shape*, not contents.
    """
    if isinstance(obj, dict):
        for key, value in obj.items():
            child = f"{prefix}.{key}" if prefix else key
            if isinstance(value, (dict, list)):
                # Record the container itself, then descend.
                yield child, value
                yield from flatten(value, child)
            else:
                yield child, value
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)):
                yield from flatten(item, f"{prefix}[]")
            else:
                yield f"{prefix}[]", item


def build_inventory(records: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Describe the shape of a batch: path -> FieldStat."""
    stats: Dict[str, FieldStat] = {}
    raw_values: Dict[str, set] = {}

    for record in records:
        for path, value in flatten(record):
            stat = stats.setdefault(path, FieldStat(path=path))
            stat.present_count += 1
            tname = type_name(value)
            if tname == "null":
                stat.null_count += 1
            elif tname not in stat.types:
                stat.types.append(tname)
            if isinstance(value, str) and len(value) <= VALUE_TRACKING_MAX_LENGTH:
                raw_values.setdefault(path, set()).add(value)

    for path, values in raw_values.items():
        if len(values) <= ENUM_CARDINALITY_LIMIT:
            stats[path].values = sorted(values)

    return {path: stat.as_dict() for path, stat in sorted(stats.items())}


def is_categorical(stat: Dict[str, Any]) -> bool:
    """Does this field hold labels (report new values) or data (do not)?

    `status` -> 2 distinct across 12 records -> categorical.
    `clause_id` -> 12 distinct across 12 records -> an identifier.
    """
    values = stat.get("values")
    if not values:
        return False
    return len(values) * ENUM_MIN_REPETITION <= stat["present_count"]


def fingerprint(inventory: Dict[str, Dict[str, Any]]) -> str:
    """Stable hash of a batch's shape. Stamped onto every Bronze record."""
    material = "|".join(
        f"{path}:{','.join(stat['types'])}" for path, stat in sorted(inventory.items())
    )
    return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Drift events
# --------------------------------------------------------------------------
@dataclass
class DriftEvent:
    kind: str
    severity: str
    path: str
    detail: str
    action_required: Optional[str] = None
    confidence: Optional[float] = None
    evidence: Optional[Dict[str, Any]] = None

    def as_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


def jaccard(left: List[str], right: List[str]) -> float:
    a, b = set(left), set(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def diff_inventories(
    old: Dict[str, Dict[str, Any]], new: Dict[str, Dict[str, Any]]
) -> List[DriftEvent]:
    """Compare two batch shapes and describe every difference."""
    events: List[DriftEvent] = []
    old_paths, new_paths = set(old), set(new)

    removed = sorted(old_paths - new_paths)
    added = sorted(new_paths - old_paths)

    # --- suspected renames -------------------------------------------------
    # A rename looks like one removal plus one addition at the same depth,
    # carrying the same kind of values.  We match on value-set overlap
    # because field names tell us nothing (`clause_type` vs `category` share
    # no substring) but the values are 75% identical.
    matched: Dict[str, str] = {}
    for r in removed:
        r_stat = old[r]
        if not r_stat.get("values"):
            continue
        best, best_score = None, 0.0
        for a in added:
            if a in matched.values():
                continue
            a_stat = new[a]
            if r.count(".") != a.count(".") or not a_stat.get("values"):
                continue
            if sorted(r_stat["types"]) != sorted(a_stat["types"]):
                continue
            score = jaccard(r_stat["values"], a_stat["values"])
            if score > best_score:
                best, best_score = a, score
        if best and best_score >= RENAME_CONFIDENCE_THRESHOLD:
            matched[r] = best
            events.append(
                DriftEvent(
                    kind="SUSPECTED_RENAME",
                    severity="NEEDS_HUMAN_CONFIRMATION",
                    path=f"{r} -> {best}",
                    detail=(
                        f"Field '{r}' disappeared and '{best}' appeared carrying "
                        f"{int(best_score * 100)}% of the same values. This looks like a rename."
                    ),
                    confidence=round(best_score, 3),
                    action_required=(
                        f"A human must confirm by adding \"{best}\": \"<target_field>\" to "
                        f"phase1_ingestion/config/schema_aliases.json. Until then normalize.py "
                        f"emits null for the target field and lists '{best}' in _unmapped_fields."
                    ),
                    evidence={
                        "removed_values": old[r].get("values"),
                        "added_values": new[best].get("values"),
                    },
                )
            )

    # --- removals ----------------------------------------------------------
    for r in removed:
        if r in matched:
            # Explained by the rename above; still reported, but not BREAKING.
            events.append(
                DriftEvent(
                    kind="FIELD_REMOVED",
                    severity="INFO",
                    path=r,
                    detail=f"Absent from the new batch, but explained by suspected rename to '{matched[r]}'.",
                )
            )
            continue
        events.append(
            DriftEvent(
                kind="FIELD_REMOVED",
                severity="BREAKING",
                path=r,
                detail=(
                    f"Present in {old[r]['present_count']} records of the previous schema, "
                    f"absent from every record of this one. Downstream consumers that require "
                    f"this field will break; the value is not recoverable."
                ),
                action_required=(
                    "Confirm the field is genuinely retired upstream, then mark it nullable "
                    "in data_contract.md and notify consumers."
                ),
            )
        )

    # --- additions ---------------------------------------------------------
    # A new nested object arrives as one change, not eight. `review_history`
    # plus its seven descendants is a single decision for a human to make, so
    # we report the subtree root and list the descendants as evidence.
    # Without this the "action required" list is eight identical lines and
    # stops being read.
    def is_descendant(child: str, parent: str) -> bool:
        return child.startswith(parent + ".") or child.startswith(parent + "[]")

    for a in added:
        if a in matched.values():
            continue  # already reported as the target of a rename
        if any(is_descendant(a, other) for other in added if other != a):
            continue  # covered by its subtree root
        nested = sorted(x for x in added if is_descendant(x, a))
        nested_note = f" It carries {len(nested)} nested field(s)." if nested else ""
        events.append(
            DriftEvent(
                kind="FIELD_ADDED",
                severity="ADDITIVE",
                path=a,
                detail=(
                    f"New field of type {'/'.join(new[a]['types']) or 'null'}, present in "
                    f"{new[a]['present_count']} records.{nested_note} Landed in Bronze; "
                    f"not yet exposed in Silver."
                ),
                action_required="Decide whether Silver should surface this field, then extend the data contract.",
                evidence={"nested_paths": nested} if nested else None,
            )
        )

    # --- shared paths: type, nullability, enum values ----------------------
    for path in sorted(old_paths & new_paths):
        o, n = old[path], new[path]

        if sorted(o["types"]) != sorted(n["types"]):
            events.append(
                DriftEvent(
                    kind="TYPE_CHANGED",
                    severity="BREAKING",
                    path=path,
                    detail=f"Type changed from {'/'.join(o['types'])} to {'/'.join(n['types'])}.",
                    action_required="Downstream parsing will break. Confirm intent before regenerating Silver.",
                )
            )

        if o["null_count"] == 0 and n["null_count"] > 0:
            events.append(
                DriftEvent(
                    kind="NULLABILITY_CHANGED",
                    severity="WARN",
                    path=path,
                    detail=(
                        f"Previously never null; now null in {n['null_count']} of "
                        f"{n['present_count']} records."
                    ),
                )
            )

        # Only categorical fields. The previous batch defines the known
        # vocabulary, so enum-likeness is judged on the baseline.
        if is_categorical(o) and n.get("values"):
            new_values = sorted(set(n["values"]) - set(o["values"]))
            if new_values:
                events.append(
                    DriftEvent(
                        kind="ENUM_VALUE_ADDED",
                        severity="INFO",
                        path=path,
                        detail=f"New value(s) not seen in the previous schema: {', '.join(new_values)}.",
                        evidence={"new_values": new_values, "known_values": o["values"]},
                    )
                )

    events.sort(key=lambda e: (-SEVERITY_ORDER.get(e.severity, 0), e.path))
    return events


# --------------------------------------------------------------------------
# Registry  (machine-written state -> lives under output/)
# --------------------------------------------------------------------------
def load_registry(path: Path) -> Dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"sources": {}}


def save_registry(path: Path, registry: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(registry, indent=2) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# Entry point used by ingest.py
# --------------------------------------------------------------------------
def detect(
    *,
    source_name: str,
    api_version: str,
    records: List[Dict[str, Any]],
    registry_path: Path,
    run_id: str,
    ingested_at: str,
) -> Dict[str, Any]:
    """Compare this batch against the last known shape for the same source.

    Returns a drift report dict.  Does not write anything except the registry;
    the caller owns report files so that all output paths live in one place.
    """
    inventory = build_inventory(records)
    fp = fingerprint(inventory)

    registry = load_registry(registry_path)
    source = registry["sources"].setdefault(source_name, {"versions": {}, "latest_version": None})
    previous_version = source.get("latest_version")

    events: List[DriftEvent] = []
    if previous_version is None:
        baseline = "none (first batch from this source)"
    elif previous_version == api_version and source["versions"][api_version]["fingerprint"] == fp:
        baseline = f"{previous_version} (identical shape)"
    else:
        baseline = previous_version
        events = diff_inventories(source["versions"][previous_version]["inventory"], inventory)
        if previous_version != api_version:
            events.insert(
                0,
                DriftEvent(
                    kind="API_VERSION_CHANGED",
                    severity="INFO",
                    path="metadata.api_version",
                    detail=f"Source declared api_version {previous_version} -> {api_version}.",
                ),
            )

    # Record this shape.  Only reached once the batch has landed successfully,
    # so a crashed run never poisons the baseline for the next one.
    source["versions"][api_version] = {
        "fingerprint": fp,
        "first_seen": source["versions"].get(api_version, {}).get("first_seen", ingested_at),
        "last_seen": ingested_at,
        "last_run_id": run_id,
        "record_count": len(records),
        "inventory": inventory,
    }
    source["latest_version"] = api_version
    save_registry(registry_path, registry)

    severities = [e.severity for e in events]
    return {
        "generated_at": ingested_at,
        "run_id": run_id,
        "source": source_name,
        "api_version": api_version,
        "compared_against": baseline,
        "schema_fingerprint": fp,
        "record_count": len(records),
        "drift_detected": bool(events),
        "requires_human_action": any(
            s in ("BREAKING", "NEEDS_HUMAN_CONFIRMATION") for s in severities
        ),
        "summary": {
            severity: severities.count(severity)
            for severity in SEVERITY_ORDER
            if severities.count(severity)
        },
        "events": [e.as_dict() for e in events],
        "guarantee": (
            "All records in this batch were written to Bronze unchanged, regardless of the "
            "drift reported above. Drift detection never filters, rewrites, or rejects data."
        ),
    }


def render_markdown(report: Dict[str, Any]) -> str:
    """Human-readable twin of drift_report.json.

    Worth the twenty lines: an on-call engineer at 2am should
    not have to pretty-print JSON to find out what changed.
    """
    lines = [
        "# Schema Drift Report",
        "",
        f"- **Run:** `{report['run_id']}`",
        f"- **Source:** {report['source']} (api_version **{report['api_version']}**)",
        f"- **Compared against:** {report['compared_against']}",
        f"- **Records in batch:** {report['record_count']}",
        f"- **Schema fingerprint:** `{report['schema_fingerprint']}`",
        f"- **Generated:** {report['generated_at']}",
        "",
    ]

    if not report["drift_detected"]:
        lines += ["No drift detected against the previous known schema.", ""]
    else:
        counts = ", ".join(f"{n} {sev}" for sev, n in report["summary"].items())
        lines += [f"**{len(report['events'])} change(s) detected:** {counts}", ""]
        lines += ["| Severity | Change | Field | Detail |", "|---|---|---|---|"]
        for e in report["events"]:
            detail = e["detail"].replace("|", "/")
            lines.append(f"| `{e['severity']}` | {e['kind']} | `{e['path']}` | {detail} |")
        lines.append("")

        actions = [e for e in report["events"] if e.get("action_required")]
        if actions:
            lines += ["## Action required", ""]
            for e in actions:
                confidence = f" _(confidence {e['confidence']})_" if e.get("confidence") else ""
                lines.append(f"- **{e['kind']}** on `{e['path']}`{confidence} — {e['action_required']}")
            lines.append("")

    lines += ["## Guarantee", "", report["guarantee"], ""]
    return "\n".join(lines)
