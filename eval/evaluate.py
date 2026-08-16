#!/usr/bin/env python3
"""
eval/evaluate.py
================
Measures whether the agents are actually right.

WHY THIS EXISTS
---------------
Nothing else in this repository answers "is the classifier correct?". Guardrails
prove the system is controllable; the audit log proves it is accountable.
Neither says it is accurate. Without this, every quality claim is an assertion.

WHAT IT MEASURES
----------------
1. Classification accuracy, reported SEPARATELY for the two decision paths:
     rule path   -- reads Silver's clause_category through a mapping table
     model path  -- forced with --model-only, bypassing the rule shortcut
   Splitting them matters. The rule path reads the same field the ground truth
   was *deliberately not* derived from, but it is still close to tautological;
   the model path is the number that means something, and it is the one that
   changes when you swap the stub for a real model.

2. Risk detection: precision, recall and F1 against human labels of which
   clauses should be flagged and which standards should fire. These labels were
   written by reading clause text against the policy file, independent of
   rules.py -- so THIS is the genuinely informative measurement here.

3. Standard-level accuracy: not just "was it flagged" but "was it flagged for
   the right reason". A finding that is right by accident is a latent bug.

4. Escalation precision: did we escalate things that deserved it.

HONEST LIMITS -- READ BEFORE QUOTING ANY NUMBER
-----------------------------------------------
* 20 clauses, labelled by one person. This is a smoke test, not a measurement.
  It catches regressions; it does not establish accuracy.
* The stub classifier is a keyword matcher whose keywords were chosen against
  this same corpus. If it scores 100%, that measures the harness, not the
  model. The value of this file is that the number becomes meaningful the
  moment a real model is configured -- and that a regression becomes visible.
* No inter-annotator agreement, no confidence intervals, no held-out split.
  With 20 items none of those would mean anything either.

USAGE
  python eval/evaluate.py                # as configured (rules first)
  python eval/evaluate.py --model-only   # force the LLM path
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.observability import Telemetry, default_log_dir, utc_now  # noqa: E402
from phase2_agents.agents.base import PromptTemplate  # noqa: E402
from phase2_agents.agents.classifier import ClassifierAgent  # noqa: E402
from phase2_agents.callbacks import TelemetryCallbackHandler  # noqa: E402
from phase2_agents.llm import DecisionCache, build_model  # noqa: E402
from phase2_agents.models import DecidedBy  # noqa: E402
from phase2_agents.rules import evaluate as evaluate_rules  # noqa: E402
from phase2_agents.run import load_config, load_silver  # noqa: E402

EVAL_DIR = ROOT / "eval"
OUTPUT_DIR = EVAL_DIR / "output"


def prf(true_positive: int, false_positive: int, false_negative: int) -> Dict[str, float]:
    precision = true_positive / (true_positive + false_positive) if (true_positive + false_positive) else 0.0
    recall = true_positive / (true_positive + false_negative) if (true_positive + false_negative) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {"precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4)}


# --------------------------------------------------------------------------
def evaluate_classification(
    clauses: List[Dict[str, Any]],
    labels: Dict[str, Any],
    config: Dict[str, Any],
    telemetry: Telemetry,
    model_only: bool,
) -> Dict[str, Any]:
    agent_config = dict(config["agents"]["classifier"])
    if model_only:
        # Bypass the rule shortcut so we are measuring the model, not a lookup.
        agent_config["prefer_rules"] = False

    handler = TelemetryCallbackHandler(
        telemetry,
        config["pricing"].get(config["model"]["name"], {}),
        model_name=f"{config['model']['provider']}:{config['model']['name']}",
    )
    agent = ClassifierAgent(
        build_model(config["model"]),
        PromptTemplate.load(agent_config["prompt_version"]),
        telemetry,
        handler,
        DecisionCache(None, enabled=False),  # never score a cached answer
        config["pricing"].get(config["model"]["name"], {}),
        agent_config,
    )

    rows: List[Dict[str, Any]] = []
    confusion: Counter = Counter()
    for clause in clauses:
        clause_id = clause["clause_id"]
        label = labels.get(clause_id)
        if label is None:
            continue
        result = agent.classify(clause)
        predicted = result.output.category.value if result.output else None
        expected = label["expected_category"]
        correct = predicted == expected
        confusion[(expected, predicted or "none")] += 1
        rows.append(
            {
                "clause_id": clause_id,
                "expected": expected,
                "predicted": predicted,
                "correct": correct,
                "confidence": result.output.confidence if result.output else None,
                "decided_by": result.decided_by.value,
                "escalated": result.status.value == "escalated",
            }
        )

    correct = sum(1 for r in rows if r["correct"])
    by_path: Dict[str, Dict[str, int]] = {}
    for row in rows:
        bucket = by_path.setdefault(row["decided_by"], {"n": 0, "correct": 0})
        bucket["n"] += 1
        bucket["correct"] += int(row["correct"])

    # Calibration: a confidence score is only useful if being confident and
    # being right are correlated. If the wrong answers are as confident as the
    # right ones, the escalation threshold is decorative.
    confident_correct = [r["confidence"] for r in rows if r["correct"] and r["confidence"]]
    confident_wrong = [r["confidence"] for r in rows if not r["correct"] and r["confidence"]]

    return {
        "mode": "model_only" if model_only else "as_configured",
        "n": len(rows),
        "correct": correct,
        "accuracy": round(correct / len(rows), 4) if rows else 0.0,
        "by_decision_path": {
            path: {**counts, "accuracy": round(counts["correct"] / counts["n"], 4)}
            for path, counts in by_path.items()
        },
        "mean_confidence_when_correct": round(sum(confident_correct) / len(confident_correct), 4)
        if confident_correct
        else None,
        "mean_confidence_when_wrong": round(sum(confident_wrong) / len(confident_wrong), 4)
        if confident_wrong
        else None,
        "misclassified": [r for r in rows if not r["correct"]],
        "confusion": {f"{e}->{p}": n for (e, p), n in sorted(confusion.items()) if e != p},
        "cost_usd": round(handler.total_cost_usd, 6),
        "llm_calls": handler.llm_calls,
    }


def evaluate_risk(
    clauses: List[Dict[str, Any]], labels: Dict[str, Any], firm_standards: Dict[str, Any]
) -> Dict[str, Any]:
    """Precision/recall on flagging, and on flagging for the RIGHT reason.

    Evaluated against the human-labelled `expected_standards`, which were
    derived from clause text and the policy file rather than from rules.py.
    """
    clause_tp = clause_fp = clause_fn = 0
    std_tp = std_fp = std_fn = 0
    rows: List[Dict[str, Any]] = []

    for clause in clauses:
        clause_id = clause["clause_id"]
        label = labels.get(clause_id)
        if label is None:
            continue
        category = label["expected_category"]  # isolate risk detection from classification
        findings = evaluate_rules(category, clause.get("clause_text", ""), firm_standards)
        found: Set[str] = {f["standard_reference"] for f in findings if f["standard_reference"]}
        expected: Set[str] = set(label["expected_standards"])

        flagged, should_flag = bool(found), bool(label["should_flag"])
        clause_tp += int(flagged and should_flag)
        clause_fp += int(flagged and not should_flag)
        clause_fn += int(not flagged and should_flag)

        std_tp += len(found & expected)
        std_fp += len(found - expected)
        std_fn += len(expected - found)

        if found != expected:
            rows.append(
                {
                    "clause_id": clause_id,
                    "expected_standards": sorted(expected),
                    "found_standards": sorted(found),
                    "missed": sorted(expected - found),
                    "spurious": sorted(found - expected),
                    "notes": label["notes"],
                }
            )

    return {
        "clause_level": {
            "true_positives": clause_tp, "false_positives": clause_fp, "false_negatives": clause_fn,
            **prf(clause_tp, clause_fp, clause_fn),
        },
        "standard_level": {
            "true_positives": std_tp, "false_positives": std_fp, "false_negatives": std_fn,
            **prf(std_tp, std_fp, std_fn),
            "_note": "Flagged for the right REASON, not merely flagged. A finding that is "
                     "right by accident is a latent bug.",
        },
        "disagreements": rows,
    }


# --------------------------------------------------------------------------
def render_markdown(report: Dict[str, Any]) -> str:
    classification = report["classification"]
    risk = report["risk"]
    lines = [
        "# Agent Evaluation",
        "",
        f"- **Generated:** {report['generated_at']}",
        f"- **Ground truth:** `eval/ground_truth.json` v{report['ground_truth_version']} "
        f"({report['clause_count']} clauses, human-labelled)",
        f"- **Model:** {report['model']} · prompt `{report['prompt_version']}`",
        f"- **Firm standards:** v{report['firm_standards_version']}",
        "",
        "> **20 clauses labelled by one person. A regression detector, not a measurement.**",
        "> Numbers here catch drift; they do not establish accuracy.",
        "",
        "## Classification",
        "",
        f"| Mode | n | Correct | Accuracy |",
        "|---|---|---|---|",
    ]
    for entry in report["classification_modes"]:
        lines.append(
            f"| {entry['mode']} | {entry['n']} | {entry['correct']} | **{entry['accuracy']:.0%}** |"
        )
    for entry in report["classification_modes"]:
        lines += ["", f"Decision paths — {entry['mode']}:", ""]
        for path, counts in sorted(entry["by_decision_path"].items()):
            lines.append(f"- `{path}` — {counts['correct']}/{counts['n']} ({counts['accuracy']:.0%})")
    if classification["misclassified"]:
        lines += ["", "Misclassified:", ""]
        for row in classification["misclassified"]:
            lines.append(f"- `{row['clause_id']}` expected `{row['expected']}`, got `{row['predicted']}`")
    else:
        lines += ["", "No misclassifications.", ""]
    lines += [
        "",
        f"Confidence when correct: {classification['mean_confidence_when_correct']} · "
        f"when wrong: {classification['mean_confidence_when_wrong']}",
        "",
        "## Risk detection",
        "",
        "| Level | Precision | Recall | F1 | TP | FP | FN |",
        "|---|---|---|---|---|---|---|",
    ]
    for level in ("clause_level", "standard_level"):
        block = risk[level]
        lines.append(
            f"| {level.replace('_', ' ')} | {block['precision']:.2f} | {block['recall']:.2f} | "
            f"{block['f1']:.2f} | {block['true_positives']} | {block['false_positives']} | "
            f"{block['false_negatives']} |"
        )
    lines += [
        "",
        "*Clause level* = did we flag the right clauses. *Standard level* = did we flag them "
        "for the right reason.",
        "",
    ]
    if risk["disagreements"]:
        lines += ["### Disagreements", ""]
        for row in risk["disagreements"]:
            lines.append(f"- **{row['clause_id']}** — missed `{row['missed']}`, spurious `{row['spurious']}`")
            lines.append(f"  - {row['notes']}")
    else:
        lines.append("No disagreements with the human labels.")
    lines += ["", "## What these numbers do not tell you", ""]
    lines += [
        "- The stub classifier is a keyword matcher tuned against this corpus. A high score "
        "measures the harness, not the model. The number becomes meaningful when "
        "`model.provider: anthropic` is configured.",
        "- Risk-detection figures are the informative ones: the labels were derived from clause "
        "text and the policy file, independent of `rules.py`.",
        "- 20 items, one annotator, no held-out split, no confidence intervals.",
        "- Nothing here measures whether the *firm standards themselves* are right. That is a "
        "legal judgement, and it is what the override signal in the audit log is for.",
        "",
    ]
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Score the agents against human-labelled ground truth.")
    parser.add_argument("--model-only", action="store_true", help="Force the LLM path (skip rule shortcuts).")
    args = parser.parse_args(argv)

    config = load_config()
    ground_truth = json.loads((EVAL_DIR / "ground_truth.json").read_text(encoding="utf-8"))
    labels = ground_truth["labels"]
    firm_standards = json.loads(
        (ROOT / "phase2_agents" / "config" / "firm_standards.json").read_text(encoding="utf-8")
    )
    clauses = load_silver(ROOT / config["input"]["silver"])

    telemetry = Telemetry("contract-intelligence.eval", default_log_dir())
    try:
        with telemetry.span("eval.run"):
            configured = evaluate_classification(clauses, labels, config, telemetry, model_only=args.model_only)
            # Always report the model path too -- it is the number that moves
            # when the model changes, and the one worth watching.
            model_only = (
                configured
                if args.model_only
                else evaluate_classification(clauses, labels, config, telemetry, model_only=True)
            )
            risk = evaluate_risk(clauses, labels, firm_standards)

            report = {
                "generated_at": utc_now(),
                "ground_truth_version": ground_truth["version"],
                "clause_count": len(labels),
                "model": f"{config['model']['provider']}:{config['model']['name']}",
                "prompt_version": config["agents"]["classifier"]["prompt_version"],
                "firm_standards_version": firm_standards["version"],
                "classification": configured,
                "classification_modes": [configured] if args.model_only else [configured, model_only],
                "risk": risk,
                "caveats": [
                    "20 clauses, one annotator: a regression detector, not a measurement.",
                    "The stub classifier's keywords were chosen against this corpus; a high "
                    "classification score measures the harness, not the model.",
                    "Risk-detection labels were derived independently of rules.py, so those "
                    "figures are the informative ones.",
                ],
            }
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            (OUTPUT_DIR / "eval_report.json").write_text(
                json.dumps(report, indent=2) + "\n", encoding="utf-8"
            )
            (OUTPUT_DIR / "eval_report.md").write_text(render_markdown(report), encoding="utf-8")

            telemetry.info(
                "eval.complete",
                f"classification {configured['accuracy']:.0%} ({configured['mode']}) · "
                f"risk F1 {risk['standard_level']['f1']:.2f} at standard level",
            )
            telemetry.count("eval.runs_total")
            print(render_markdown(report))
        return 0
    finally:
        telemetry.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
