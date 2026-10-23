from __future__ import annotations

import json
import sqlite3
import unittest

from fxi_novel.knowledge.graph_parser_support import IDENTITY_RESOLUTION_VERSION
from fxi_novel.knowledge.graph_parser_identity_helpers import (
    _identity_authorized_projection_updates,
)
from fxi_novel.knowledge.models_base import knowledge_graph_identity_source_content_hash
from fxi_novel.knowledge.repository_parse_resolution_support import (
    _identity_attempt_supports_transition,
    _identity_fact_transition_supported,
    _identity_group_id,
)
from fxi_novel.knowledge.repository_parse_batch_resolution_gate import (
    _source_resolution_authorization,
)
from fxi_novel.knowledge.story_coordinates import NarrativePosition


def _position(source_id: str, chapter: int) -> dict[str, object]:
    return NarrativePosition(
        space="learning",
        work_id="identity-work",
        branch_id=None,
        manifest_revision=1,
        manifest_hash="a" * 64,
        manifest_coordinate_status="verified",
        source_id=source_id,
        snapshot_id=f"snapshot-{source_id}",
        content_hash="b" * 64,
        chapter_title=f"第{chapter}章",
        narrative_ordinal=chapter,
        offset=0,
        coordinate_basis="verified",
    ).model_dump(mode="json")


