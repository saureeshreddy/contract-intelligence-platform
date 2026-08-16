#!/usr/bin/env python3
"""
phase2_agents/apply_proposal.py
===============================
The human approval step. The ONLY code in this repository that writes to
config/.

WHY THIS IS A SEPARATE SCRIPT
-----------------------------
The requirements's non-negotiable governance rule: the system must not modify its own behaviour,
prompts or decision logic without an explicit human approval step, and the
learning component must describe the mechanism rather than self-execute it.

That guarantee is worth exactly as much as its enforcement. A comment saying
"the agent must not do this" is not enforcement. So the boundary here is
structural:

    machine-owned    phase2_agents/output/    agents write freely
    human-owned      phase2_agents/config/    ONLY this script writes

Nothing in run.py, agents/, rules.py or llm.py opens a file under config/ for
writing. `tests/test_phase2.py::test_no_agent_process_writes_to_config` asserts
it by fingerprinting the directory across a full run.

This script is never called by the pipeline. A person runs it, by hand, with
their name.

WHAT MAKES THE APPROVAL REAL
----------------------------
1. Without --approved-by, this prints the diff and changes nothing. Reviewing
   is the default; applying is the exception.
2. The approver's name, the timestamp and the rationale are appended to the
   config file's own version_history, so the file carries its own provenance.
3. An approval record is appended to output/approvals.jsonl and to the audit
   ledger, so "who changed this standard, when, and on what evidence" is
   answerable from the ledger alone.
4. What changes is readable policy text -- not model weights, not a silently
   mutated prompt. The approver can actually evaluate the diff.

USAGE
  python phase2_agents/apply_proposal.py --list
  python phase2_agents/apply_proposal.py --id LP-indemnification-duty_to_defend_permitted
  python phase2_agents/apply_proposal.py --id LP-... --approved-by "K. Chen" \
      --rationale "Counsel reviewed; exceptions are correctly scoped."

EXIT CODES
  0  applied, or diff shown
  1  proposal not found / nothing to do
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.observability import Telemetry, default_log_dir, utc_now  # noqa: E402

PHASE_DIR = ROOT / "phase2_agents"
OUTPUT_DIR = PHASE_DIR / "output"
PROPOSALS_PATH = OUTPUT_DIR / "learning_proposals.json"
APPROVALS_PATH = OUTPUT_DIR / "approvals.jsonl"
AUDIT_PATH = OUTPUT_DIR / "audit_log.jsonl"

# The test suite exercises this script end to end; without this its console
# output buries the test results. The FILES it writes are unaffected -- a
# governance action is never silenced, only its terminal echo.
QUIET = os.environ.get("CI_TELEMETRY_QUIET", "") in ("1", "true", "yes")


def emit(text: str = "") -> None:
    if not QUIET:
        print(text)


def load_proposals() -> List[Dict[str, Any]]:
    if not PROPOSALS_PATH.exists():
        raise SystemExit(f"No proposals at {PROPOSALS_PATH}. Run phase2_agents/run.py first.")
    return json.loads(PROPOSALS_PATH.read_text(encoding="utf-8")).get("proposals", [])


def show(proposal: Dict[str, Any]) -> None:
    """Print what a human needs in order to decide. Nothing is changed."""
    emit()
    emit("=" * 78)
    emit(f"  {proposal['proposal_id']}   [{proposal['status']}]")
    emit("=" * 78)
    emit(f"\n  {proposal['summary']}\n")
    emit(f"  Kind        : {proposal['kind']}")
    emit(f"  Target file : {proposal['target_file']}")
    emit(f"  Occurrences : {proposal['occurrences']}")
    emit(f"  Evidence    : {', '.join(proposal['evidence_clause_ids'])}")
    emit(f"  Overrides   : {', '.join(proposal['evidence_override_ids'])}")
    emit("\n  PROPOSED CHANGE")
    emit("  " + "-" * 40)
    for line in json.dumps(proposal["current_value"], indent=2).splitlines():
        emit(f"  - {line}")
    for line in json.dumps(proposal["proposed_value"], indent=2).splitlines():
        emit(f"  + {line}")
    emit("\n  RATIONALE")
    emit(f"    {proposal['rationale']}")
    emit("\n  RISK IF APPLIED")
    emit(f"    {proposal['risk_if_applied']}")
    emit()
    emit("  Nothing has been changed. To apply:")
    emit(f"    python phase2_agents/apply_proposal.py --id {proposal['proposal_id']} \\")
    emit('        --approved-by "Your Name" --rationale "why you agree"')
    emit()


def apply_to_firm_standards(
    proposal: Dict[str, Any], approved_by: str, rationale: str, telemetry: Telemetry
) -> Dict[str, Any]:
    """Write the approved change into the human-owned policy file."""
    target = ROOT / proposal["target_file"]
    document = json.loads(target.read_text(encoding="utf-8"))

    # LP-<section>-<key>
    _, section, key = proposal["proposal_id"].split("-", 2)
    node = document.get("standards", {}).get(section, {}).get(key)
    if node is None:
        raise SystemExit(f"{proposal['target_file']} has no standards.{section}.{key} to change.")

    before = json.loads(json.dumps(node))
    proposed = proposal["proposed_value"]
    if isinstance(proposed, dict):
        node.update(proposed)
    else:
        node["value"] = proposed

    # The file carries its own provenance. Someone reading firm_standards.json
    # in a year can see who changed what and why without leaving the file.
    old_version = document.get("version", "0.0.0")
    major, minor, patch = (old_version.split(".") + ["0", "0", "0"])[:3]
    new_version = f"{major}.{int(minor) + 1}.0"
    document["version"] = new_version
    document.setdefault("version_history", []).append(
        {
            "version": new_version,
            "date": utc_now(),
            "approved_by": approved_by,
            "proposal_id": proposal["proposal_id"],
            "note": rationale or proposal["summary"],
            "evidence_clause_ids": proposal["evidence_clause_ids"],
        }
    )
    target.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    telemetry.warn(
        "config.changed_by_human",
        f"{approved_by} applied {proposal['proposal_id']} to {proposal['target_file']} "
        f"(v{old_version} -> v{new_version}).",
        approved_by=approved_by,
        proposal_id=proposal["proposal_id"],
        target_file=proposal["target_file"],
        version_from=old_version,
        version_to=new_version,
    )
    return {"before": before, "after": node, "version_from": old_version, "version_to": new_version}


def record_approval(proposal: Dict[str, Any], approved_by: str, rationale: str, change: Dict[str, Any]) -> None:
    """Append to the approval log AND the audit ledger.

    Both, deliberately. The approval log is the record of governance actions;
    the audit ledger is the record of everything that affected a decision.
    A config change is both, and "who changed this standard?" must be
    answerable from the ledger without knowing a second file exists.
    """
    approval = {
        "approval_id": f"APR-{proposal['proposal_id']}",
        "timestamp": utc_now(),
        "proposal_id": proposal["proposal_id"],
        "approved_by": approved_by,
        "rationale": rationale,
        "target_file": proposal["target_file"],
        "change": change,
        "evidence_override_ids": proposal["evidence_override_ids"],
    }
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with APPROVALS_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(approval, ensure_ascii=False) + "\n")

    with AUDIT_PATH.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "timestamp": utc_now(),
                    "agent": "human_approval",
                    "clause_id": "-",
                    "action": "apply_learning_proposal",
                    "model_output": None,
                    "human_decision": approval,
                    "effective_value": change["after"],
                    "decided_by": "human",
                    "status": "accepted",
                    "run_id": "manual",
                    "detail": (
                        f"{approved_by} approved {proposal['proposal_id']}. Firm standards "
                        f"v{change['version_from']} -> v{change['version_to']}. This is the only "
                        f"path by which system behaviour changes."
                    ),
                },
                ensure_ascii=False,
            )
            + "\n"
        )


def mark_applied(proposal_id: str, approved_by: str) -> None:
    document = json.loads(PROPOSALS_PATH.read_text(encoding="utf-8"))
    for proposal in document.get("proposals", []):
        if proposal["proposal_id"] == proposal_id:
            proposal["status"] = "APPROVED_AND_APPLIED"
            proposal["approved_by"] = approved_by
            proposal["approved_at"] = utc_now()
    PROPOSALS_PATH.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Review and apply a learning proposal. The only writer to config/."
    )
    parser.add_argument("--list", action="store_true", help="List pending proposals.")
    parser.add_argument("--id", help="Proposal id to review or apply.")
    parser.add_argument(
        "--approved-by",
        metavar="NAME",
        help="Your name. WITHOUT THIS, NOTHING IS CHANGED -- the diff is printed and the script exits.",
    )
    parser.add_argument("--rationale", default="", help="Why you approve. Recorded in the config file.")
    args = parser.parse_args(argv)

    proposals = load_proposals()

    if args.list or not args.id:
        if not proposals:
            emit("No proposals pending.")
            return 0
        emit(f"\n{len(proposals)} proposal(s):\n")
        for proposal in proposals:
            emit(f"  {proposal['proposal_id']:<48} [{proposal['status']}]")
            emit(f"      {proposal['summary']}")
            emit(f"      evidence: {', '.join(proposal['evidence_clause_ids'])}\n")
        emit("Review one with:  --id <proposal_id>")
        return 0

    match = next((p for p in proposals if p["proposal_id"] == args.id), None)
    if match is None:
        print(f"No proposal with id {args.id!r}. Use --list.", file=sys.stderr)
        return 1

    if not args.approved_by:
        # The safe default: show, do not touch.
        show(match)
        return 0

    if match["status"] != "PENDING_HUMAN_APPROVAL":
        emit(f"{match['proposal_id']} is already {match['status']}. Nothing to do.")
        return 1

    telemetry = Telemetry("contract-intelligence.approval", default_log_dir())
    try:
        with telemetry.span("approval.apply", **{"proposal.id": match["proposal_id"]}):
            change = apply_to_firm_standards(match, args.approved_by, args.rationale, telemetry)
            record_approval(match, args.approved_by, args.rationale, change)
            mark_applied(match["proposal_id"], args.approved_by)
        emit(
            f"\nApplied {match['proposal_id']} to {match['target_file']}\n"
            f"  approved by : {args.approved_by}\n"
            f"  version     : {change['version_from']} -> {change['version_to']}\n"
            f"  recorded in : {APPROVALS_PATH.name}, {AUDIT_PATH.name}\n\n"
            f"Re-run phase2_agents/run.py to review under the updated standards.\n"
        )
        return 0
    finally:
        telemetry.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
