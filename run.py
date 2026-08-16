#!/usr/bin/env python3
"""
run.py
======
The single entry point. Cross-platform, no shell required.

    python run.py all           everything, from a clean slate  (~30s)
    python run.py phase1        ingest -> drift -> normalize -> supersede
    python run.py phase2        classify -> flag risk -> audit -> gold
    python run.py eval          accuracy against human-labelled ground truth
    python run.py test          the test suite
    python run.py clean         delete generated outputs
    python run.py --help

WHY THIS REPLACED THE SHELL SCRIPTS
-----------------------------------
This started as run_phase1.sh + run_phase1.bat + run_phase2.sh + .bat +
run_all.sh + .bat -- six files, two dialects, and every change needed making
twice. The .bat versions were also the only Windows-specific thing in the
repository, in a project whose sole prerequisite is Python.

One Python file removes the duplication, works identically on every platform,
and gets things a shell script cannot easily have: real exit-code assertions,
a `clean` command that will not delete the wrong directory, and a dependency
check that prints what to install rather than a traceback.

WHAT `all` DEMONSTRATES
-----------------------
It deliberately runs the FAILURE paths, not the happy path:

  * ingestion crashes inside the commit window, then resumes and dedupes
  * the agent run blows a deliberately low budget and halts
  * a blind resume is refused until a human acknowledges the halt

so the committed outputs are evidence rather than claims. Steps that are
*expected* to fail declare their expected exit code, and this script fails
loudly if one of them unexpectedly succeeds -- a guardrail that silently
stopped working would otherwise look exactly like a passing run.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Sequence

ROOT = Path(__file__).resolve().parent
PYTHON = sys.executable

# Generated directories. Listed explicitly, never globbed: `clean` deleting the
# wrong thing is a much worse failure than `clean` missing something.
GENERATED = [
    ROOT / "phase1_ingestion" / "output",
    ROOT / "phase2_agents" / "output",
    ROOT / "output",
    ROOT / "eval" / "output",
]

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"
if sys.platform == "win32" and not sys.stdout.isatty():
    BOLD = DIM = RESET = ""


def banner(text: str) -> None:
    print(f"\n{BOLD}{'=' * 78}\n {text}\n{'=' * 78}{RESET}", flush=True)


def step(text: str) -> None:
    print(f"\n{BOLD}{text}{RESET}\n{'-' * 78}", flush=True)


def run(
    args: Sequence[str],
    *,
    expect: Optional[Sequence[int]] = None,
    quiet_tests: bool = False,
) -> int:
    """Run a subcommand and assert its exit code.

    `expect` is the point: a step that is supposed to halt must actually halt.
    If a guardrail stops working, the run turns green and nobody notices -- so
    an unexpected SUCCESS is treated as a failure here.
    """
    env = None
    if quiet_tests:
        import os

        env = {**os.environ, "CI_TELEMETRY_QUIET": "1"}

    result = subprocess.run([PYTHON, *args], cwd=ROOT, env=env)
    allowed = list(expect) if expect is not None else [0]
    if result.returncode not in allowed:
        print(
            f"\n{BOLD}FAILED{RESET}: {' '.join(args)}\n"
            f"  exit {result.returncode}, expected one of {allowed}",
            file=sys.stderr,
        )
        raise SystemExit(1)
    if expect is not None and expect != [0]:
        print(f"{DIM}     -> exit {result.returncode} (expected){RESET}", flush=True)
    return result.returncode


# --------------------------------------------------------------------------
def check_dependencies(phase2: bool) -> None:
    """Fail with an instruction, not a traceback."""
    if sys.version_info < (3, 10):
        raise SystemExit(
            f"Python 3.10+ required (langchain-core needs it); this is "
            f"{sys.version_info.major}.{sys.version_info.minor}."
        )
    if not phase2:
        return
    missing = [
        name
        for name, module in (("langchain-core", "langchain_core"), ("pydantic", "pydantic"), ("PyYAML", "yaml"))
        if not __import__("importlib.util", fromlist=["util"]).find_spec(module)
    ]
    if missing:
        raise SystemExit(
            f"Phase 2 needs: {', '.join(missing)}\n"
            f"  pip install -r requirements.txt\n"
            f"(Phase 1 runs on the standard library alone: python run.py phase1)"
        )


def reset(*paths: Path) -> None:
    """Wipe one phase's outputs so its demonstration starts from a known state.

    Each phase command is a DEMONSTRATION, and a demonstration that only works
    the first time is not one. Re-running without this hits three different
    already-done paths -- ingest no-ops on a complete checkpoint, drift finds no
    change because the registry already knows the schema, and the agent run
    skips every clause so the budget never trips. All three are *correct system
    behaviour*; they just do not show anything.

    Resuming and re-running for real is still available through the underlying
    scripts, which is what the README's command reference documents.
    """
    for path in paths:
        if path.exists():
            shutil.rmtree(path)


def clean() -> None:
    for path in GENERATED:
        if path.exists():
            shutil.rmtree(path)
            print(f"  removed {path.relative_to(ROOT)}")
    print("Generated outputs deleted. Regenerate with: python run.py all")


# --------------------------------------------------------------------------
def phase1() -> None:
    banner("PHASE 1 - ingestion, drift detection, normalization")
    reset(ROOT / "phase1_ingestion" / "output")

    step("1/5  Ingest batch 1 (API v2.1, 12 clauses) -- establishes the baseline schema")
    run(["phase1_ingestion/ingest.py", "--source", "data/clauses_batch_1.json"])

    step("2/5  Ingest batch 2 -- crash INSIDE the commit window after 5 records")
    print(f"{DIM}     The record is on disk but the checkpoint has not advanced.{RESET}")
    run(
        ["phase1_ingestion/ingest.py", "--source", "data/clauses_batch_2.json",
         "--simulate-crash-after", "5", "--crash-window"],
        expect=[1],
    )

    step("3/5  Re-run the SAME command -- resumes, dedupes, and detects the drift")
    run(
        ["phase1_ingestion/ingest.py", "--source", "data/clauses_batch_2.json", "--fail-on-drift"],
        expect=[2],  # data landed; drift needs a human before the downstream stage
    )

    step("4/5  Normalize Bronze -> Silver (human-confirmed aliases, replay dedupe)")
    run(["phase1_ingestion/normalize.py"])

    step("5/5  Propose clause supersessions for human review")
    run(["phase1_ingestion/supersede.py"])


def phase2() -> None:
    banner("PHASE 2 - multi-agent review, guardrails, audit ledger")
    reset(ROOT / "phase2_agents" / "output")
    if not (ROOT / "phase1_ingestion/output/silver/clauses.jsonl").exists():
        raise SystemExit("Phase 1 output missing. Run: python run.py phase1")

    step("1/4  Budget halt -- an artificially low ceiling stops the run")
    print(f"{DIM}     All 20 clauses are still accounted for; none are silently dropped.{RESET}")
    run(["phase2_agents/run.py", "--max-cost-usd", "0.005", "--no-cache"], expect=[2])

    step("2/4  Blind resume is REFUSED -- a human must say they looked")
    run(["phase2_agents/run.py"], expect=[2])

    step("3/4  Full review, with the halt acknowledged")
    run(
        ["phase2_agents/run.py", "--acknowledge-halt",
         "Budget was set low to demonstrate the guardrail."],
        expect=[0, 3],  # 3 = complete, escalations open
    )

    step("4/4  Learning proposal -- review only; nothing is changed")
    run(["phase2_agents/apply_proposal.py", "--list"])
    print(
        f"{DIM}     To apply one -- the ONLY path that changes system behaviour:\n"
        f"       python phase2_agents/apply_proposal.py --id <id> --approved-by \"Your Name\"{RESET}"
    )


def evaluate() -> None:
    banner("EVALUATION - agent accuracy against human-labelled ground truth")
    run(["eval/evaluate.py"])


def test() -> None:
    banner("TESTS")
    run(["-m", "unittest", "discover", "-s", "tests"], quiet_tests=True)


def summary() -> None:
    banner("DONE")
    for path, why in [
        ("phase1_ingestion/output/drift/drift_report.md", "what changed upstream, and what needs a human"),
        ("phase2_agents/output/gold/clause_risk_register.json", "the effective view of every clause"),
        ("phase2_agents/output/audit_log.json", "every decision - model vs human"),
        ("phase2_agents/output/learning_proposals.json", "the system asking permission to change itself"),
        ("phase2_agents/output/run_summary.json", "cost, cache, guardrail state"),
        ("eval/output/eval_report.md", "accuracy"),
        ("docs/architecture_note.md", "one page on why it is built this way"),
    ]:
        mark = " " if (ROOT / path).exists() else "!"
        print(f"  {mark} {path:<52} {DIM}{why}{RESET}")


# --------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Contract Intelligence Pipeline - single cross-platform entry point.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("WHY THIS REPLACED")[0].split("=\n")[-1].strip(),
    )
    parser.add_argument(
        "command",
        choices=["all", "phase1", "phase2", "eval", "test", "clean"],
        help="what to run",
    )
    parser.add_argument(
        "--keep-outputs",
        action="store_true",
        help="with 'all', do not wipe generated outputs first (they are regenerated anyway)",
    )
    args = parser.parse_args(argv)

    if args.command == "clean":
        clean()
        return 0

    check_dependencies(phase2=args.command in ("all", "phase2", "eval", "test"))

    if args.command == "all":
        if not args.keep_outputs:
            clean()
        phase1()
        phase2()
        evaluate()
        test()
        summary()
    elif args.command == "phase1":
        phase1()
    elif args.command == "phase2":
        phase2()
    elif args.command == "eval":
        evaluate()
    elif args.command == "test":
        test()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
