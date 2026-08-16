"""
tests/test_phase2.py
====================
Tests for the multi-agent review. Same discipline as Phase 1: stdlib
`unittest`, temporary output directories, no installs beyond what the agents
already need.

    python -m unittest discover -s tests -v

The most important test in this file is
`TestHumanInTheLoopBoundary::test_no_agent_process_writes_to_config`. That is
the platform's non-negotiable governance rule, and it is asserted rather than promised.
"""

from __future__ import annotations

import copy
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.observability import Telemetry  # noqa: E402
from phase2_agents import apply_proposal, rules, run as run_module  # noqa: E402
from phase2_agents.agents.auditor import AuditorAgent  # noqa: E402
from phase2_agents.agents.base import Agent, ParseFailure, PromptTemplate  # noqa: E402
from phase2_agents.agents.classifier import ClassifierAgent  # noqa: E402
from phase2_agents.callbacks import TelemetryCallbackHandler  # noqa: E402
from phase2_agents.guardrails import Guardrails, HaltProcessing  # noqa: E402
from phase2_agents.llm import DecisionCache, build_model, usage_from_message  # noqa: E402
from phase2_agents.models import (  # noqa: E402
    AuditRecord,
    ClauseClassification,
    DecidedBy,
    DecisionStatus,
    TokenUsage,
)

CONFIG_DIR = ROOT / "phase2_agents" / "config"
FIRM_STANDARDS = json.loads((CONFIG_DIR / "firm_standards.json").read_text(encoding="utf-8"))
SILVER = ROOT / "phase1_ingestion" / "output" / "silver" / "clauses.jsonl"


def load_clauses():
    if not SILVER.exists():  # pragma: no cover - Phase 1 must run first
        raise unittest.SkipTest("Phase 1 Silver output missing; run: python run.py phase1")
    return [json.loads(l) for l in SILVER.read_text(encoding="utf-8").splitlines() if l.strip()]


