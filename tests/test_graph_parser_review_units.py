"""Bounded targets, disjoint coverage, context-only identities and native recovery."""

import asyncio
from copy import deepcopy

import pytest

from fxi_novel.knowledge.graph_parser_review_units import (
    KnowledgeGraphSourceReviewUnitMixin, build_review_unit_plan, select_review_unit_rows,
    MAX_REVIEW_TARGETS, MAX_DIALOGUE_TARGETS, requires_dependency_check,
    _checkpoint_matches_current_candidates, _checkpoint_selection_matches, _set_unit_id,
    _target_rows, _unit_dependencies,
    _validate_saved_plan,
)


def _rows(count=145):
    return [{"item_id": f"item-{index:03}", "version_id": f"version-item-{index:03}",
             "content": {"candidate_role": "entity" if index < 4 else "fact",
                         "entity_ref": f"entity-{index}" if index < 4 else None,
                         "evidence_quote_start": index * 40,
                         "evidence_quote_end": index * 40 + 20}}
            for index in range(count)]


def _dialogues():
    return [{"record_id": f"speech-{index}", "start_offset": index * 40 + 10,
             "end_offset": index * 40 + 25} for index in range(26)]


def _plan(rows=None, dialogues=None):
    return build_review_unit_plan(rows or _rows(), _dialogues() if dialogues is None else dialogues,
                                  source_id="source", snapshot_id="snapshot", section_ids=None)


def test_plan_bounds_every_target_without_losing_or_repeating_rows():
    plan = _plan()
    assert all(len(unit["candidate_ids"]) + len(unit["dialogue_record_ids"]) <= MAX_REVIEW_TARGETS for unit in plan)
    assert all(len(unit["dialogue_record_ids"]) <= MAX_DIALOGUE_TARGETS for unit in plan)
    assert all(unit["source_position_end"] - unit["source_position_start"] <= 4096 for unit in plan)
    candidates = [value for unit in plan for value in unit["candidate_ids"]]
    speeches = [value for unit in plan for value in unit["dialogue_record_ids"]]
    assert len(set(candidates)) == len(candidates) == 145
    assert len(set(speeches)) == len(speeches) == 26
    assert plan == _plan(list(reversed(_rows())), list(reversed(_dialogues())))


def test_plan_groups_by_source_offsets_instead_of_uuid_and_keeps_far_ranges_separate():
    rows = [
        {"item_id": "z-late", "version_id": "v-z", "content": {
            "candidate_role": "fact", "evidence_quote_start": 5000, "evidence_quote_end": 5010}},
        {"item_id": "a-early", "version_id": "v-a", "content": {
            "candidate_role": "fact", "evidence_quote_start": 0, "evidence_quote_end": 10}},
    ]
    dialogues = [{"record_id": "speech-near", "start_offset": 20, "end_offset": 30}]
    plan = _plan(rows, dialogues)
    assert [unit["candidate_ids"] for unit in plan] == [["a-early"], ["z-late"]]
    assert [unit["dialogue_record_ids"] for unit in plan] == [["speech-near"], []]
    assert all(unit["source_position_end"] - unit["source_position_start"] <= 4096 for unit in plan)


def test_dependency_unit_precedes_dependent_target_while_preserving_locality_for_ready_units():
    rows = [
        {"item_id": "dependent", "version_id": "v-dependent", "content": {
            "candidate_role": "fact", "target_entity_ref": "person-ref",
            "evidence_quote_start": 0, "evidence_quote_end": 10}},
        {"item_id": "person", "version_id": "v-person", "content": {
            "candidate_role": "entity", "entity_ref": "person-ref",
            "evidence_quote_start": 6000, "evidence_quote_end": 6010}},
        {"item_id": "independent", "version_id": "v-independent", "content": {
            "candidate_role": "fact", "evidence_quote_start": 12000, "evidence_quote_end": 12010}},
    ]
    plan = _plan(rows, [])
    assert [unit["candidate_ids"] for unit in plan] == [["person"], ["dependent"], ["independent"]]
    assert plan[1]["depends_on_candidate_ids"] == ["person"]
    assert plan[1]["dependency_version_ids"] == {"person": "v-person"}
    assert plan[2]["depends_on_candidate_ids"] == []


