#!/usr/bin/env python3
"""
phase1_ingestion/supersede.py
=============================
Propose clause supersessions for human review.

WHY THIS EXISTS
---------------
This is not strictly required.  It is in the data.

    CLZ-2025-0001  CTR-4401  "Section 8.1"
        "...indemnify... regardless of whether such claim is caused in part
         by the Owner's negligence."

    CLZ-2025-0013  CTR-4401  "Section 8.1 (Amended)"
        "...shall be limited to claims arising from the Consultant's
         negligent acts..."
        review_history: "Original clause contained broad indemnification --
                         revised to negligence standard"

The second replaces the first.  They have different clause_ids, so an
append-only Bronze layer keeps both, and both look `active` in Silver.  Left
alone, the Phase 2 risk agent will flag a clause that the legal team already
fixed a month ago -- and an engineer reading the output has no way to know.

WHAT THIS DOES ABOUT IT
-----------------------
Groups clauses by (contract_id, section number with any "(Amended)" marker
stripped) and reports:

  SUPERSESSION_CANDIDATE   two or more clauses share a contract + section
  AMENDS_UNKNOWN_CLAUSE    a clause is marked "(Amended)" but the clause it
                           amends was never delivered to us

WHAT IT DOES NOT DO
-------------------
It does not edit Silver.  It does not set a `superseded` flag.  It writes
proposals with status PENDING_HUMAN_REVIEW and stops.  Section numbering is a
convention, not a guarantee -- a firm could legitimately reuse "Section 8.1"
for something unrelated -- so this is evidence for a human, not a decision.

Third use of the same boundary in Phase 1: the machine proposes, a human
disposes.  (The other two: schema renames, and the alias config it cannot
write to.)

OUTPUT
  output/proposals/supersession_candidates.json

USAGE
  python phase1_ingestion/supersede.py
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.observability import Telemetry, default_log_dir, utc_now  # noqa: E402

PHASE_DIR = ROOT / "phase1_ingestion"
OUTPUT_DIR = PHASE_DIR / "output"
SILVER_PATH = OUTPUT_DIR / "silver" / "clauses.jsonl"
PROPOSALS_DIR = OUTPUT_DIR / "proposals"
LOG_DIR = default_log_dir()  # repo-root output/logs/ -- telemetry is cross-phase

AMENDMENT_MARKER = re.compile(r"\(\s*amend(?:ed|ment)\s*\)", re.IGNORECASE)
SECTION_NUMBER = re.compile(r"(\d+(?:\.\d+)*)")


def section_key(section_ref: Optional[str]) -> Optional[str]:
    """'Section 8.1 (Amended)' -> '8.1'.  Returns None if no number is present."""
    if not section_ref:
        return None
    stripped = AMENDMENT_MARKER.sub("", section_ref)
    match = SECTION_NUMBER.search(stripped)
    return match.group(1) if match else None


def is_amendment(section_ref: Optional[str]) -> bool:
    return bool(section_ref and AMENDMENT_MARKER.search(section_ref))


def load_silver(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        raise SystemExit(f"Silver not found at {path}. Run normalize.py first.")
    with path.open("r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def analyse(clauses: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    groups: Dict[tuple, List[Dict[str, Any]]] = defaultdict(list)
    for clause in clauses:
        key = section_key(clause.get("section_ref"))
        if key is None:
            continue
        groups[(clause.get("contract_id"), key)].append(clause)

    proposals: List[Dict[str, Any]] = []

    for (contract_id, section), members in sorted(groups.items()):
        if len(members) > 1:
            # Newest wins by last_modified; ties fall back to clause_id order.
            ordered = sorted(
                members, key=lambda c: (c.get("last_modified") or "", c.get("clause_id") or "")
            )
            newest = ordered[-1]
            for older in ordered[:-1]:
                proposals.append(
                    {
                        "proposal_id": f"SUP-{older['clause_id']}",
                        "kind": "SUPERSESSION_CANDIDATE",
                        "status": "PENDING_HUMAN_REVIEW",
                        "contract_id": contract_id,
                        "section": section,
                        "superseded_clause_id": older["clause_id"],
                        "superseding_clause_id": newest["clause_id"],
                        "evidence": {
                            "same_contract_and_section": True,
                            "superseding_section_ref": newest.get("section_ref"),
                            "superseded_section_ref": older.get("section_ref"),
                            "superseding_marked_amended": is_amendment(newest.get("section_ref")),
                            "superseded_last_modified": older.get("last_modified"),
                            "superseding_last_modified": newest.get("last_modified"),
                            "superseding_review_notes": [
                                review.get("notes")
                                for review in (newest.get("review_history") or {}).get("reviews", [])
                            ],
                        },
                        "impact_if_confirmed": (
                            f"{older['clause_id']} is no longer in force. Downstream risk review "
                            f"should exclude it, or flag it as historical."
                        ),
                        "action_required": (
                            "A human must confirm. If confirmed, record the supersession in the "
                            "source system; do NOT edit Silver by hand -- Silver is regenerated "
                            "from Bronze and any manual edit is lost on the next run."
                        ),
                    }
                )
        elif is_amendment(members[0].get("section_ref")):
            clause = members[0]
            proposals.append(
                {
                    "proposal_id": f"ORPHAN-{clause['clause_id']}",
                    "kind": "AMENDS_UNKNOWN_CLAUSE",
                    "status": "PENDING_HUMAN_REVIEW",
                    "contract_id": contract_id,
                    "section": section,
                    "clause_id": clause["clause_id"],
                    "evidence": {
                        "section_ref": clause.get("section_ref"),
                        "matching_original_found": False,
                    },
                    "impact_if_confirmed": (
                        "We hold an amendment but never received the clause it amends, so our "
                        "view of this contract section is incomplete."
                    ),
                    "action_required": (
                        "Confirm with the source system whether an earlier version of this "
                        "section exists and was missed by a previous export."
                    ),
                }
            )

    return proposals


def main(argv: Optional[List[str]] = None) -> int:
    argparse.ArgumentParser(
        description="Propose clause supersessions for human review (never applies them)."
    ).parse_args(argv)

    telemetry = Telemetry("contract-intelligence.supersede", LOG_DIR)
    try:
        with telemetry.span("supersede.run") as root:
            root.set_attribute("telemetry.backend", telemetry.backend)
            clauses = load_silver(SILVER_PATH)
            proposals = analyse(clauses)

            PROPOSALS_DIR.mkdir(parents=True, exist_ok=True)
            document = {
                "generated_at": utc_now(),
                "generated_by": "phase1_ingestion/supersede.py",
                "clauses_examined": len(clauses),
                "proposal_count": len(proposals),
                "status": "PENDING_HUMAN_REVIEW",
                "authority": (
                    "ADVISORY ONLY. This file records machine-generated proposals. Nothing in "
                    "the pipeline reads it back to change behaviour. Silver is unmodified."
                ),
                "detection_method": (
                    "Clauses grouped by contract_id + section number, with '(Amended)' markers "
                    "stripped. Section numbering is a convention, not a guarantee, so every "
                    "match is a candidate for human review rather than a conclusion."
                ),
                "proposals": proposals,
            }
            (PROPOSALS_DIR / "supersession_candidates.json").write_text(
                json.dumps(document, indent=2) + "\n", encoding="utf-8"
            )

            for proposal in proposals:
                telemetry.warn(
                    f"supersede.{proposal['kind'].lower()}",
                    proposal.get("impact_if_confirmed", ""),
                    proposal_id=proposal["proposal_id"],
                    contract_id=proposal["contract_id"],
                    section=proposal["section"],
                )
            telemetry.count("supersede.proposals_total", len(proposals))
            telemetry.info(
                "supersede.complete",
                f"{len(proposals)} proposal(s) from {len(clauses)} clause(s), all PENDING_HUMAN_REVIEW.",
                proposals=len(proposals),
            )
        return 0
    except SystemExit as exc:
        telemetry.error("supersede.failed", str(exc))
        return int(exc.code or 1)
    except Exception as exc:  # noqa: BLE001
        telemetry.error("supersede.failed", f"{type(exc).__name__}: {exc}")
        return 1
    finally:
        telemetry.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