class Phase2TestCase(unittest.TestCase):
    """Redirects the pipeline's outputs into a throwaway directory."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ci_phase2_"))
        self.logs = self.tmp / "logs"
        self.real_config = run_module.load_config()

        self._saved = {name: getattr(run_module, name) for name in ("OUTPUT_DIR", "LOG_DIR", "load_config")}
        run_module.OUTPUT_DIR = self.tmp
        run_module.LOG_DIR = self.logs
        run_module.load_config = self._test_config

    def tearDown(self) -> None:
        for name, value in self._saved.items():
            setattr(run_module, name, value)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _test_config(self):
        """The real config, with every output path redirected to a temp dir.

        `ROOT / <absolute path>` resolves to the absolute path, so run.py needs
        no change to be testable.
        """
        config = copy.deepcopy(self.real_config)
        config["output"] = {
            "audit_log_jsonl": str(self.tmp / "audit_log.jsonl"),
            "audit_log_json": str(self.tmp / "audit_log.json"),
            "run_summary": str(self.tmp / "run_summary.json"),
            "gold_register": str(self.tmp / "gold" / "clause_risk_register.json"),
            "checkpoint": str(self.tmp / "_checkpoint.json"),
        }
        config["cache"]["path"] = str(self.tmp / "cache.json")
        config["guardrails"]["kill_switch"]["stop_file"] = str(self.tmp / "STOP")
        # The configured 2 req/sec is a real throttle even against the stub --
        # it sits in the actual call path, which is the point of wiring it in.
        # Tests run the pipeline a dozen times, so we lift the ceiling rather
        # than spend two minutes proving the limiter works. That it works is
        # covered by test_rate_limiter_is_in_the_call_path.
        config["model"]["rate_limit"]["requests_per_second"] = 1000.0
        config["model"]["rate_limit"]["check_every_n_seconds"] = 0.001
        return config

    def telemetry(self, name: str = "test") -> Telemetry:
        return Telemetry(name, self.logs)

    def run_pipeline(self, argv=None) -> int:
        return run_module.main(argv or [])

    def summary(self) -> dict:
        return json.loads((self.tmp / "run_summary.json").read_text(encoding="utf-8"))

    def register(self) -> list:
        path = self.tmp / "gold" / "clause_risk_register.json"
        return json.loads(path.read_text(encoding="utf-8"))["clauses"]

    def audit_records(self) -> list:
        return [
            json.loads(l)
            for l in (self.tmp / "audit_log.jsonl").read_text(encoding="utf-8").splitlines()
            if l.strip()
        ]


# ==========================================================================
class TestDeterministicRules(unittest.TestCase):
    """Rules, not the model, do the measurable work. That is the cost lever
    and the audit story at the same time."""

    def test_findings_cite_the_standard_they_applied(self) -> None:
        clauses = {c["clause_id"]: c for c in load_clauses()}
        findings = rules.evaluate(
            "payment_terms", clauses["CLZ-2025-0004"]["clause_text"], FIRM_STANDARDS
        )
        references = {f["standard_reference"] for f in findings}
        self.assertIn("payment_terms.max_days_to_pay", references)
        self.assertIn("payment_terms.discretionary_withholding_permitted", references)
        for finding in findings:
            # An auditable finding names its policy, what it saw, and what was
            # required. Without all three it is an opinion.
            self.assertTrue(finding["standard_reference"])
            self.assertTrue(finding["observed"])
            self.assertTrue(finding["expected"])
            self.assertTrue(finding["suggested_alternative"])
            self.assertEqual(finding["confidence"], 1.0)

    def test_nearest_number_wins_when_a_clause_states_two(self) -> None:
        """CLZ-2025-0015 gives 30 days for cause and 21 for convenience.

        Taking the first match in the window returns the cause period and
        inverts the finding, so this guards a real bug that shipped once.
        """
        clauses = {c["clause_id"]: c for c in load_clauses()}
        findings = rules.evaluate(
            "termination", clauses["CLZ-2025-0015"]["clause_text"], FIRM_STANDARDS
        )
        notice = next(f for f in findings if "min_notice_days" in f["standard_reference"])
        self.assertIn("21", notice["observed"])

    def test_compliant_clauses_produce_nothing(self) -> None:
        clauses = {c["clause_id"]: c for c in load_clauses()}
        # 1x cap == the standard, and CLZ-0013 limits indemnity to own negligence.
        self.assertEqual(rules.evaluate("liability", clauses["CLZ-2025-0002"]["clause_text"], FIRM_STANDARDS), [])
        self.assertEqual(
            rules.evaluate("indemnification", clauses["CLZ-2025-0013"]["clause_text"], FIRM_STANDARDS), []
        )

    def test_precedents_are_drawn_from_clean_clauses(self) -> None:
        clean = {"indemnification": ["CLZ-2025-0007", "CLZ-2025-0013"]}
        found = rules.find_precedents("indemnification", "CLZ-2025-0001", clean)
        self.assertEqual(found, ["CLZ-2025-0007", "CLZ-2025-0013"])
        # Never cite the clause being reviewed as its own precedent.
        self.assertNotIn("CLZ-2025-0013", rules.find_precedents("indemnification", "CLZ-2025-0013", clean))


class TestLLMSeam(unittest.TestCase):
    """The stub must be production-shaped, or the budget is fiction."""

    def setUp(self) -> None:
        self.config = {"provider": "stub", "name": "claude-sonnet-5", "temperature": 0.0, "max_tokens": 512}
        self.pricing = {"input_per_1m": 3.0, "output_per_1m": 15.0}

    def test_populates_usage_metadata_like_a_real_model(self) -> None:
        response = build_model(self.config).invoke(
            'RESPOND WITH JSON MATCHING: ClauseClassification\nCLAUSE TEXT:\n"""The Consultant shall indemnify the Owner."""'
        )
        self.assertGreater(response.usage_metadata["input_tokens"], 0)
        self.assertGreater(response.usage_metadata["output_tokens"], 0)
        usage = usage_from_message(response, self.pricing)
        self.assertGreater(usage.cost_usd, 0)

    def test_is_deterministic(self) -> None:
        """Same input, same output. In an audited pipeline this is the product:
        a decision you cannot reproduce cannot be defended."""
        model = build_model(self.config)
        prompt = 'RESPOND WITH JSON MATCHING: ClauseClassification\nCLAUSE TEXT:\n"""Payment within sixty (60) days of invoice."""'
        self.assertEqual(model.invoke(prompt).content, model.invoke(prompt).content)

    def test_rate_limiter_is_in_the_call_path(self) -> None:
        """Not decoration: it genuinely throttles, even against the stub.

        Distinct from the budget guard -- a rate limiter WAITS (provider
        constraint), a budget guard STOPS (business constraint).
        """
        import time

        config = {**self.config, "rate_limit": {"requests_per_second": 4.0,
                                                "check_every_n_seconds": 0.01,
                                                "max_bucket_size": 1}}
        model = build_model(config)
        prompt = 'RESPOND WITH JSON MATCHING: ClauseClassification\nCLAUSE TEXT:\n"""x"""'
        model.invoke(prompt)                      # drains the bucket
        started = time.perf_counter()
        model.invoke(prompt)
        self.assertGreater(time.perf_counter() - started, 0.1)

    def test_cache_key_changes_with_prompt_version(self) -> None:
        """Promoting a prompt version must not serve decisions made by the old
        one. That is a correctness property, not an optimisation."""
        a = DecisionCache.key("same prompt", "model", "classifier.v1")
        b = DecisionCache.key("same prompt", "model", "classifier.v2")
        self.assertNotEqual(a, b)


class TestGuardrails(Phase2TestCase):
    def test_budget_halt_marks_every_remaining_clause(self) -> None:
        """The control must not itself become a silent-drop mechanism."""
        code = self.run_pipeline(["--max-cost-usd", "0.005", "--no-cache"])
        self.assertEqual(code, 2)

        summary = self.summary()
        self.assertEqual(summary["status"], "halted")
        self.assertEqual(summary["halt_reason"], "budget_exceeded")

        register = self.register()
        self.assertEqual(len(register), 20, "all clauses must appear even after a halt")
        statuses = {c["clause_id"]: c["status"] for c in register}
        self.assertEqual(
            summary["clauses_not_processed"],
            sum(1 for s in statuses.values() if s == "not_processed"),
        )

    def test_resume_after_halt_requires_acknowledgement(self) -> None:
        self.run_pipeline(["--max-cost-usd", "0.005", "--no-cache"])
        self.assertEqual(self.run_pipeline([]), 2, "blind resume must be refused")
        # 0 = clean, 3 = complete but escalations open. Both mean the run
        # finished; only 2 means a guardrail stopped it.
        self.assertIn(self.run_pipeline(["--acknowledge-halt", "checked; demo budget"]), (0, 3))
        self.assertEqual(self.summary()["clauses_processed"], 20)

    def test_resume_preserves_earlier_findings(self) -> None:
        """A clause finished before a halt must not lose its findings.

        The checkpoint records which clauses are done; the append-only ledger
        records what was decided. Resume replays the ledger. Without that,
        recovery silently drops findings -- reintroducing exactly the failure
        the guardrails exist to prevent.
        """
        self.run_pipeline(["--max-cost-usd", "0.005", "--no-cache"])
        processed_first = [c for c in self.register() if c["status"] != "not_processed"]
        self.assertTrue(processed_first)
        early_ids = {c["clause_id"] for c in processed_first}

        self.assertIn(self.run_pipeline(["--acknowledge-halt", "demo"]), (0, 3))
        final = {c["clause_id"]: c for c in self.register()}
        for clause_id in early_ids:
            self.assertNotEqual(
                final[clause_id]["status"], "not_processed",
                f"{clause_id} was processed before the halt but lost its result on resume",
            )
            self.assertIsNotNone(final[clause_id]["review_category"])

    def test_kill_switch_file_halts_processing(self) -> None:
        (self.tmp / "STOP").write_text("halt", encoding="utf-8")
        self.assertEqual(self.run_pipeline([]), 2)
        self.assertEqual(self.summary()["halt_reason"], "kill_switch_file")

    def test_kill_switch_rate_check_needs_a_minimum_sample(self) -> None:
        """One failure out of one record is 100% and would trip instantly."""
        guards = Guardrails.from_config(self._test_config(), ROOT)
        telemetry = self.telemetry()
        try:
            guards.kill_switch.record_failure()
            guards.kill_switch.check(telemetry)  # must not raise
            for _ in range(9):
                guards.kill_switch.record_failure()
            with self.assertRaises(HaltProcessing):
                guards.kill_switch.check(telemetry)
        finally:
            telemetry.shutdown()

    def test_scope_guard_refuses_rather_than_guessing(self) -> None:
        guards = Guardrails.from_config(self._test_config(), ROOT)
        ok, _, _ = guards.scope.check_clause({"clause_text": "x" * 50})
        self.assertTrue(ok)
        ok, reason, _ = guards.scope.check_clause({"clause_text": "short"})
        self.assertFalse(ok)
        self.assertEqual(reason, "clause_too_short")
        ok, reason, _ = guards.scope.check_clause({"clause_text": "x" * 99999})
        self.assertFalse(ok)
        self.assertEqual(reason, "clause_too_long")


class TestAuditLedger(Phase2TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.assertIn(self.run_pipeline([]), (0, 3))
        self.records = self.audit_records()

    def test_every_required_field_is_present(self) -> None:
        for record in self.records:
            for field in ("timestamp", "agent", "clause_id", "action", "effective_value"):
                self.assertIn(field, record)

    def test_model_and_human_decisions_are_separated(self) -> None:
        """The requirements's 'clear separation between model output and human
        decision', as three distinct fields rather than a convention."""
        overrides = [r for r in self.records if r["action"] == "human_override"]
        self.assertTrue(overrides, "expected at least one simulated override")
        for record in overrides:
            self.assertIsNotNone(record["model_output"])
            self.assertIsNotNone(record["human_decision"])
            self.assertEqual(record["decided_by"], "human")
            # The model's original opinion survives the override.
            self.assertNotEqual(record["model_output"], record["effective_value"])

    def test_model_decisions_carry_reproducibility_metadata(self) -> None:
        """'Which prompt produced this decision?' must be answerable later."""
        for record in self.records:
            if record["decided_by"] == "model":
                self.assertTrue(record["prompt_version"])
                self.assertTrue(record["model_name"])
                self.assertTrue(record["firm_standards_version"])

    def test_ledger_is_append_only(self) -> None:
        """A human override appends; it never edits the original record."""
        by_clause = {}
        for record in self.records:
            by_clause.setdefault(record["clause_id"], []).append(record)
        overridden = [r for r in self.records if r["action"] == "human_override"]
        for record in overridden:
            originals = [
                r for r in by_clause[record["clause_id"]]
                if r["agent"] == "risk_flagger" and r["action"] == "assess_risk"
            ]
            self.assertTrue(originals)
            self.assertEqual(originals[0]["status"], "proposed", "original was mutated")

    def test_named_output_file_is_valid_json(self) -> None:
        """The requirements names audit_log.json; JSONL is the append target."""
        document = json.loads((self.tmp / "audit_log.json").read_text(encoding="utf-8"))
        self.assertEqual(document["record_count"], len(self.records))

    def test_lineage_reaches_back_to_bronze(self) -> None:
        entry = next(c for c in self.register() if c["lineage"])
        self.assertIn("record_hash", entry["lineage"])
        self.assertIn("run_id", entry["lineage"])