def test_chapter9_archive_entity_fact_links_do_not_form_a_group_cycle():
    # The two cross-boundary links below are copied from the read-only chapter 9
    # archive (source 8e9561c65fe6592c87544535492483d1, snapshot
    # 1034a6efed0e5b809bc6072a1986424f). Thirty independent earlier targets
    # retain the original 32-target split: d402... depends on 7be..., while
    # 9ab... in the next group depends on 93f.... Mixed groups therefore had
    # reciprocal edges even though the candidate-level prerequisites do not.
    rows = [{
        "item_id": f"archive-padding-{index:02}", "version_id": f"padding-version-{index:02}",
        "content": {"candidate_role": "fact", "evidence_quote_start": index,
                    "evidence_quote_end": index + 1},
    } for index in range(30)]
    rows.extend([
        {"item_id": "d4023a70ffca447da0098fdf380b255c", "version_id": "1daf8247f061441c9cb51abe349a8d43",
         "content": {"candidate_role": "fact", "entity_ref": "7be57f5a5d23480a9959bb45698b203f",
                     "evidence_quote_start": 25, "evidence_quote_end": 44}},
        {"item_id": "93f46c73a0e049b286bd9b87c3b31934", "version_id": "a7fbd5db7c49483bb2e07b7296a35d6a",
         "content": {"candidate_role": "entity", "entity_ref": "93f46c73a0e049b286bd9b87c3b31934",
                     "evidence_quote_start": 94, "evidence_quote_end": 103}},
        {"item_id": "7be57f5a5d23480a9959bb45698b203f", "version_id": "c95a758efff2432c93fc257ff30a8807",
         "content": {"candidate_role": "entity", "entity_ref": "7be57f5a5d23480a9959bb45698b203f",
                     "evidence_quote_start": 119, "evidence_quote_end": 136}},
        {"item_id": "9ab00983837145a0bf9ae94ed12ddf3e", "version_id": "cc31d6ecd94f4e9eb0b6bb6561f3793e",
         "content": {"candidate_role": "fact", "entity_ref": "6aebc70d2d404f7181996403593939e8",
                     "asserted_by_entity_ref": "93f46c73a0e049b286bd9b87c3b31934",
                     "evidence_quote_start": 119, "evidence_quote_end": 136}},
    ])

    targets, _, entity_candidates_by_ref = _target_rows(rows, [])
    former_first_group = targets[:MAX_REVIEW_TARGETS]
    former_second_group = targets[MAX_REVIEW_TARGETS:]
    first_ids = {target["id"] for target in former_first_group}
    second_ids = {target["id"] for target in former_second_group}
    d402 = next(target for target in former_first_group
                if target["id"] == "d4023a70ffca447da0098fdf380b255c")
    recap = next(target for target in former_second_group
                 if target["id"] == "9ab00983837145a0bf9ae94ed12ddf3e")
    assert "93f46c73a0e049b286bd9b87c3b31934" in first_ids
    assert "7be57f5a5d23480a9959bb45698b203f" in second_ids
    assert "7be57f5a5d23480a9959bb45698b203f" in _unit_dependencies(
        [d402], entity_candidates_by_ref)
    assert "93f46c73a0e049b286bd9b87c3b31934" in _unit_dependencies(
        [recap], entity_candidates_by_ref)

    plan = build_review_unit_plan(rows, [], source_id="chapter-9", snapshot_id="snapshot-9",
                                 section_ids=None)
    assert {unit["unit_lane"] for unit in plan} == {"entity", "dependent"}
    assert all(
        all(next(unit for unit in plan if candidate in unit["candidate_ids"])["unit_lane"] == "entity"
            for candidate in unit["depends_on_candidate_ids"])
        for unit in plan
    )
    by_candidate = {candidate: unit for unit in plan for candidate in unit["candidate_ids"]}
    assert by_candidate["d4023a70ffca447da0098fdf380b255c"]["unit_lane"] == "dependent"
    assert by_candidate["9ab00983837145a0bf9ae94ed12ddf3e"]["unit_lane"] == "dependent"
    assert by_candidate["93f46c73a0e049b286bd9b87c3b31934"]["unit_lane"] == "entity"
    assert by_candidate["7be57f5a5d23480a9959bb45698b203f"]["unit_lane"] == "entity"
    assert "7be57f5a5d23480a9959bb45698b203f" in by_candidate[
        "d4023a70ffca447da0098fdf380b255c"]["depends_on_candidate_ids"]
    assert "93f46c73a0e049b286bd9b87c3b31934" in by_candidate[
        "9ab00983837145a0bf9ae94ed12ddf3e"]["depends_on_candidate_ids"]


