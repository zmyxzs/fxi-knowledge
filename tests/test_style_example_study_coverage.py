"""Each example attributes only the source studies that actually supplied it."""
from __future__ import annotations

import asyncio

import pytest

from fxi_novel.learning.service import LearningService
from test_style_operation_synthesis import operation
from test_style_rule_support import PROMPTS, RecordedExtraction, learning_fixture, rule


@pytest.mark.parametrize("legacy_quote", [False, True])
def test_example_study_coverage_uses_the_selected_source_not_all_studies(learning_fixture, legacy_quote):
    knowledge, _, work = learning_fixture
    texts = ["甲先问事情的经过，听到回应后才决定下一步安排。", "乙来不及等详细解释，听到门外脚步就催促对方离开。"]
    studies = []
    for index, text in enumerate(texts):
        source = knowledge.add_text_source(title=f"研究来源 {index}", content=text, space="writing", work_id=work)
        studies.append(knowledge.create_knowledge_item(space="writing", work_id=work, kind="source_study", status="draft",
            evidence_span_ids=source.evidence_span_ids, content={"analysis_type": "style", "source_refs": [{
                "source_id": source.source_id, "snapshot_id": source.snapshot_id, "content_hash": source.content_hash,
                "title": f"研究来源 {index}", "selection_kind": "whole_source"}],
                "markdown": "### 交流观察\n\n当前来源人物的回应。" + f"【EVIDENCE:{source.evidence_span_ids[0]}】"}))
    response = {"author_alias": "原作", "summary": "两段不同处境的交流。"}
    if legacy_quote:
        response["dialogue_subtext"] = [rule(source_selection=None, exemplar_quote=texts[0],
            counterexample_selections=[{"sample_id": "s2", "start_paragraph": 1, "end_paragraph": 1}])]
    else:
        learned = operation()
        learned["examples"][1].update(role="counterexample")
        learned["examples"][1]["source_selection"] = {"sample_id": "s2", "start_paragraph": 1, "end_paragraph": 1}
        response["operations"] = [learned]
    model = RecordedExtraction(response)
    service = LearningService(knowledge, model, PROMPTS)
    study_ids = [study["id"] for study in studies]
    receipts = []
    if legacy_quote:
        with pytest.raises(ValueError, match="contract"):
            asyncio.run(service.synthesize_style_methods(study_ids, on_progress=receipts.append))
        service.chat_model = None
        result = asyncio.run(service.synthesize_style_methods(study_ids, completed_extractions={1: receipts[0]}))
    else:
        result = asyncio.run(service.synthesize_style_methods(study_ids, on_progress=receipts.append))
    item = result["item"]
    payload = item["content"]["craft_method_payload"]
    assert payload["profile"]["study_item_ids"] == study_ids
    method = knowledge.read_scoped_writing_style_methods(item["id"], expected_version=item["version"],
        profile_id=payload["profile"]["profile_id"], work_id=work)["methods"][0]
    assert [row["text"] for row in method["examples"]] == texts
    assert method["coverage"]["support"][0]["study_item_ids"] == [study_ids[0]]
    assert method["coverage"]["counterexamples"][0]["study_item_ids"] == [study_ids[1]]
    assert len(model.requests) == 1
