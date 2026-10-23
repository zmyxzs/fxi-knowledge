import copy
import json
import sqlite3
import unittest

from fxi_novel.knowledge.parse_candidate_qualification import (
    build_candidate_qualification, receipt_extraction_ready, extraction_ready_sql,
    qualified_candidate_sql,
)
from fxi_novel.knowledge.graph_parser_batch_stage_contracts import parse_item_extraction_ready


class CandidateQualificationTests(unittest.TestCase):
    def receipt(self):
        qualification = build_candidate_qualification(
            receipt_id="receipt", generation=2, source_id="source", snapshot_id="snapshot",
            candidate_ids=["good"], checkpoints={
                "group-1": {"status": "failed", "candidate_ids": ["good"]}},
            coverage_completed=0, coverage_total=1,
        )
        return dict(id="receipt", generation=2, source_id="source", snapshot_id="snapshot",
                    candidate_count=1, status="completed", review_status="incomplete",
                    coverage_completed=0, coverage_total=1,
                    review_diagnostic_json=json.dumps({"candidate_qualification": qualification}))

    def sql_ready(self, receipt, candidate="good"):
        db = sqlite3.connect(":memory:")
        columns = ",".join(f"? AS {key}" for key in receipt)
        result = db.execute(
            f"WITH receipt AS (SELECT {columns}), item AS (SELECT ? AS id) "
            f"SELECT {extraction_ready_sql('receipt')}, {qualified_candidate_sql('receipt', 'item')} "
            "FROM receipt, item", [*receipt.values(), candidate],
        ).fetchone()
        db.close()
        return tuple(bool(value) for value in result)

    def test_preserved_rows_can_proceed_but_unknown_rows_cannot(self):
        receipt = self.receipt()
        self.assertTrue(receipt_extraction_ready(receipt))
        self.assertEqual(self.sql_ready(receipt), (True, True))
        self.assertEqual(self.sql_ready(receipt, "bad"), (True, False))
        manifest = json.loads(receipt["review_diagnostic_json"])["candidate_qualification"]
        self.assertFalse(manifest["coverage_complete"])
        self.assertEqual(manifest["unresolved_group_refs"], ["group-1"])

    def test_legacy_incomplete_and_wrong_pins_are_denied(self):
        for field, value in [("review_diagnostic_json", "{}"), ("snapshot_id", "new"),
                             ("generation", 3), ("candidate_count", 2), ("status", "running")]:
            with self.subTest(field=field):
                receipt = self.receipt()
                receipt[field] = value
                self.assertFalse(receipt_extraction_ready(receipt))
                self.assertEqual(self.sql_ready(receipt), (False, False))

    def test_malformed_manifest_has_python_sql_parity(self):
        for field, value in [("candidate_ids", ["good", "good"]),
                             ("candidate_ids", [1]), ("candidate_ids", [""]),
                             ("candidate_ids", "good"), ("version", True),
                             ("generation", True)]:
            with self.subTest(field=field, value=value):
                receipt = self.receipt()
                diagnostic = json.loads(receipt["review_diagnostic_json"])
                diagnostic["candidate_qualification"][field] = value
                if isinstance(value, list):
                    receipt["candidate_count"] = len(value)
                receipt["review_diagnostic_json"] = json.dumps(diagnostic)
                self.assertFalse(receipt_extraction_ready(receipt))
                self.assertEqual(self.sql_ready(receipt), (False, False))

    def test_complete_legacy_receipt_remains_accepted(self):
        receipt = self.receipt()
        receipt.update(review_status="passed", coverage_completed=1, review_diagnostic_json="{}")
        self.assertTrue(receipt_extraction_ready(receipt))
        self.assertEqual(self.sql_ready(receipt), (True, True))

    def test_batch_gate_requires_incomplete_status_for_partial_manifest(self):
        receipt = self.receipt()
        item = copy.deepcopy(receipt)
        item.update(receipt_id=item.pop("id"), extraction_status="completed",
                    candidate_qualification=json.loads(item["review_diagnostic_json"])["candidate_qualification"])
        self.assertTrue(parse_item_extraction_ready(item))
        item["review_status"] = "pending"
        self.assertFalse(parse_item_extraction_ready(item))

    def test_builder_rejects_unrepresented_or_duplicate_rows(self):
        for ids in [["missing"], ["good", "good"]]:
            with self.assertRaises(ValueError):
                build_candidate_qualification(
                    receipt_id="r", generation=1, source_id="s", snapshot_id="p",
                    candidate_ids=ids, checkpoints={"g": {"candidate_ids": ["good"]}},
                    coverage_completed=0, coverage_total=1,
                )
