"""Reading-only sharing preserves the complete owner-validated context."""

from copy import deepcopy
import json

import pytest

from fxi_novel.learning.context_reading_projection import (
    project_study_context_reading, restore_study_context_reading,
)


def context_fixture():
    position = {"domain": "narrative_order", "space": "learning", "work_id": "logical-book",
        "branch_id": None, "source_id": "chapter-15", "snapshot_id": "snapshot-15",
        "content_hash": "a" * 64, "manifest_hash": "b" * 64, "manifest_revision": 1,
        "manifest_coordinate_status": "verified", "coordinate_basis": "verified",
        "chapter_title": "第15章", "narrative_ordinal": 14, "offset_unit": "unicode_codepoint"}
    return {"query": "学习人物所知和未知", "interpretation_boundary": "来源观察须结合原文核对。",
        "scope": {"work_id": "logical-book", "source_ids": ["chapter-15"]},
        "candidates": [{"item_id": f"person-{i}", "version_id": f"public-{i}",
            "evidence_ids": [f"span-{i}"], "version_scope": "published", "content": {
                "entity_name": "夕若", "value": "自称夕若；名字关联尚待核对。", "candidate_role": "entity",
                "narrative_position": {**position, "offset": i}, "story_time_expression": None,
                "speaker": "风", "asserted_by": None, "holder": "夕若", "audience": ["风"],
                "observed_by": "风", "claim_basis": "belief", "state_value_status": "explicit_unknown",
                "conditions": ["查验之前"], "unknown_extension": {"unknown": None}}} for i in range(9)],
        "evidence": [{"id": f"ref-{i}", "source_id": "chapter-15", "snapshot_id": "snapshot-15",
            "space": "learning", "work_id": None, "branch_id": None, "content_hash": "a" * 64,
            "offset_unit": "unicode_codepoint", "start_offset": i, "end_offset": i + 4,
            "quote": "我叫夕若", "quote_hash": "c" * 64, "purpose": "query_entity_candidate",
            "role": "supporting" if i else "counterexample", "evidence_ids": [f"span-{i}"]} for i in range(10)]}


def test_projection_roundtrips_all_fields_without_mutating_receipt():
    context = context_fixture()
    before = deepcopy(context)
    projected = project_study_context_reading(context)
    assert context == before
    assert restore_study_context_reading(projected) == context
    assert len(json.dumps(projected, ensure_ascii=False)) < len(json.dumps(context, ensure_ascii=False))
    assert len(projected["reading_shared"]["narrative_positions"]) == 1
    assert len(projected["reading_shared"]["evidence_source_metadata"]) == 1
    for original, row in zip(context["candidates"], projected["candidates"]):
        assert row["evidence_ids"] == original["evidence_ids"]
        assert {k: v for k, v in row["content"].items() if k != "narrative_position"} == {
            k: v for k, v in original["content"].items() if k != "narrative_position"}
        assert row["content"]["narrative_position"]["source_id"] == "chapter-15"
        assert row["content"]["narrative_position"]["snapshot_id"] == "snapshot-15"
    for original, row in zip(context["evidence"], projected["evidence"]):
        for key in ("id", "source_id", "snapshot_id", "start_offset", "end_offset", "quote", "role", "purpose", "evidence_ids"):
            assert row[key] == original[key]


def test_distinct_scopes_hashes_nulls_and_missing_values_never_merge():
    context = context_fixture()
    context["candidates"][0]["content"]["narrative_position"]["work_id"] = None
    context["candidates"][1]["content"]["narrative_position"].pop("work_id")
    context["candidates"][2]["content"]["narrative_position"]["manifest_revision"] = 2
    context["candidates"][3]["content"]["narrative_position"]["coordinate_basis"] = "unknown"
    context["evidence"][0]["content_hash"] = "d" * 64
    context["evidence"][1].pop("work_id")
    context["evidence"][2]["work_id"] = "other-book"
    projected = project_study_context_reading(context)
    assert restore_study_context_reading(projected) == context
    assert "position_ref" not in projected["candidates"][0]["content"]["narrative_position"]
    assert projected["candidates"][0]["content"]["narrative_position"]["work_id"] is None
    assert "work_id" not in projected["candidates"][1]["content"]["narrative_position"]
    assert "source_metadata_ref" not in projected["evidence"][0]