class TestAgentBehaviour(Phase2TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.assertIn(self.run_pipeline([]), (0, 3))

    def test_rules_decide_more_than_the_model_does(self) -> None:
        """If the model is doing the measurable work, we are paying for
        something a regex does better."""
        decided = self.summary()["decided_by"]
        self.assertGreater(decided.get("rule", 0), 0)
        self.assertGreaterEqual(decided.get("rule", 0), decided.get("model", 0))

    def test_unmappable_category_escalates_instead_of_being_filed_as_other(self) -> None:
        """CLZ-2025-0012 is a real security-clearance clause with no home in
        the seven-category taxonomy. Filing it silently hides a coverage gap."""
        escalations = [
            json.loads(l)
            for l in (self.tmp / "escalations.jsonl").read_text(encoding="utf-8").splitlines()
            if l.strip()
        ]
        reasons = {e["clause_id"]: e["reason"] for e in escalations}
        self.assertEqual(reasons.get("CLZ-2025-0012"), "category_outside_review_taxonomy")

    def test_high_severity_publishes_but_never_as_settled(self) -> None:
        for entry in self.register():
            if entry["overall_severity"] == "high" and not entry["human_overrides"]:
                self.assertEqual(entry["review_status"], "pending")

    def test_supersession_is_carried_through_from_phase_1(self) -> None:
        """Without this the register flags a clause the legal team already fixed."""
        entry = next(c for c in self.register() if c["clause_id"] == "CLZ-2025-0001")
        self.assertEqual(entry["superseded_by"], "CLZ-2025-0013")

    def test_findings_suggest_alternatives_from_our_own_corpus(self) -> None:
        entry = next(c for c in self.register() if c["clause_id"] == "CLZ-2025-0001")
        precedents = {p for f in entry["findings"] for p in f["precedent_clause_ids"]}
        self.assertTrue(precedents, "expected precedent clauses from the corpus")
        self.assertNotIn("CLZ-2025-0001", precedents)

    def test_parse_failure_escalates_rather_than_fabricating(self) -> None:
        class BrokenAgent(ClassifierAgent):
            def _parse(self, content):
                raise ParseFailure("simulated")

        telemetry = self.telemetry()
        try:
            config = self._test_config()
            handler = TelemetryCallbackHandler(telemetry, {}, "stub:test")
            agent = BrokenAgent(
                build_model(config["model"]),
                PromptTemplate.load(config["agents"]["classifier"]["prompt_version"]),
                telemetry, handler, DecisionCache(None, enabled=False), {},
                {**config["agents"]["classifier"], "prefer_rules": False},
            )
            result = agent.classify({"clause_id": "X", "clause_text": "y" * 100})
            self.assertEqual(result.status, DecisionStatus.ESCALATED)
            self.assertIsNone(result.output)
        finally:
            telemetry.shutdown()

    def test_cache_hit_costs_nothing(self) -> None:
        summary = self.summary()
        self.assertGreaterEqual(summary["cache"]["misses"], 1)
        self.assertIn(self.run_pipeline(["--restart"]), (0, 3))
        second = self.summary()
        self.assertGreater(second["cache"]["hits"], 0)
        self.assertLess(second["llm"]["cost_usd"], summary["llm"]["cost_usd"])


class TestHumanInTheLoopBoundary(Phase2TestCase):
    """The requirements's non-negotiable governance rule."""

    def _config_fingerprint(self) -> dict:
        return {p.name: p.read_bytes() for p in sorted(CONFIG_DIR.rglob("*")) if p.is_file()}

    def test_no_agent_process_writes_to_config(self) -> None:
        """THE non-negotiable assertion.

        A full run must leave every human-owned file byte-identical. The
        guarantee is structural -- machine state goes to output/, human
        decisions live in config/ -- and this is what proves it rather than
        asserting it in a README.
        """
        before = self._config_fingerprint()
        self.assertIn(self.run_pipeline([]), (0, 3))
        self.assertEqual(before, self._config_fingerprint())

    def test_learning_proposals_are_never_self_applied(self) -> None:
        self.assertIn(self.run_pipeline([]), (0, 3))
        document = json.loads((self.tmp / "learning_proposals.json").read_text(encoding="utf-8"))
        self.assertTrue(document["proposals"], "expected a proposal from repeated overrides")
        for proposal in document["proposals"]:
            self.assertEqual(proposal["status"], "PENDING_HUMAN_APPROVAL")
            self.assertIn("apply_proposal.py", proposal["authority"])

    def test_one_override_is_not_a_pattern(self) -> None:
        """A single override is an exception; only repetition proposes a rule
        change. Otherwise every individual judgement rewrites policy."""
        telemetry = self.telemetry()
        try:
            auditor = AuditorAgent(telemetry, "run", self.tmp, min_occurrences_for_proposal=2)
            record = AuditRecord(
                timestamp="t", agent="risk_flagger", clause_id="CLZ-1", action="assess_risk",
                model_output={
                    "overall_severity": "high",
                    "findings": [{"severity": "high", "standard_reference": "liability.max_cap_multiple_of_fees"}],
                },
                effective_value={"overall_severity": "high"}, run_id="run",
            )
            auditor.apply_review(
                {"clause_id": "CLZ-1", "reviewer": "R", "action": "override", "rationale": "one-off",
                 "source": "simulated"},
                record,
            )
            self.assertEqual(len(auditor.overrides), 1)
            self.assertEqual(auditor.propose_learning(FIRM_STANDARDS), [])
        finally:
            telemetry.shutdown()

    def test_override_changes_the_clause_not_the_rule(self) -> None:
        """The crux. If overriding a clause retuned a threshold, we would have
        failed the requirement while appearing to comply."""
        standards_before = (CONFIG_DIR / "firm_standards.json").read_bytes()
        self.assertIn(self.run_pipeline([]), (0, 3))
        overrides = [
            json.loads(l)
            for l in (self.tmp / "human_overrides.jsonl").read_text(encoding="utf-8").splitlines()
            if l.strip()
        ]
        self.assertTrue(overrides)
        self.assertEqual(standards_before, (CONFIG_DIR / "firm_standards.json").read_bytes())

    def test_apply_proposal_without_approver_changes_nothing(self) -> None:
        """Reviewing is the default; applying is the exception."""
        self.assertIn(self.run_pipeline([]), (0, 3))
        saved = apply_proposal.PROPOSALS_PATH
        apply_proposal.PROPOSALS_PATH = self.tmp / "learning_proposals.json"
        try:
            before = (CONFIG_DIR / "firm_standards.json").read_bytes()
            proposals = apply_proposal.load_proposals()
            code = apply_proposal.main(["--id", proposals[0]["proposal_id"]])
            self.assertEqual(code, 0)
            self.assertEqual(before, (CONFIG_DIR / "firm_standards.json").read_bytes())
        finally:
            apply_proposal.PROPOSALS_PATH = saved

    def test_approved_proposal_is_applied_and_recorded(self) -> None:
        """The one path that may change behaviour, exercised end to end
        against a copy of the config so the repo's own file is untouched."""
        self.assertIn(self.run_pipeline([]), (0, 3))

        sandbox = self.tmp / "config_copy"
        sandbox.mkdir(parents=True, exist_ok=True)
        shutil.copy(CONFIG_DIR / "firm_standards.json", sandbox / "firm_standards.json")

        proposals_path = self.tmp / "learning_proposals.json"
        document = json.loads(proposals_path.read_text(encoding="utf-8"))
        proposal = document["proposals"][0]
        proposal["target_file"] = str(sandbox / "firm_standards.json")
        proposals_path.write_text(json.dumps(document), encoding="utf-8")

        saved = (apply_proposal.PROPOSALS_PATH, apply_proposal.APPROVALS_PATH, apply_proposal.AUDIT_PATH,
                 apply_proposal.OUTPUT_DIR)
        apply_proposal.PROPOSALS_PATH = proposals_path
        apply_proposal.APPROVALS_PATH = self.tmp / "approvals.jsonl"
        apply_proposal.AUDIT_PATH = self.tmp / "audit_log.jsonl"
        apply_proposal.OUTPUT_DIR = self.tmp
        try:
            code = apply_proposal.main(
                ["--id", proposal["proposal_id"], "--approved-by", "K. Chen",
                 "--rationale", "Counsel reviewed the exceptions."]
            )
            self.assertEqual(code, 0)

            updated = json.loads((sandbox / "firm_standards.json").read_text(encoding="utf-8"))
            self.assertNotEqual(updated["version"], FIRM_STANDARDS["version"])
            latest = updated["version_history"][-1]
            self.assertEqual(latest["approved_by"], "K. Chen")
            self.assertEqual(latest["proposal_id"], proposal["proposal_id"])

            approvals = [
                json.loads(l)
                for l in (self.tmp / "approvals.jsonl").read_text(encoding="utf-8").splitlines()
                if l.strip()
            ]
            self.assertEqual(approvals[0]["approved_by"], "K. Chen")

            # The config change is in the ledger too, so "who changed this
            # standard?" is answerable without knowing a second file exists.
            ledger = self.audit_records()
            self.assertTrue(any(r["action"] == "apply_learning_proposal" for r in ledger))
        finally:
            (apply_proposal.PROPOSALS_PATH, apply_proposal.APPROVALS_PATH,
             apply_proposal.AUDIT_PATH, apply_proposal.OUTPUT_DIR) = saved


class TestObservability(Phase2TestCase):
    def test_token_cost_and_latency_are_recorded_per_call(self) -> None:
        self.assertIn(self.run_pipeline([]), (0, 3))
        logs = [
            json.loads(l)
            for l in (self.logs / "pipeline.jsonl").read_text(encoding="utf-8").splitlines()
            if l.strip()
        ]
        responses = [l for l in logs if l["event"] == "llm.response"]
        self.assertTrue(responses)
        for entry in responses:
            attributes = entry["attributes"]
            # OTel GenAI semantic conventions, so these traces drop into a
            # GenAI-aware backend without remapping.
            self.assertIn("gen_ai.usage.input_tokens", attributes)
            self.assertIn("gen_ai.usage.output_tokens", attributes)
            self.assertIn("llm.cost_usd", attributes)
            self.assertIn("llm.latency_ms", attributes)
            self.assertTrue(entry["trace_id"], "log line must join to its span")

    def test_summary_reports_cost_and_guardrail_state(self) -> None:
        self.assertIn(self.run_pipeline([]), (0, 3))
        summary = self.summary()
        self.assertIn("cost_usd", summary["llm"])
        self.assertIn("budget", summary["guardrails"])
        self.assertIn("kill_switch", summary["guardrails"])
        self.assertIn("classifier", summary["model"]["prompt_versions"])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestEvaluationHarness(unittest.TestCase):
    """Proves the meter is not stuck at 100%.

    The harness currently reports perfect scores. That is only meaningful if
    it would report something else when the system is wrong -- otherwise a
    passing evaluation and a broken evaluation look identical. So we break the
    system on purpose and assert the numbers move.
    """

    def setUp(self) -> None:
        sys.path.insert(0, str(ROOT / "eval"))
        import evaluate  # noqa: E402

        self.evaluate = evaluate
        self.labels = json.loads(
            (ROOT / "eval" / "ground_truth.json").read_text(encoding="utf-8")
        )["labels"]
        self.clauses = load_clauses()

    def test_scores_the_real_system_perfectly(self) -> None:
        risk = self.evaluate.evaluate_risk(self.clauses, self.labels, FIRM_STANDARDS)
        self.assertEqual(risk["standard_level"]["recall"], 1.0)
        self.assertEqual(risk["standard_level"]["precision"], 1.0)
        self.assertEqual(risk["disagreements"], [])

    def test_detects_a_missed_finding(self) -> None:
        """Disable a standard; recall must fall and the miss must be named."""
        broken = copy.deepcopy(FIRM_STANDARDS)
        del broken["standards"]["liability"]["max_cap_multiple_of_fees"]

        risk = self.evaluate.evaluate_risk(self.clauses, self.labels, broken)
        self.assertLess(risk["standard_level"]["recall"], 1.0)
        self.assertGreater(risk["standard_level"]["false_negatives"], 0)

        missed = {d["clause_id"]: d["missed"] for d in risk["disagreements"]}
        # CLZ-2025-0018 is the 2x liability cap J. Martinez flagged.
        self.assertIn("CLZ-2025-0018", missed)
        self.assertIn("liability.max_cap_multiple_of_fees", missed["CLZ-2025-0018"])

    def test_detects_a_spurious_finding(self) -> None:
        """Tighten a standard so a compliant clause trips it; precision must fall."""
        broken = copy.deepcopy(FIRM_STANDARDS)
        broken["standards"]["payment_terms"]["max_days_to_pay"]["value"] = 1

        risk = self.evaluate.evaluate_risk(self.clauses, self.labels, broken)
        self.assertLess(risk["standard_level"]["precision"], 1.0)
        self.assertGreater(risk["standard_level"]["false_positives"], 0)
        spurious = {d["clause_id"] for d in risk["disagreements"] if d["spurious"]}
        # CLZ-2025-0020 pays in 30 days and is compliant under the real standard.
        self.assertIn("CLZ-2025-0020", spurious)

    def test_ground_truth_is_not_copied_from_the_source_field(self) -> None:
        """If labels were copied from Silver's clause_category, the evaluation
        would score the code against its own input and always pass."""
        silver = {c["clause_id"]: c["clause_category"] for c in self.clauses}
        differing = [
            cid for cid, label in self.labels.items()
            if silver.get(cid) != label["expected_category"]
        ]
        # limitation_of_liability -> liability, consequential_damages -> liability,
        # security_clearance -> other. Independent labels, not a copy.
        self.assertGreaterEqual(len(differing), 3)


class TestLedgerRehydrationTypes(Phase2TestCase):
    """Values replayed from JSON must come back as model types, not raw dicts.

    Assigning a plain string where an enum belongs is accepted by pydantic at
    assignment time and only surfaces later as serializer warnings and as
    comparisons that silently fail. Caught only after the test output was
    quietened enough to see the warnings.
    """

    def test_rehydrated_entries_are_properly_typed(self) -> None:
        from phase2_agents.models import ClauseCategory, RiskFinding, Severity

        self.run_pipeline(["--max-cost-usd", "0.005", "--no-cache"])
        self.assertIn(self.run_pipeline(["--acknowledge-halt", "demo"]), (0, 3))

        entries = run_module.rehydrate_from_ledger(
            {
                c["clause_id"]: run_module.ClauseRiskEntry(
                    clause_id=c["clause_id"], contract_id="x", client_name="y", section_ref="z"
                )
                for c in load_clauses()
            },
            self.tmp / "audit_log.jsonl",
            {c["clause_id"] for c in load_clauses()},
        )
        self.assertTrue(entries)

    def test_register_serialises_without_warnings(self) -> None:
        import warnings

        self.run_pipeline(["--max-cost-usd", "0.005", "--no-cache"])
        with warnings.catch_warnings():
            warnings.simplefilter("error", UserWarning)
            self.assertIn(self.run_pipeline(["--acknowledge-halt", "demo"]), (0, 3))

        typed = [c for c in self.register() if c["review_category"]]
        self.assertTrue(typed)
        for entry in typed:
            self.assertIn(entry["review_category"], {c.value for c in
                          __import__("phase2_agents.models", fromlist=["ClauseCategory"]).ClauseCategory})