def test_legacy_mixed_plan_migrates_and_reuses_only_exact_revision_checkpoints():
    rows = [
        {"item_id": "fact-a", "version_id": "fact-v1", "content": {
            "candidate_role": "fact", "target_entity_ref": "person-ref",
            "evidence_quote_start": 0, "evidence_quote_end": 10}},
        {"item_id": "person", "version_id": "person-v1", "content": {
            "candidate_role": "entity", "entity_ref": "person-ref",
            "evidence_quote_start": 20, "evidence_quote_end": 30}},
    ]
    fresh = _plan(rows, [])
    legacy = [{
        "version": 2, "source_id": "source", "snapshot_id": "snapshot", "section_ids": None,
        "source_position_start": 0, "source_position_end": 30,
        "candidate_ids": ["fact-a", "person"], "dialogue_record_ids": [],
        "depends_on_candidate_ids": [], "dependency_version_ids": {},
    }]
    _set_unit_id(legacy[0])
    _validate_saved_plan(legacy, fresh)

    saved_dependent = {
        **fresh[1], "version": 2,
    }
    saved_dependent.pop("unit_lane")
    _set_unit_id(saved_dependent)
    assert _checkpoint_selection_matches(saved_dependent, fresh[1])
    checkpoint = {"selection": saved_dependent, "review": {
        "review_contract": "contract", "protected_candidates": [], "sources": [{
            "candidate_version_ids": {"fact-a": "fact-v1"},
        }],
    }}
    source = {"review_contract": "contract", "candidate_version_ids": {
        "person": "person-v1", "fact-a": "fact-v1",
    }}
    assert _checkpoint_matches_current_candidates(checkpoint, source, fresh[1], source["candidate_version_ids"])
    changed = dict(source["candidate_version_ids"], person="person-v2")
    assert not _checkpoint_matches_current_candidates(checkpoint, source, fresh[1], changed)

    wrong_dependency = {**saved_dependent, "dependency_version_ids": {"person": "person-v2"}}
    assert not _checkpoint_selection_matches(wrong_dependency, fresh[1])


def test_entity_and_speaker_context_is_read_only_and_not_a_review_target():
    rows = [{"item_id": "person", "status": "rejected", "content": {"candidate_role": "entity", "entity_ref": "person"}},
            {"item_id": "event", "content": {"candidate_role": "fact", "typed_event": {"actor_ref": "person"}}}]
    selected, dialogues, context = select_review_unit_rows(rows, [], {"candidate_ids": ["event"], "dialogue_record_ids": []})
    assert [row["item_id"] for row in selected] == ["event"]
    assert [row["item_id"] for row in context] == ["person"]
    assert not dialogues
    assert requires_dependency_check(selected[0]["content"], context)
    assert not requires_dependency_check({"entity_ref": "other"}, context)
    with pytest.raises(ValueError, match="changed or were removed"):
        select_review_unit_rows(rows, [], {"candidate_ids": ["missing"], "dialogue_record_ids": []})


@pytest.mark.parametrize("assessment", ["excluded", "uncertain", "conflicted", "supported", "attributed", "inferred"])
def test_existing_unconfirmed_dependency_judgment_is_reused_without_reopening(assessment):
    from fxi_novel.knowledge.models import KNOWLEDGE_GRAPH_SEMANTIC_REVIEW_VERSION

    content = {"candidate_role": "fact", "entity_ref": "person", "semantic_review": {
        "reviewer_version": KNOWLEDGE_GRAPH_SEMANTIC_REVIEW_VERSION, "assessment": assessment,
    }}
    context = [{"item_id": "person", "status": "rejected"}]
    assert requires_dependency_check(content, context) == (assessment in {"supported", "attributed", "inferred"})
    content["semantic_review"]["reviewer_version"] = "knowledge-graph-semantic-review.v1"
    assert requires_dependency_check(content, context)


