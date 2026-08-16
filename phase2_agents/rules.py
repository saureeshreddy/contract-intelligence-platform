"""
phase2_agents/rules.py
======================
Deterministic policy checks. The part of the system that does NOT use an LLM.

WHY THIS EXISTS
---------------
"Payment shall be made within sixty (60) days" measured against a firm standard
of 30 is a comparison, not a judgement. Sending it to a language model would be
slower, cost money, and produce a *less* defensible answer.

A rule is free, instant, reproducible, and -- the part that matters for an
audited pipeline -- it cites the policy it applied:

    NOT  "the model thought this was risky"
    BUT  "60 days exceeds payment_terms.max_days_to_pay (30), firm standards
          v1.0.0, set 2025-01-01 by legal_counsel"

So rules handle the measurable, and the LLM handles the ambiguous residue --
language that needs interpretation rather than comparison against a number.
Every finding records `decided_by`, so the split is visible in the audit log
and in the cost report.

HONEST LIMITS
-------------
These are regex extractors over English legal prose. They are precise about
what they match and silent about what they do not; a clause phrased unusually
will simply produce no finding, and the LLM pass still sees it. They are not a
substitute for legal review, and `data_contract`-style limitations are listed
at the bottom of this docstring:

  * numbers must appear as digits in parentheses ("sixty (60) days"), which is
    the convention throughout this corpus but is not universal
  * negation and carve-outs are handled only where explicitly matched (e.g.
    "shall not extend to ... the Owner's sole negligence")
  * one clause per string; cross-clause interactions are invisible here
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15,
    "twenty": 20, "thirty": 30, "forty": 40, "forty-five": 45, "sixty": 60, "ninety": 90,
}

RULES_VERSION = "1.0.0"


# --------------------------------------------------------------------------
# Extractors
# --------------------------------------------------------------------------
def _days_near(text: str, anchor: str, window: int = 220) -> Optional[int]:
    """Find a day count near an anchor phrase.

    Scoped to a window because a termination clause routinely states two
    different notice periods (cause vs convenience) and picking the wrong one
    inverts the finding.
    """
    lowered = text.lower()
    index = lowered.find(anchor)
    if index == -1:
        return None
    start = max(0, index - window)
    segment = lowered[start : index + window]
    anchor_offset = index - start

    # NEAREST match, not first. CLZ-2025-0015 states two notice periods in one
    # clause -- 30 days for cause, then 21 for convenience. Taking the first
    # match in the window returns the cause period and inverts the finding.
    best: Optional[int] = None
    best_distance: Optional[int] = None
    for match in re.finditer(r"\((\d+)\)\s*(?:calendar\s+|business\s+)?days?", segment):
        distance = abs(match.start() - anchor_offset)
        if best_distance is None or distance < best_distance:
            best, best_distance = int(match.group(1)), distance
    return best


def _any_days(text: str) -> Optional[int]:
    match = re.search(r"\((\d+)\)\s*(?:calendar\s+|business\s+)?days?", text.lower())
    return int(match.group(1)) if match else None


def _liability_multiple(text: str) -> Optional[float]:
    lowered = text.lower()
    if "not exceed" not in lowered and "shall be limited to" not in lowered:
        return None
    match = re.search(r"\((\d+(?:\.\d+)?)\s*x\)", lowered)          # "two times (2x)"
    if match:
        return float(match.group(1))
    match = re.search(r"(\w+)\s+times\s+the\s+total\s+fees", lowered)
    if match and match.group(1) in NUMBER_WORDS:
        return float(NUMBER_WORDS[match.group(1)])
    if re.search(r"not exceed the total fees", lowered):
        return 1.0
    return None


def _money(text: str, pattern: str) -> Optional[float]:
    match = re.search(pattern, text.lower())
    return float(match.group(1).replace(",", "")) if match else None


def _tail_years(text: str) -> Optional[int]:
    match = re.search(r"([a-z\-]+|\d+)\s+years?\s+following\s+completion", text.lower())
    if not match:
        return None
    token = match.group(1)
    return int(token) if token.isdigit() else NUMBER_WORDS.get(token)


# --------------------------------------------------------------------------
# The checks
# --------------------------------------------------------------------------
def _finding(
    risk: str,
    severity: str,
    reference: str,
    observed: str,
    expected: str,
    alternative: str,
) -> Dict[str, Any]:
    return {
        "risk": risk,
        "severity": severity,
        # Rules are exact comparisons against a stated policy. Confidence is
        # 1.0 by construction -- there is nothing probabilistic happening.
        "confidence": 1.0,
        "standard_reference": reference,
        "observed": observed,
        "expected": expected,
        "suggested_alternative": alternative,
        "precedent_clause_ids": [],
    }


def check_indemnification(text: str, std: Dict[str, Any]) -> List[Dict[str, Any]]:
    lowered = text.lower()
    findings: List[Dict[str, Any]] = []
    rules = std.get("indemnification", {})

    # An explicit carve-out means the clause already limits itself.
    carved_out = bool(
        re.search(r"shall not (?:extend to|apply to)[^.]*owner'?s?\s+(?:sole\s+)?negligence", lowered)
        or re.search(r"no obligation to indemnify[^.]*owner'?s?\s+negligence", lowered)
        or re.search(r"neither party shall be required to indemnify", lowered)
    )
    covers_owner_negligence = bool(
        re.search(r"regardless of[^.]*owner'?s?\s+negligence", lowered)
        or re.search(r"caused in part by the owner'?s?\s+negligence", lowered)
    )
    if covers_owner_negligence and not carved_out:
        rule = rules.get("must_be_limited_to_own_negligence", {})
        findings.append(
            _finding(
                "Indemnity extends to claims caused by the Owner's own negligence.",
                rule.get("severity_if_breached", "high"),
                "indemnification.must_be_limited_to_own_negligence",
                "indemnity applies regardless of the Owner's negligence",
                "indemnity limited to the Consultant's negligent acts, errors, or omissions",
                rule.get(
                    "acceptable_language_example",
                    "Limit the indemnity to claims arising from the Consultant's negligence.",
                ),
            )
        )

    if not rules.get("duty_to_defend_permitted", {}).get("value", True):
        if re.search(r"\bdefend\b", lowered) and ("indemnif" in lowered):
            findings.append(
                _finding(
                    "Clause imposes a duty to defend, which attaches on allegation rather than on a finding of fault.",
                    rules["duty_to_defend_permitted"].get("severity_if_breached", "high"),
                    "indemnification.duty_to_defend_permitted",
                    "duty to defend present",
                    "indemnify only; no separate duty to defend",
                    "Strike 'defend' from the indemnity obligation, leaving indemnification for damages actually incurred.",
                )
            )

    if not rules.get("fullest_extent_permitted_by_law_permitted", {}).get("value", True):
        if "fullest extent permitted by law" in lowered:
            findings.append(
                _finding(
                    "Open-ended indemnity ceiling: exposure is whatever a future court permits.",
                    rules["fullest_extent_permitted_by_law_permitted"].get("severity_if_breached", "medium"),
                    "indemnification.fullest_extent_permitted_by_law_permitted",
                    "'to the fullest extent permitted by law'",
                    "an explicit, bounded limit of liability",
                    "Replace with an explicit cap tied to fees paid or to available insurance proceeds.",
                )
            )
    return findings


def check_liability(text: str, std: Dict[str, Any]) -> List[Dict[str, Any]]:
    rules = std.get("liability", {})
    findings: List[Dict[str, Any]] = []
    multiple = _liability_multiple(text)
    maximum = rules.get("max_cap_multiple_of_fees", {}).get("value")
    if multiple is not None and maximum is not None and multiple > float(maximum):
        findings.append(
            _finding(
                f"Liability cap of {multiple:g}x fees exceeds the firm standard of {float(maximum):g}x.",
                rules["max_cap_multiple_of_fees"].get("severity_if_breached", "high"),
                "liability.max_cap_multiple_of_fees",
                f"{multiple:g}x total fees",
                f"{float(maximum):g}x total fees",
                rules["max_cap_multiple_of_fees"].get(
                    "acceptable_language_example",
                    "Cap total liability at the total fees paid under this Agreement.",
                ),
            )
        )
    return findings


def check_payment_terms(text: str, std: Dict[str, Any]) -> List[Dict[str, Any]]:
    lowered = text.lower()
    rules = std.get("payment_terms", {})
    findings: List[Dict[str, Any]] = []

    days = _any_days(text)
    maximum = rules.get("max_days_to_pay", {}).get("value")
    if days is not None and maximum is not None and days > int(maximum):
        findings.append(
            _finding(
                f"Payment due in {days} days, beyond the firm standard of {maximum}.",
                rules["max_days_to_pay"].get("severity_if_breached", "medium"),
                "payment_terms.max_days_to_pay",
                f"{days} days",
                f"{maximum} days",
                f"Reduce the payment period to {maximum} days from receipt of a properly submitted invoice.",
            )
        )

    retainage = _money(text, r"up to\s+(\d+(?:\.\d+)?)%[^.]*retainage")
    if retainage is None:
        retainage = _money(text, r"withhold[^.]*?(\d+(?:\.\d+)?)%")
    max_retainage = rules.get("max_retainage_percent", {}).get("value")
    if retainage is not None and max_retainage is not None and retainage > float(max_retainage):
        findings.append(
            _finding(
                f"Retainage of {retainage:g}% exceeds the firm standard of {float(max_retainage):g}%.",
                rules["max_retainage_percent"].get("severity_if_breached", "medium"),
                "payment_terms.max_retainage_percent",
                f"{retainage:g}% retainage",
                f"{float(max_retainage):g}% maximum",
                "Remove retainage on professional services, or cap it at the firm standard and release on substantial completion.",
            )
        )

    if not rules.get("discretionary_withholding_permitted", {}).get("value", True):
        if re.search(r"withhold payment[^.]*(any dispute|disputes|reserves the right)", lowered):
            findings.append(
                _finding(
                    "Owner may withhold payment at its own initiative pending any dispute, with no objective test.",
                    rules["discretionary_withholding_permitted"].get("severity_if_breached", "high"),
                    "payment_terms.discretionary_withholding_permitted",
                    "unilateral right to withhold pending 'any disputes'",
                    "withholding permitted only for amounts actually in dispute, with written notice",
                    "Limit withholding to the disputed amount, require written notice of the basis, and require payment of the undisputed balance.",
                )
            )

    if rules.get("late_interest_required", {}).get("value") and "interest" not in lowered:
        findings.append(
            _finding(
                "No interest accrues on late payment, so there is no cost to the client for paying late.",
                rules["late_interest_required"].get("severity_if_breached", "low"),
                "payment_terms.late_interest_required",
                "no late-payment interest term",
                "interest accrues on overdue amounts",
                "Add: 'Late payments shall accrue interest at 1.5% per month until paid.'",
            )
        )
    return findings


def check_termination(text: str, std: Dict[str, Any]) -> List[Dict[str, Any]]:
    lowered = text.lower()
    rules = std.get("termination", {})
    findings: List[Dict[str, Any]] = []

    if "convenience" in lowered:
        days = _days_near(text, "convenience")
        minimum = rules.get("min_notice_days_for_convenience", {}).get("value")
        if days is not None and minimum is not None and days < int(minimum):
            findings.append(
                _finding(
                    f"Termination for convenience on {days} days' notice, below the firm standard of {minimum}.",
                    rules["min_notice_days_for_convenience"].get("severity_if_breached", "medium"),
                    "termination.min_notice_days_for_convenience",
                    f"{days} days' notice",
                    f"{minimum} days' notice",
                    f"Extend notice for convenience to {minimum} days.",
                )
            )
        if rules.get("demobilisation_costs_recoverable", {}).get("value"):
            recoverable = bool(re.search(r"demobili[sz]ation|wind[- ]down|termination expenses", lowered))
            excluded = bool(re.search(r"no additional compensation|no compensation for lost profits", lowered))
            if excluded and not recoverable:
                findings.append(
                    _finding(
                        "Termination for convenience recovers no demobilisation cost; the client's choice is funded by us.",
                        rules["demobilisation_costs_recoverable"].get("severity_if_breached", "medium"),
                        "termination.demobilisation_costs_recoverable",
                        "compensation limited to services performed",
                        "reasonable demobilisation and wind-down costs also recoverable",
                        "Add recovery of reasonable demobilisation costs on termination for convenience.",
                    )
                )
    return findings


def check_insurance(text: str, std: Dict[str, Any]) -> List[Dict[str, Any]]:
    rules = std.get("insurance", {})
    findings: List[Dict[str, Any]] = []

    per_claim = _money(text, r"professional liability insurance[^.]*?\$([\d,]+)\s*per claim")
    if per_claim is None:
        per_claim = _money(text, r"professional liability insurance of\s*\$([\d,]+)")
    maximum = rules.get("max_professional_liability_per_claim_usd", {}).get("value")
    if per_claim is not None and maximum is not None and per_claim > float(maximum):
        findings.append(
            _finding(
                f"Professional liability cover of ${per_claim:,.0f} per claim exceeds the ${float(maximum):,.0f} the firm carries as standard.",
                rules["max_professional_liability_per_claim_usd"].get("severity_if_breached", "medium"),
                "insurance.max_professional_liability_per_claim_usd",
                f"${per_claim:,.0f} per claim",
                f"${float(maximum):,.0f} per claim",
                "Either reduce the requirement to standard limits, or price a project-specific policy into the fee.",
            )
        )

    tail = _tail_years(text)
    max_tail = rules.get("max_tail_years_after_completion", {}).get("value")
    if tail is not None and max_tail is not None and tail > int(max_tail):
        findings.append(
            _finding(
                f"Tail coverage of {tail} years after completion exceeds the firm standard of {max_tail}.",
                rules["max_tail_years_after_completion"].get("severity_if_breached", "medium"),
                "insurance.max_tail_years_after_completion",
                f"{tail} years",
                f"{max_tail} years",
                f"Reduce the tail requirement to {max_tail} years, or price the additional premium into the fee.",
            )
        )
    return findings


def check_scope_of_work(text: str, std: Dict[str, Any]) -> List[Dict[str, Any]]:
    lowered = text.lower()
    rule = std.get("scope_of_work", {}).get("open_ended_scope_language_permitted", {})
    if rule.get("value", True):
        return []
    triggers = [p for p in rule.get("trigger_phrases", []) if p in lowered]
    if not triggers:
        return []
    return [
        _finding(
            "Open-ended scope language converts a fixed fee into an unbounded commitment.",
            rule.get("severity_if_breached", "medium"),
            "scope_of_work.open_ended_scope_language_permitted",
            f"'{triggers[0]}'",
            "an exhaustive, enumerated list of deliverables",
            "Replace open-ended phrasing with an enumerated deliverables list; handle additions through a change order.",
        )
    ]


CHECKS = {
    "indemnification": check_indemnification,
    "liability": check_liability,
    "payment_terms": check_payment_terms,
    "termination": check_termination,
    "insurance": check_insurance,
    "scope_of_work": check_scope_of_work,
}


def evaluate(category: str, clause_text: str, firm_standards: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Apply the checks for one category. Returns RiskFinding-shaped dicts."""
    check = CHECKS.get(category)
    if check is None:
        return []
    return check(clause_text, firm_standards.get("standards", {}))


def find_precedents(
    category: str, clause_id: str, clean_by_category: Dict[str, List[str]], limit: int = 3
) -> List[str]:
    """Clauses in our own corpus, same category, that breach no firm standard.

    This is why "suggest an alternative" is worth something here. Rather than
    inventing boilerplate, we can point at language the firm has already
    accepted on a comparable contract -- which is a far easier sell to a
    client than a lawyer's redline out of nowhere.

    A dict lookup, not a vector store: 20 clauses do not need embeddings, and
    an inspectable grouping beats an opaque similarity score at this size.
    """
    return [cid for cid in clean_by_category.get(category, []) if cid != clause_id][:limit]