def test_explicit_null_or_absent_position_and_unknown_content_remain_intact():
    context = context_fixture()
    context["candidates"][0]["content"]["narrative_position"] = None
    context["candidates"][1]["content"].pop("narrative_position")
    context["candidates"][2]["content"]["story_time_expression"] = ""
    context["evidence"][0]["role"] = "context"
    projected = project_study_context_reading(context)
    assert restore_study_context_reading(projected) == context
    assert projected["candidates"][0]["content"]["narrative_position"] is None
    assert "narrative_position" not in projected["candidates"][1]["content"]


@pytest.mark.parametrize("identity_field", ["source_id", "snapshot_id", "manifest_hash", "manifest_revision"])
def test_identical_metadata_of_different_sources_snapshots_or_manifests_is_not_shared(identity_field):
    context = context_fixture()
    for row in context["candidates"][:3]:
        row["content"]["narrative_position"][identity_field] = "different-coordinate"
    for row in context["evidence"][:3]:
        row[identity_field] = "different-coordinate"
    projected = project_study_context_reading(context)
    assert restore_study_context_reading(projected) == context
    first_position = projected["candidates"][0]["content"]["narrative_position"]["position_ref"]
    other_position = projected["candidates"][-1]["content"]["narrative_position"]["position_ref"]
    assert first_position != other_position
    assert projected["evidence"][0]["source_metadata_ref"] != projected["evidence"][-1]["source_metadata_ref"]
    assert "inherits" in projected["reading_shared"]["inheritance"]


def test_single_occurrences_do_not_add_indirection():
    context = context_fixture()
    context["candidates"] = context["candidates"][:1]
    context["evidence"] = context["evidence"][:1]
    assert project_study_context_reading(context) == context
    assert restore_study_context_reading(context) == context


@pytest.mark.parametrize("mutation", ["missing_position", "missing_source", "overlapping_position", "overlapping_source",
                                      "metadata_content_injection"])
def test_restorer_refuses_missing_or_ambiguous_metadata(mutation):
    projected = project_study_context_reading(context_fixture())
    if mutation == "missing_position":
        projected["reading_shared"]["narrative_positions"].clear()
    elif mutation == "missing_source":
        projected["reading_shared"]["evidence_source_metadata"].clear()
    elif mutation == "overlapping_position":
        projected["candidates"][0]["content"]["narrative_position"]["work_id"] = "other-book"
    elif mutation == "overlapping_source":
        projected["evidence"][0]["content_hash"] = "other-hash"
    else:
        projected["reading_shared"]["narrative_positions"]["N1"]["value"] = "changed description"
    with pytest.raises(ValueError):
        restore_study_context_reading(projected)


def test_empty_and_minimal_unshared_rows_roundtrip_alongside_shared_rows():
    context = context_fixture()
    context["candidates"].extend([
        {"item_id": "empty-position", "content": {"narrative_position": {}}},
        {"item_id": "minimal-position", "content": {"narrative_position": {
            "source_id": "other-source", "snapshot_id": "other-snapshot", "offset": 8}}},
    ])
    context["evidence"].extend([{}, {"source_id": "other-source", "snapshot_id": "other-snapshot",
        "start_offset": 8, "end_offset": 12, "quote": "我叫夕若"}])
    projected = project_study_context_reading(context)
    assert "reading_shared" in projected
    assert projected["candidates"][-2:] == context["candidates"][-2:]
    assert projected["evidence"][-2:] == context["evidence"][-2:]
    assert restore_study_context_reading(projected) == context


@pytest.mark.parametrize("kind", ["position", "evidence"])
def test_removed_reference_is_detected_by_full_restored_payload_comparison(kind):
    context = context_fixture()
    projected = project_study_context_reading(context)
    if kind == "position":
        projected["candidates"][0]["content"]["narrative_position"].pop("position_ref")
    else:
        projected["evidence"][0].pop("source_metadata_ref")
    restored = restore_study_context_reading(projected)
    assert restored != context


def test_reserved_projection_fields_are_not_silently_overwritten():
    context = context_fixture()
    context["evidence"][0]["source_metadata_ref"] = "original-extension"
    with pytest.raises(ValueError, match="reserved"):
        project_study_context_reading(context)
    with pytest.raises(ValueError, match="already projected"):
        project_study_context_reading(project_study_context_reading(context_fixture()))


@pytest.mark.parametrize("mutation", ["removed_table", "null_table", "unknown_table"])
def test_restorer_requires_the_declared_shared_tables(mutation):
    projected = project_study_context_reading(context_fixture())
    if mutation == "removed_table":
        projected.pop("reading_shared")
    elif mutation == "null_table":
        projected["reading_shared"] = None
    else:
        projected["reading_shared"]["other_content"] = {"value": "changed"}
    with pytest.raises(ValueError):
        restore_study_context_reading(projected)