class _Harness(KnowledgeGraphSourceReviewUnitMixin):
    semantic_review_prompt = "one current semantic prompt"
    def __init__(self):
        self.rows = _rows()
        self.dialogues = _dialogues()
        self.calls = []
        self.fail_unit = None
        self.version_ids = {row["item_id"]: "version-" + row["item_id"] for row in self.rows}

    async def _review_sources_unit(self, *, _plan_only=False, _selection=None, **request):
        if _plan_only:
            return {"reviewed_candidates": 0, "uncertain_candidates": 0, "sources": [{
                "source_id": "source", "snapshot_id": "snapshot", "title": "第9章", "status": "planned",
                "candidate_version_ids": self.version_ids,
                "dialogue_input_hashes": {unit["unit_id"]: "dialogue-" + unit["unit_id"]
                                           for unit in _plan(self.rows, self.dialogues)},
                "review_units": _plan(self.rows, self.dialogues),
            }]}
        assert request["expected_snapshot_ids"] == {"source": "snapshot"}
        self.calls.append(_selection["unit_id"])
        failed = _selection["unit_id"] == self.fail_unit
        return {"failed_sources": int(failed), "reviewed_candidates": 0 if failed else len(_selection["candidate_ids"]),
                "uncertain_candidates": 0, "sources": [{
                    "title": "第9章", "status": "failed" if failed else "completed",
                    "reviewed_candidates": 0 if failed else len(_selection["candidate_ids"]),
                    "candidate_versions": {} if failed else {value: 2 for value in _selection["candidate_ids"]},
                    "candidate_version_ids": {} if failed else {value: "version-" + value for value in _selection["candidate_ids"]},
                    "dialogue_review_proof": {"input_annotation_hash": "dialogue-" + _selection["unit_id"],
                                              "reviewed_record_ids": _selection["dialogue_record_ids"]},
                    "diagnostic": "bounded unit failed" if failed else None,
                }]}


def test_checkpointed_resume_reuses_complete_units_after_reordering_and_counts_one_source():
    async def run():
        parser = _Harness()
        plans = {}
        completed = []
        parser.fail_unit = _plan()[2]["unit_id"]
        request = dict(space="learning", work_id=None, folder_id=None, source_ids=["source"],
                       _checkpoint_plan=lambda source_id, plan: plans.update({source_id: deepcopy(plan)}),
                       _checkpoint_unit=lambda selection, review: completed.append({
                           "selection": deepcopy(selection), "review": deepcopy(review), "status": "completed"}),
                       _planned_units=plans, _completed_units=completed)
        first = await parser.review_sources(**request)
        assert first["failed_sources"] == 1 and len(completed) == len(_plan()) - 1
        assert len(parser.calls) == len(_plan())
        parser.rows.reverse()
        parser.calls = []
        parser.fail_unit = None
        second = await parser.review_sources(**request)
        assert second["failed_sources"] == 0 and second["completed_sources"] == second["total_sources"] == 1
        assert len(parser.calls) == 1 and len(completed) == len(_plan())
        assert second["reviewed_candidates"] == 145
        assert len(second["sources"][0]["candidate_versions"]) == 145
    asyncio.run(run())


@pytest.mark.parametrize("change", ["unit_id", "version", "candidate_ids"])
def test_tampered_saved_plan_is_rejected_before_dispatch(change):
    async def run():
        parser = _Harness()
        saved = _plan()
        saved[0][change] = "wrong" if change == "unit_id" else True if change == "version" else [""]
        with pytest.raises(ValueError, match="saved semantic review"):
            await parser.review_sources(space="learning", work_id=None, folder_id=None,
                                        source_ids=["source"], _planned_units={"source": saved})
        assert not parser.calls
    asyncio.run(run())


def test_changed_candidate_revision_cannot_reuse_a_completed_unit():
    async def run():
        parser = _Harness()
        completed = []
        await parser.review_sources(space="learning", work_id=None, folder_id=None, source_ids=["source"],
                                    _checkpoint_unit=lambda selection, review: completed.append({
                                        "selection": selection, "status": "completed", "review": review}))
        parser.calls.clear()
        parser.version_ids["item-000"] = "new-revision"
        await parser.review_sources(space="learning", work_id=None, folder_id=None, source_ids=["source"],
                                    _completed_units=completed)
        assert parser.calls == [_plan()[0]["unit_id"]]
    asyncio.run(run())


