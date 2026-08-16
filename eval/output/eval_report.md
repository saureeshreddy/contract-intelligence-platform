# Agent Evaluation

- **Generated:** 2026-08-16T16:53:06.254Z
- **Ground truth:** `eval/ground_truth.json` v1.0.0 (20 clauses, human-labelled)
- **Model:** stub:claude-sonnet-5 · prompt `classifier.v1`
- **Firm standards:** v1.0.0

> **20 clauses labelled by one person. A regression detector, not a measurement.**
> Numbers here catch drift; they do not establish accuracy.

## Classification

| Mode | n | Correct | Accuracy |
|---|---|---|---|
| as_configured | 20 | 20 | **100%** |
| model_only | 20 | 20 | **100%** |

Decision paths — as_configured:

- `rule` — 20/20 (100%)

Decision paths — model_only:

- `model` — 19/19 (100%)
- `rule` — 1/1 (100%)

No misclassifications.


Confidence when correct: 0.92 · when wrong: None

## Risk detection

| Level | Precision | Recall | F1 | TP | FP | FN |
|---|---|---|---|---|---|---|
| clause level | 1.00 | 1.00 | 1.00 | 11 | 0 | 0 |
| standard level | 1.00 | 1.00 | 1.00 | 18 | 0 | 0 |

*Clause level* = did we flag the right clauses. *Standard level* = did we flag them for the right reason.

No disagreements with the human labels.

## What these numbers do not tell you

- The stub classifier is a keyword matcher tuned against this corpus. A high score measures the harness, not the model. The number becomes meaningful when `model.provider: anthropic` is configured.
- Risk-detection figures are the informative ones: the labels were derived from clause text and the policy file, independent of `rules.py`.
- 20 items, one annotator, no held-out split, no confidence intervals.
- Nothing here measures whether the *firm standards themselves* are right. That is a legal judgement, and it is what the override signal in the audit log is for.
