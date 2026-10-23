"""Source rules remain useful when only their reviewed base claim is public."""

import json
from types import SimpleNamespace

import pytest

from fxi_novel.knowledge.candidate_query_projection import candidate_publication_projection
from fxi_novel.knowledge.graph_parser_source_validation import validate_graph_output
from fxi_novel.knowledge.repository_parse_publication import graph_candidate_publication_capabilities
from fxi_novel.models.gateway import ChatResult


@pytest.mark.parametrize("claim_basis", ["narration", "dialogue"])
@pytest.mark.parametrize("existing_fact", [False, True])
def test_rule_conditions_survive_base_publication_without_promoting_typed_rules(
    claim_basis, existing_fact,
):
    quote = "只有持有火石才能施展火苗术，每次消耗一点法力；雨中不可用，避雨后例外。"
    original_value = "持火石且不淋雨时可以施展，每次消耗一点法力。"
    raw = {
        "schema_version": 4, "covered_segment_refs": ["E1"],
        "entities": [{
            "ref": "N1", "category": "ability", "name": "火苗术",
            "description": "需要火石的法术", "claim_basis": claim_basis,
            "source_segment_ref": "E1", "evidence_quote": quote,
        }],
        "facts": [{
            "entity_ref": "N1", "category": "rule", "predicate": "施展规则",
            "value": original_value, "claim_basis": claim_basis,
            "asserted_by": "术士" if claim_basis == "dialogue" else None,
            "source_segment_ref": "E1", "evidence_quote": quote,
        }] if existing_fact else [],
        "rules": [{
            "ref": "R1", "entity_ref": "N1", "name": "施展规则",
            "conditions": ["持有火石"], "effect": "可以施展火苗术",
            "limitations": ["雨中不可用"], "costs": ["一点法力"],
            "exceptions": ["避雨后例外"], "claim_basis": claim_basis,
            "source_segment_ref": "E1", "evidence_quote": quote,
        }],
    }
    source = SimpleNamespace(
        space="learning", work_id=None, source_id="chapter", snapshot_id="snapshot",
        content=quote, source_chapter_order=None,
    )
    candidates, _, _ = validate_graph_output(
        ChatResult(text=json.dumps(raw, ensure_ascii=False), provider_model="test", usage={}),
        parse_warning=None, profile_attribute_category_normalized=lambda: False,
        hydrate_evidence_references=lambda _: None, canonical_entity_ids={},
        current_segments=[{
            "ref": "E1", "evidence_span_id": "span", "start_offset": 0,
            "end_offset": len(quote), "text": quote,
        }], validation_errors=[], source=source, branch_id=None,
    )
    rule_candidates = [c["content"] for c in candidates if c["content"].get("typed_rule")]
    assert len(rule_candidates) == 1
    content = rule_candidates[0]
    published, gaps = candidate_publication_projection(
        content, graph_candidate_publication_capabilities("reviewed_claim"),
    )
    assert published["value"] == (original_value if existing_fact else quote)
    assert published["claim_basis"] == claim_basis
    if existing_fact and claim_basis == "dialogue":
        assert published["asserted_by"] == "术士"
    assert "typed_rule" not in published
    assert "typed_rule:not_published" in gaps
    assert content["typed_rule"]["conditions"] == ["持有火石"]
    assert content["typed_rule"]["evidence_refs"][0]["quote"] == quote