def test_changed_target_set_rejects_saved_unit_plan_before_dispatch():
    async def run():
        parser = _Harness()
        saved = {"source": _plan()}
        parser.rows.pop()
        with pytest.raises(ValueError, match="target set changed"):
            await parser.review_sources(space="learning", work_id=None, folder_id=None,
                                        source_ids=["source"], _planned_units=saved)
        assert parser.calls == []
    asyncio.run(run())


def test_failed_unit_does_not_stop_independent_unit_and_dependent_unit_waits_then_resumes():
    async def run():
        parser = _Harness()
        parser.rows = [
            {"item_id": "dependent", "version_id": "version-dependent", "content": {
                "candidate_role": "fact", "target_entity_ref": "person-ref",
                "evidence_quote_start": 0, "evidence_quote_end": 10}},
            {"item_id": "person", "version_id": "version-person", "content": {
                "candidate_role": "entity", "entity_ref": "person-ref",
                "evidence_quote_start": 6000, "evidence_quote_end": 6010}},
            {"item_id": "independent", "version_id": "version-independent", "content": {
                "candidate_role": "fact", "evidence_quote_start": 12000, "evidence_quote_end": 12010}},
        ]
        parser.dialogues = []
        parser.version_ids = {row["item_id"]: row["version_id"] for row in parser.rows}
        plan = _plan(parser.rows, [])
        parser.fail_unit = plan[0]["unit_id"]
        completed = []
        first = await parser.review_sources(
            space="learning", work_id=None, folder_id=None, source_ids=["source"],
            _checkpoint_unit=lambda selection, review: completed.append({
                "selection": deepcopy(selection), "status": "completed", "review": deepcopy(review)}),
        )
        units = first["sources"][0]["review_units"]
        assert [row["status"] for row in units] == ["failed", "waiting", "completed"]
        assert units[1]["blocked_by_unit_ids"] == [plan[0]["unit_id"]]
        assert parser.calls == [plan[0]["unit_id"], plan[2]["unit_id"]]
        assert [row["selection"]["candidate_ids"] for row in completed] == [["independent"]]

        parser.fail_unit = None
        parser.calls.clear()
        second = await parser.review_sources(
            space="learning", work_id=None, folder_id=None, source_ids=["source"],
            _planned_units={"source": plan}, _completed_units=completed,
            _checkpoint_unit=lambda selection, review: completed.append({
                "selection": deepcopy(selection), "status": "completed", "review": deepcopy(review)}),
        )
        assert second["failed_sources"] == 0
        assert [row["status"] for row in second["sources"][0]["review_units"]] == [
            "completed", "completed", "completed"]
        assert parser.calls == [plan[0]["unit_id"], plan[1]["unit_id"]]
    asyncio.run(run())


def test_retained_entity_allows_its_fact_to_continue_despite_unrelated_same_unit_failure():
    class PartialHarness(_Harness):
        async def _review_sources_unit(self, **kwargs):
            result = await super()._review_sources_unit(**kwargs)
            if kwargs.get("_selection") and result.get("failed_sources"):
                source = result["sources"][0]
                source["candidate_version_ids"] = {"person": "version-person", "bad": "version-bad"}
                source["reviewed_candidate_version_ids"] = {"person": "version-person"}
            return result

    async def run():
        parser = PartialHarness()
        parser.rows = [
            {"item_id": name, "version_id": "version-" + name, "content": {
                "candidate_role": "entity", "entity_ref": name,
                "evidence_quote_start": i * 10, "evidence_quote_end": i * 10 + 5}}
            for i, name in enumerate(["person", "bad"])
        ] + [{"item_id": "fact", "version_id": "version-fact", "content": {
            "candidate_role": "fact", "entity_ref": "person",
            "evidence_quote_start": 30, "evidence_quote_end": 35}}]
        parser.dialogues = []
        parser.version_ids = {row["item_id"]: row["version_id"] for row in parser.rows}
        parser.fail_unit = _plan(parser.rows, [])[0]["unit_id"]
        result = await parser.review_sources(space="learning", work_id=None, folder_id=None, source_ids=["source"])
        assert [row["status"] for row in result["sources"][0]["review_units"]] == ["failed", "completed"]
        assert len(parser.calls) == 2
        assert result["sources"][0]["reviewed_candidate_version_ids"] == {"person": "version-person"}
    asyncio.run(run())