class IdentityProjectionAuthorizationTests(unittest.TestCase):
    def test_selected_identity_run_keeps_prior_changes_read_only_and_pins_writes(self) -> None:
        rows = {
            "prior-entity": {"version_id": "prior-v2", "selected": False},
            "prior-fact": {"version_id": "prior-fact-v1", "selected": False},
            "current-entity": {"version_id": "current-v4", "selected": True},
        }
        updates = {
            item_id: {"identity_group_id": "group"}
            for item_id in rows
        }
        refs = {
            "prior-entity": {"role": "prior_published", "version_id": "prior-v2"},
            "prior-fact": {"role": "prior_published", "version_id": "prior-fact-v1"},
            "current-entity": {"role": "target", "version_id": "current-v4"},
        }

        writable = _identity_authorized_projection_updates(
            updates,
            row_by_id=rows,
            audit_refs_by_id=refs,
            selected_source_scope=True,
            is_target=lambda row: bool(row["selected"]),
        )
        self.assertEqual(set(writable), {"current-entity"})

        stale_target_refs = {
            **refs,
            "current-entity": {"role": "target", "version_id": "current-v3"},
        }
        with self.assertRaisesRegex(ValueError, "exact target source-version receipt"):
            _identity_authorized_projection_updates(
                updates,
                row_by_id=rows,
                audit_refs_by_id=stale_target_refs,
                selected_source_scope=True,
                is_target=lambda row: bool(row["selected"]),
            )

        # A non-batch directory reconciliation has no selected-source filter;
        # prior rows still cannot be written using context-only audit refs.
        with self.assertRaisesRegex(ValueError, "exact target source-version receipt"):
            _identity_authorized_projection_updates(
                updates,
                row_by_id=rows,
                audit_refs_by_id=refs,
                selected_source_scope=False,
                is_target=lambda row: bool(row["selected"]),
            )

    def test_resolution_gate_accepts_only_exactly_qualified_partial_receipt_candidates(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        connection.execute(
            "CREATE TABLE knowledge_version (id TEXT, item_id TEXT, version INTEGER, content_json TEXT)"
        )
        connection.execute("INSERT INTO knowledge_version VALUES ('v-item', 'item', 1, '{}')")
        stages = {
            "check": {"result": {"units": [{"review": {"sources": [{
                "candidate_version_ids": {"item": "v-item"},
            }]}}]}},
        }
        receipt = {
            "id": "receipt", "generation": 2, "source_id": "source",
            "snapshot_id": "snapshot", "status": "completed",
            "review_status": "incomplete", "coverage_completed": 1,
            "coverage_total": 2, "candidate_count": 1,
        }

        def with_qualification(candidate_id: str) -> dict[str, object]:
            value = {
                "version": 1, "receipt_id": "receipt", "generation": 2,
                "source_id": "source", "snapshot_id": "snapshot",
                "candidate_ids": [candidate_id], "coverage_complete": False,
                "unresolved_group_refs": ["failed-window"],
            }
            return {
                **receipt,
                "review_diagnostic_json": json.dumps({"candidate_qualification": value}),
            }

        result = _source_resolution_authorization(
            connection,
            repository=object(), batch={}, item={}, receipt=with_qualification("item"),
            stages=stages, candidates=[{"item_id": "item", "version_id": "v-item"}],
        )
        self.assertEqual(result, {"item": "v-item"})

        with self.assertRaisesRegex(ValueError, "qualification"):
            _source_resolution_authorization(
                connection,
                repository=object(), batch={}, item={}, receipt=with_qualification("other"),
                stages=stages, candidates=[{"item_id": "item", "version_id": "v-item"}],
            )

        legacy_incomplete = {**receipt, "review_diagnostic_json": "{}"}
        with self.assertRaisesRegex(ValueError, "extraction-ready"):
            _source_resolution_authorization(
                connection,
                repository=object(), batch={}, item={}, receipt=legacy_incomplete,
                stages=stages, candidates=[{"item_id": "item", "version_id": "v-item"}],
            )

    def test_prior_group_member_and_fact_require_exact_target_version_and_projection(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        connection.execute(
            "CREATE TABLE knowledge_version (id TEXT, item_id TEXT, content_json TEXT)"
        )
        group_id = _identity_group_id(
            space="learning", work_id="identity-work", folder_id=None, canonical_id="A",
        )
        shared_roles = [
            {"name": "圣魂村", "role": "canonical", "first_source_chapter_order": 2},
            {"name": "诺丁城", "role": "alias", "first_source_chapter_order": 4},
        ]
        contents = {
            "A": {
                "candidate_role": "entity", "entity_kind": "location", "category": "location",
                "entity_name": "圣魂村", "entity_ref": "E-A", "value": "村庄",
                "narrative_position": _position("source-a", 2),
                "identity_group_id": group_id, "identity_is_canonical": True,
                "identity_display_name": "圣魂村", "identity_aliases": [],
                "identity_name_roles": shared_roles, "identity_resolution": "merged",
                "identity_resolution_reason": "既有来源理由。",
            },
            "B": {
                "candidate_role": "entity", "entity_kind": "location", "category": "location",
                "entity_name": "圣魂村", "entity_ref": "E-B", "value": "村庄",
                "narrative_position": _position("source-b", 5),
                "identity_group_id": group_id, "identity_is_canonical": False,
                "identity_display_name": "圣魂村", "identity_aliases": [],
                "identity_name_roles": shared_roles, "identity_resolution": "merged",
                "identity_resolution_reason": "较新的目标成员已确认该组的共同身份。",
            },
            "C": {
                "candidate_role": "entity", "entity_kind": "location", "category": "location",
                "entity_name": "圣魂村", "entity_ref": "E-C", "value": "村庄",
                "narrative_position": _position("source-c", 1),
            },
            "F": {
                "candidate_role": "fact", "entity_ref": "E-B", "predicate": "居住地",
                "identity_group_id": None, "identity_display_name": None,
                "narrative_position": _position("source-b", 5),
            },
        }
        refs = []
        for item_id, content in contents.items():
            version_id = f"v-{item_id}"
            connection.execute(
                "INSERT INTO knowledge_version VALUES (?, ?, ?)",
                (version_id, item_id, json.dumps(content, ensure_ascii=False)),
            )
            refs.append({
                "item_id": item_id,
                "version_id": version_id,
                "source_id": f"source-{item_id.lower()}",
                "snapshot_id": f"snapshot-source-{item_id.lower()}",
                "receipt_id": f"receipt-{item_id.lower()}",
                "generation": 1,
                "role": "prior_published" if item_id == "A" else "target",
                "evidence_refs": [{"id": f"evidence-{item_id}"}],
            })
        prompt_hash = "identity-prompt"
        decision = {
            "canonical_entity_item_id": "C",
            "rationale": "第三章证据将新候选归入现有村庄组。",
            "confidence": "high",
            "members": [
                {"entity_item_id": "A", "name_role": "canonical"},
                {"entity_item_id": "C", "name_role": "same_name"},
            ],
        }
        attempt = {
            "phase": "identity_resolution",
            "prompt_hash": prompt_hash,
            "candidate_refs": refs,
            "validated_response": {"schema_version": 1, "groups": [decision]},
        }

        # The parser preserves the prior canonical and recomputes earliest
        # order from exact source versions, including the new chapter-one row.
        from fxi_novel.knowledge.repository_parse_resolution_support import (
            _identity_attempt_group_projection,
        )

        resolved = _identity_attempt_group_projection(
            connection,
            attempt,
            group=decision,
            space="learning",
            logical_work_id="identity-work",
            folder_id=None,
        )
        self.assertIsNotNone(resolved)
        state, projection, _ = resolved
        self.assertEqual(projection["canonical_id"], "A")
        self.assertEqual(projection["group_id"], group_id)
        self.assertEqual(
            projection["reason"],
            contents["B"]["identity_resolution_reason"],
        )
        self.assertEqual(
            next(row for row in projection["name_roles"] if row["name"] == "圣魂村")[
                "first_source_chapter_order"
            ],
            1,
        )
        self.assertEqual(
            next(row for row in projection["name_roles"] if row["name"] == "诺丁城")[
                "first_source_chapter_order"
            ],
            4,
        )
        self.assertEqual(state["members"], {"A", "B", "C"})

        # Moving the original chapters into their正文 directory changes the
        # projection namespace, not the pinned existing group's rationale.
        moved = _identity_attempt_group_projection(
            connection, attempt, group=decision, space="learning",
            logical_work_id="identity-work", folder_id="moved-folder",
        )
        self.assertIsNotNone(moved)
        self.assertEqual(moved[1]["reason"], contents["B"]["identity_resolution_reason"])
        self.assertEqual(moved[1]["group_id"], group_id)
        legacy = _identity_attempt_group_projection(
            connection, attempt, group=decision, space="learning",
            logical_work_id="identity-work", folder_id="moved-folder", projection_version=6,
        )
        self.assertEqual(legacy[1]["group_id"], _identity_group_id(
            space="learning", work_id="identity-work", folder_id="moved-folder", canonical_id="A",
        ))

        previous_entity = contents["B"]
        current_entity = {
            **previous_entity,
            "identity_group_id": projection["group_id"],
            "identity_display_name": projection["display_name"],
            "identity_is_canonical": False,
            "identity_aliases": projection["aliases"],
            "identity_name_roles": projection["name_roles"],
            "identity_resolution": projection["resolution"],
            "identity_resolution_reason": projection["reason"],
            "identity_prompt_hash": prompt_hash,
            "identity_resolution_version": IDENTITY_RESOLUTION_VERSION,
        }
        current_entity["identity_source_content_hash"] = knowledge_graph_identity_source_content_hash(
            current_entity
        )
        diagnostics: list[str] = []
        self.assertTrue(_identity_attempt_supports_transition(
            connection,
            attempts=[attempt],
            item_id="B",
            previous_version_id="v-B",
            source_id="source-b",
            snapshot_id="snapshot-source-b",
            receipt_id="receipt-b",
            generation=1,
            previous=previous_entity,
            current=current_entity,
            space="learning",
            logical_work_id="identity-work",
            folder_id=None,
            diagnostics=diagnostics,
        ), diagnostics)

        unsupported_reason = {**current_entity, "identity_resolution_reason": "其他来源的未授权理由。"}
        unsupported_reason["identity_source_content_hash"] = knowledge_graph_identity_source_content_hash(
            unsupported_reason
        )
        self.assertFalse(_identity_attempt_supports_transition(
            connection,
            attempts=[attempt],
            item_id="B",
            previous_version_id="v-B",
            source_id="source-b",
            snapshot_id="snapshot-source-b",
            receipt_id="receipt-b",
            generation=1,
            previous=previous_entity,
            current=unsupported_reason,
            space="learning",
            logical_work_id="identity-work",
            folder_id=None,
            diagnostics=[],
        ))
        unpinned_attempt = {
            **attempt,
            "candidate_refs": [ref for ref in refs if ref["item_id"] != "B"],
        }
        self.assertFalse(_identity_attempt_supports_transition(
            connection,
            attempts=[unpinned_attempt],
            item_id="B",
            previous_version_id="v-B",
            source_id="source-b",
            snapshot_id="snapshot-source-b",
            receipt_id="receipt-b",
            generation=1,
            previous=previous_entity,
            current=current_entity,
            space="learning",
            logical_work_id="identity-work",
            folder_id=None,
            diagnostics=[],
        ))

        previous_fact = contents["F"]
        current_fact = {
            **previous_fact,
            "identity_group_id": projection["group_id"],
            "identity_display_name": projection["display_name"],
            "identity_prompt_hash": prompt_hash,
            "identity_resolution_version": IDENTITY_RESOLUTION_VERSION,
        }
        current_fact["identity_source_content_hash"] = knowledge_graph_identity_source_content_hash(
            current_fact
        )
        self.assertTrue(_identity_fact_transition_supported(
            connection,
            attempts=[attempt],
            item_id="F",
            previous_version_id="v-F",
            source_id="source-f",
            snapshot_id="snapshot-source-f",
            receipt_id="receipt-f",
            generation=1,
            previous=previous_fact,
            current=current_fact,
            space="learning",
            logical_work_id="identity-work",
            folder_id=None,
            diagnostics=[],
        ))

        # Removing the newly earliest exact source pin makes the order change
        # unprovable instead of allowing a synthetic singleton receipt.
        incomplete_attempt = {
            **attempt,
            "candidate_refs": [ref for ref in refs if ref["item_id"] != "C"],
        }
        self.assertIsNone(_identity_attempt_group_projection(
            connection,
            incomplete_attempt,
            group=decision,
            space="learning",
            logical_work_id="identity-work",
            folder_id=None,
        ))


if __name__ == "__main__":
    unittest.main()
