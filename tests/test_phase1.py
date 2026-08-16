"""
tests/test_phase1.py
====================
One test per Phase 1 requirement, plus two that guard the boundaries this
design depends on.

Uses `unittest` from the standard library rather than pytest, on purpose: the
repo must be clonable and runnable as-is, and this keeps the test suite
runnable with zero installs.

    python -m unittest discover -s tests -v

Each test runs against a temporary output directory, so running the suite
never disturbs the committed outputs in phase1_ingestion/output/.
"""

from __future__ import annotations

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
from phase1_ingestion import drift, ingest, normalize, supersede  # noqa: E402

DATA = ROOT / "data"
BATCH_1 = DATA / "clauses_batch_1.json"
BATCH_2 = DATA / "clauses_batch_2.json"
FALLBACK = DATA / "clauses_ingested_fallback.json"


class Phase1TestCase(unittest.TestCase):
    """Redirects every module's output paths into a throwaway directory."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ci_phase1_"))
        self.logs = self.tmp / "logs"

        self._saved = {}
        for module, names in (
            (ingest, ["OUTPUT_DIR", "BRONZE_ROOT", "DRIFT_DIR", "LOG_DIR", "REGISTRY_PATH", "STOP_FILE"]),
            (normalize, ["OUTPUT_DIR", "BRONZE_ROOT", "SILVER_DIR", "LOG_DIR"]),
            (supersede, ["OUTPUT_DIR", "SILVER_PATH", "PROPOSALS_DIR", "LOG_DIR"]),
        ):
            for name in names:
                self._saved[(module.__name__, name)] = getattr(module, name)

        ingest.OUTPUT_DIR = self.tmp
        ingest.BRONZE_ROOT = self.tmp / "bronze"
        ingest.DRIFT_DIR = self.tmp / "drift"
        ingest.LOG_DIR = self.logs
        ingest.REGISTRY_PATH = self.tmp / "drift" / "schema_registry.json"
        ingest.STOP_FILE = self.tmp / "STOP"

        normalize.OUTPUT_DIR = self.tmp
        normalize.BRONZE_ROOT = self.tmp / "bronze"
        normalize.SILVER_DIR = self.tmp / "silver"
        normalize.LOG_DIR = self.logs

        supersede.OUTPUT_DIR = self.tmp
        supersede.SILVER_PATH = self.tmp / "silver" / "clauses.jsonl"
        supersede.PROPOSALS_DIR = self.tmp / "proposals"
        supersede.LOG_DIR = self.logs

    def tearDown(self) -> None:
        for (module_name, name), value in self._saved.items():
            module = {m.__name__: m for m in (ingest, normalize, supersede)}[module_name]
            setattr(module, name, value)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers -----------------------------------------------------------
    def telemetry(self, name: str = "test") -> Telemetry:
        return Telemetry(name, self.logs)

    def land(self, source: Path, **kwargs) -> dict:
        tel = self.telemetry()
        try:
            return ingest.ingest(source, telemetry=tel, **kwargs)
        finally:
            tel.shutdown()

    def bronze_records(self) -> list:
        records = []
        for part in sorted(ingest.BRONZE_ROOT.glob("ingest_date=*/run_id=*/part-0000.jsonl")):
            with part.open(encoding="utf-8") as fh:
                records += [json.loads(line) for line in fh if line.strip()]
        return records


class TestBronzePreservesRawData(Phase1TestCase):
    """P1-R1: land raw, partitioned by ingestion timestamp, unmodified."""

    def test_payload_round_trips_byte_equivalent(self) -> None:
        source_clauses = json.loads(BATCH_1.read_text(encoding="utf-8"))["clauses"]
        self.land(BATCH_1)

        landed = self.bronze_records()
        self.assertEqual(len(landed), len(source_clauses))
        for original, envelope in zip(source_clauses, landed):
            # Not just "equal" -- identical key order and content, i.e. the
            # payload was never touched on the way in.
            self.assertEqual(
                json.dumps(original, sort_keys=True),
                json.dumps(envelope["payload"], sort_keys=True),
            )

    def test_partitioned_by_ingestion_date(self) -> None:
        self.land(BATCH_1)
        parts = list(ingest.BRONZE_ROOT.glob("ingest_date=*/run_id=*/part-0000.jsonl"))
        self.assertEqual(len(parts), 1)
        self.assertRegex(parts[0].parent.parent.name, r"^ingest_date=\d{4}-\d{2}-\d{2}$")

    def test_raw_snapshot_matches_source_bytes(self) -> None:
        self.land(BATCH_1)
        snapshot = next(ingest.BRONZE_ROOT.glob("ingest_date=*/run_id=*/_source_snapshot.json"))
        self.assertEqual(snapshot.read_bytes(), BATCH_1.read_bytes())


class TestSchemaDriftIsSurfaced(Phase1TestCase):
    """P1-R2: detect drift, make it visible, and never drop the changed data."""

    def setUp(self) -> None:
        super().setUp()
        self.land(BATCH_1)
        self.result = self.land(BATCH_2)
        self.report = self.result["drift_report"]
        self.kinds = {(e["kind"], e["path"]): e for e in self.report["events"]}

    def test_detects_the_rename(self) -> None:
        event = self.kinds[("SUSPECTED_RENAME", "clause_type -> category")]
        self.assertEqual(event["severity"], "NEEDS_HUMAN_CONFIRMATION")
        self.assertGreaterEqual(event["confidence"], 0.5)

    def test_detects_the_removal_as_breaking(self) -> None:
        self.assertEqual(self.kinds[("FIELD_REMOVED", "modified_by")]["severity"], "BREAKING")

    def test_detects_the_new_nested_object(self) -> None:
        """One event for the subtree root, with the descendants as evidence.

        Reported as one decision, not eight, so the "action required" list
        stays short enough to actually be read.
        """
        event = self.kinds[("FIELD_ADDED", "review_history")]
        nested = event["evidence"]["nested_paths"]
        for path in (
            "review_history.reviews",
            "review_history.review_count",
            "review_history.last_review_date",
            "review_history.reviews[].reviewer",
            "review_history.reviews[].action",
        ):
            self.assertIn(path, nested)
        # ...and the descendants do not each get their own event.
        self.assertNotIn(("FIELD_ADDED", "review_history.reviews"), self.kinds)

    def test_detects_new_enum_value(self) -> None:
        event = self.kinds[("ENUM_VALUE_ADDED", "status")]
        self.assertIn("under_review", event["evidence"]["new_values"])

    def test_does_not_report_identifiers_as_enum_drift(self) -> None:
        # New clause_ids and clause texts are data, not schema change. If these
        # ever start firing, the drift report becomes noise and gets ignored.
        for path in ("clause_id", "clause_text", "contract_id", "section_ref"):
            self.assertNotIn(("ENUM_VALUE_ADDED", path), self.kinds)

    def test_drift_never_drops_records(self) -> None:
        """The requirement that matters most: changed data still lands."""
        self.assertTrue(self.report["drift_detected"])
        self.assertEqual(len(self.bronze_records()), 20)  # 12 + 8, none rejected

    def test_report_files_are_written_for_humans_and_machines(self) -> None:
        self.assertTrue((ingest.DRIFT_DIR / "drift_report.json").exists())
        markdown = (ingest.DRIFT_DIR / "drift_report.md").read_text(encoding="utf-8")
        self.assertIn("clause_type -> category", markdown)
        self.assertIn("Action required", markdown)


class TestResumability(Phase1TestCase):
    """P1-R3: fail mid-file, resume without reprocessing completed records."""

    def test_resumes_after_crash_without_reprocessing(self) -> None:
        with self.assertRaises(ingest.SimulatedCrash):
            self.land(BATCH_2, simulate_crash_after=5)

        run_dir = next(ingest.BRONZE_ROOT.glob("ingest_date=*/run_id=*"))
        checkpoint = json.loads((run_dir / "_checkpoint.json").read_text(encoding="utf-8"))
        self.assertEqual(checkpoint["last_committed_index"], 4)
        self.assertEqual(checkpoint["status"], "in_progress")
        self.assertEqual(len(self.bronze_records()), 5)

        self.land(BATCH_2)  # same command; resumes

        records = self.bronze_records()
        self.assertEqual(len(records), 8)
        self.assertEqual(len({r["_bronze"]["record_hash"] for r in records}), 8)
        # The first five were written once and never touched again.
        self.assertEqual([r["_bronze"]["record_index"] for r in records], list(range(8)))

    def test_crash_inside_commit_window_replays_and_dedupes(self) -> None:
        """The at-least-once guarantee, exercised rather than asserted."""
        with self.assertRaises(ingest.SimulatedCrash):
            self.land(BATCH_2, simulate_crash_after=5, crash_in_commit_window=True)
        self.land(BATCH_2)

        bronze = self.bronze_records()
        self.assertEqual(len(bronze), 9, "expected one replayed record in Bronze")
        self.assertEqual(len({r["_bronze"]["record_hash"] for r in bronze}), 8)

        tel = self.telemetry("normalize")
        try:
            report = normalize.normalize(tel)
        finally:
            tel.shutdown()
        self.assertEqual(report["duplicates_dropped"], 1)
        self.assertEqual(report["silver_records"], 8)

    def test_rerunning_a_complete_run_is_a_noop(self) -> None:
        self.land(BATCH_1)
        before = len(self.bronze_records())
        result = self.land(BATCH_1)
        self.assertEqual(result["status"], "already_complete")
        self.assertEqual(len(self.bronze_records()), before)

    def test_stop_file_pauses_on_a_record_boundary(self) -> None:
        ingest.STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
        ingest.STOP_FILE.write_text("pause", encoding="utf-8")
        with self.assertRaises(ingest.PausedByOperator):
            self.land(BATCH_1)

        run_dir = next(ingest.BRONZE_ROOT.glob("ingest_date=*/run_id=*"))
        checkpoint = json.loads((run_dir / "_checkpoint.json").read_text(encoding="utf-8"))
        self.assertEqual(checkpoint["status"], "paused")

        ingest.STOP_FILE.unlink()
        self.land(BATCH_1)
        self.assertEqual(len(self.bronze_records()), 12)


class TestSilverContract(Phase1TestCase):
    """P1-R4: the published shape is what downstream actually receives."""

    def test_silver_matches_the_provided_reference_dataset(self) -> None:
        """Upstream ships a pre-normalized reference dataset; ours must equal it.

        A free correctness check on the whole Bronze -> Silver path.
        """
        self.land(BATCH_1)
        self.land(BATCH_2)
        tel = self.telemetry("normalize")
        try:
            normalize.normalize(tel)
        finally:
            tel.shutdown()

        produced = [
            json.loads(line)
            for line in (normalize.SILVER_DIR / "clauses.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        contract_only = [{k: v for k, v in r.items() if not k.startswith("_")} for r in produced]
        expected = sorted(
            json.loads(FALLBACK.read_text(encoding="utf-8"))["clauses"],
            key=lambda r: r["clause_id"],
        )
        self.assertEqual(contract_only, expected)

    def test_unconfirmed_alias_degrades_visibly_not_silently(self) -> None:
        """With no human-confirmed alias, the field is null AND flagged."""
        envelope = {
            "_bronze": {
                "run_id": "r",
                "source_file": "f.json",
                "source_api_version": "2.3",
                "record_index": 0,
                "record_hash": "sha256:x",
                "ingested_at": "2026-01-01T00:00:00Z",
            },
            "payload": {"clause_id": "CLZ-1", "category": "insurance", "clause_text": "..."},
        }
        record = normalize.to_silver(envelope, aliases={})  # nothing confirmed
        self.assertIsNone(record["clause_category"])
        self.assertEqual(record["_unmapped_fields"], ["category"])

    def test_lineage_points_back_to_the_exact_bronze_record(self) -> None:
        self.land(BATCH_1)
        tel = self.telemetry("normalize")
        try:
            normalize.normalize(tel)
        finally:
            tel.shutdown()
        record = json.loads(
            (normalize.SILVER_DIR / "clauses.jsonl").read_text(encoding="utf-8").splitlines()[0]
        )
        bronze_hashes = {r["_bronze"]["record_hash"] for r in self.bronze_records()}
        self.assertIn(record["_lineage"]["record_hash"], bronze_hashes)


class TestHumanApprovalBoundary(Phase1TestCase):
    """The boundary the whole design leans on, in Phase 1 and again in Phase 2.

    The machine may propose. It may not decide, and it may not write to the
    files that encode human decisions.
    """

    CONFIG_DIR = ROOT / "phase1_ingestion" / "config"

    def _config_fingerprint(self) -> dict:
        return {
            path.name: path.read_bytes()
            for path in sorted(self.CONFIG_DIR.glob("*"))
            if path.is_file()
        }

    def test_pipeline_never_writes_to_the_human_owned_config(self) -> None:
        before = self._config_fingerprint()
        self.land(BATCH_1)
        self.land(BATCH_2)
        tel = self.telemetry("normalize")
        try:
            normalize.normalize(tel)
        finally:
            tel.shutdown()
        self.assertEqual(before, self._config_fingerprint())

    def test_rename_is_proposed_not_applied(self) -> None:
        self.land(BATCH_1)
        report = self.land(BATCH_2)["drift_report"]
        rename = next(e for e in report["events"] if e["kind"] == "SUSPECTED_RENAME")
        self.assertEqual(rename["severity"], "NEEDS_HUMAN_CONFIRMATION")
        self.assertIn("schema_aliases.json", rename["action_required"])
        self.assertTrue(report["requires_human_action"])

    def test_supersession_proposals_are_advisory_only(self) -> None:
        self.land(BATCH_1)
        self.land(BATCH_2)
        tel = self.telemetry("normalize")
        try:
            normalize.normalize(tel)
        finally:
            tel.shutdown()

        silver_before = supersede.SILVER_PATH.read_bytes()
        proposals = supersede.analyse(
            [json.loads(line) for line in silver_before.decode("utf-8").splitlines() if line.strip()]
        )
        self.assertEqual(supersede.SILVER_PATH.read_bytes(), silver_before, "Silver was modified")

        by_kind = {p["kind"]: p for p in proposals}
        self.assertEqual(
            by_kind["SUPERSESSION_CANDIDATE"]["superseded_clause_id"], "CLZ-2025-0001"
        )
        self.assertEqual(
            by_kind["SUPERSESSION_CANDIDATE"]["superseding_clause_id"], "CLZ-2025-0013"
        )
        self.assertEqual(by_kind["AMENDS_UNKNOWN_CLAUSE"]["clause_id"], "CLZ-2025-0017")
        self.assertTrue(all(p["status"] == "PENDING_HUMAN_REVIEW" for p in proposals))


class TestObservability(Phase1TestCase):
    """How on-call knows the system is healthy. This is the answer."""

    def test_emits_correlated_traces_logs_and_metrics(self) -> None:
        tel = self.telemetry("obs")
        try:
            with tel.span("ingest.run"):
                ingest.ingest(BATCH_1, telemetry=tel)
        finally:
            tel.shutdown()

        spans = [json.loads(l) for l in (self.logs / "traces.jsonl").read_text(encoding="utf-8").splitlines()]
        logs = [json.loads(l) for l in (self.logs / "pipeline.jsonl").read_text(encoding="utf-8").splitlines()]
        metrics = json.loads((self.logs / "metrics.jsonl").read_text(encoding="utf-8").splitlines()[-1])

        self.assertTrue(any(s["name"] == "ingest.land_records" for s in spans))
        # A log line must be joinable to the span that emitted it.
        committed = [l for l in logs if l["event"] == "record.committed"]
        self.assertEqual(len(committed), 12)
        span_ids = {s["span_id"] for s in spans}
        self.assertTrue(all(l["span_id"] in span_ids for l in committed))
        self.assertEqual(metrics["counters"]["ingest.records_landed_total"], 12)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestSilverSchemaIsEnforced(unittest.TestCase):
    """`silver_schema.json` must be a contract, not decoration.

    A published schema that no code reads is worse than no schema: consumers
    trust it, and nothing stops the producer drifting away from it. This
    validates the committed Silver dataset against it directly.

    Deliberately a small hand-rolled validator rather than the `jsonschema`
    package: Phase 1 is dependency-free by design, and the subset we actually
    use (required, type, additionalProperties, pattern) is a dozen lines.
    """

    SCHEMA = ROOT / "phase1_ingestion" / "silver_schema.json"
    SILVER = ROOT / "phase1_ingestion" / "output" / "silver" / "clauses.jsonl"

    JSON_TYPES = {
        "string": str, "integer": int, "number": (int, float),
        "boolean": bool, "object": dict, "array": list,
    }

    def setUp(self) -> None:
        if not self.SILVER.exists():  # pragma: no cover
            self.skipTest("Silver output missing; run: python run.py phase1")
        self.schema = json.loads(self.SCHEMA.read_text(encoding="utf-8"))
        self.records = [
            json.loads(line)
            for line in self.SILVER.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def _check(self, value, spec, path: str) -> None:
        declared = spec.get("type")
        if declared is not None:
            allowed = declared if isinstance(declared, list) else [declared]
            if value is None:
                self.assertIn("null", allowed, f"{path}: null but schema says {allowed}")
                return
            matches = tuple(self.JSON_TYPES[t] for t in allowed if t in self.JSON_TYPES)
            # bool is a subclass of int in Python; JSON Schema treats them apart.
            if isinstance(value, bool) and "boolean" not in allowed:
                self.fail(f"{path}: boolean but schema says {allowed}")
            self.assertIsInstance(value, matches, f"{path}: {type(value).__name__} not in {allowed}")

        if spec.get("pattern") and isinstance(value, str):
            self.assertRegex(value, spec["pattern"], f"{path} fails pattern")

        if isinstance(value, dict) and spec.get("properties"):
            for field in spec.get("required", []):
                self.assertIn(field, value, f"{path}: missing required field {field!r}")
            if spec.get("additionalProperties") is False:
                extra = set(value) - set(spec["properties"])
                self.assertFalse(extra, f"{path}: undeclared field(s) {sorted(extra)}")
            for field, sub in spec["properties"].items():
                if field in value:
                    self._check(value[field], sub, f"{path}.{field}")

        if isinstance(value, list) and spec.get("items"):
            for index, item in enumerate(value):
                self._check(item, spec["items"], f"{path}[{index}]")

    def test_every_silver_record_conforms(self) -> None:
        self.assertEqual(len(self.records), 20)
        for record in self.records:
            self._check(record, self.schema, record.get("clause_id", "?"))

    def test_the_validator_rejects_a_bad_record(self) -> None:
        """Prove the check can fail -- otherwise it proves nothing."""
        broken = dict(self.records[0])
        broken["clause_id"] = "NOT-A-CLAUSE-ID"
        with self.assertRaises(AssertionError):
            self._check(broken, self.schema, "broken")

        missing = {k: v for k, v in self.records[0].items() if k != "clause_category"}
        with self.assertRaises(AssertionError):
            self._check(missing, self.schema, "missing")

    def test_schema_matches_the_prose_contract(self) -> None:
        """The 13 contract fields in data_contract.md are the schema's."""
        declared = {f for f in self.schema["required"] if not f.startswith("_")}
        self.assertEqual(len(declared), 13)
        self.assertIn("clause_category", declared)
        self.assertIn("modified_by", declared)
        self.assertIn("review_history", declared)
