"""Query input keeps event decisions without repeating finalized audit detail."""

from copy import deepcopy

from fxi_novel.knowledge.query_materials import query_model_payload


def review(**updates):
    return {
        "review_id": "review-1", "target_item_id": "event-a", "target_version_id": "version-a",
        "candidate_item_id": "event-b", "candidate_version_id": "version-b",
        "relation": "distinct_occurrence", "status": "reviewed_distinct",
        "rationale": "不同发生的详细审校理由。" * 200,
        "semantic_model_id": "bulk-model", "semantic_prompt_hash": "audit-hash",
        **updates,
    }


def test_final_distinct_review_keeps_exact_decision_and_versions_without_mutation():
    original = {"typed_event": {"action_expression": "交付物品", "identity_reviews": [review()]}}
    saved = deepcopy(original)
    projected = query_model_payload(original)
    decision = projected["typed_event"]["identity_reviews"][0]
    assert decision["relation"] == "distinct_occurrence"
    assert decision["status"] == "reviewed_distinct"
    assert decision["target_version_id"] == "version-a"
    assert decision["candidate_version_id"] == "version-b"
    assert decision["review_detail_ref"] == "review-1"
    assert "rationale" not in decision
    assert projected["typed_event"]["action_expression"] == "交付物品"
    assert original == saved


def test_pending_or_same_occurrence_rationale_remains_available_for_judgment():
    for changes in ({"status": "unresolved"}, {"relation": "same_occurrence"}, {"target_version_id": None}):
        value = review(**changes)
        projected = query_model_payload(value)
        assert projected["rationale"] == value["rationale"]
        assert "review_detail_ref" not in projected


def test_different_payloads_reusing_one_review_id_are_not_compacted_or_collapsed():
    first, second = review(), review(rationale="不同的结论依据。")
    projected = query_model_payload([first, second, first])
    assert [row["rationale"] for row in projected] == [first["rationale"], second["rationale"], first["rationale"]]


def test_identical_confirmed_review_is_defined_once_and_referenced_afterwards():
    projected = query_model_payload([review(), review()])
    assert projected[0]["target_item_id"] == "event-a"
    assert projected[1] == {"review_id": "review-1"}
