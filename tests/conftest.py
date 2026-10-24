"""Known pre-existing failures, registered as strict xfail.

These 16 cases fail identically in the source project (``D:\\Projects\\Fxi-novel``)
with the same test bodies, so the extraction did not introduce them. Keeping
them as ``xfail(strict=True)`` gives ``pytest -q`` a clean baseline and turns an
accidental fix into a loud ``XPASS`` instead of a silent pass.

Six cases registered here first were later repaired and removed: the purge
batching fixture (missing column and tables), the coverage-delta segment
fixture, the row-repair runner's nullable schema, the event-resolution
monkeypatch target, the material-sharing message assertion, and the embedding
receipt counter assertion.

Maintenance rules:

* A case that starts passing must be removed from ``_KNOWN_FAILURES`` (strict
  xfail reports it as ``XPASS`` otherwise) and README's baseline updated.
* A case that is renamed or deleted must have its entry updated too; a stale
  entry simply stops matching and the new test fails normally.
"""

from __future__ import annotations

import pytest


_KNOWN_FAILURES: dict[str, str] = {
    # Migration / legacy database compatibility.
    "tests/test_dialogue_partial_resume.py::test_revision_migration_preserves_legacy_ids_quotes_fts_and_proofs": (
        "legacy dialogue revision migration lacks the edit_revision column"
    ),
    "tests/test_embedding_candidate_migration.py::test_v81_additive_repair_preserves_jobs_and_existing_backfill[True]": (
        "migration fixture lacks the receipt-generation uniqueness the repair requires"
    ),
    "tests/test_embedding_candidate_migration.py::test_v81_additive_repair_preserves_jobs_and_existing_backfill[False]": (
        "migration fixture lacks the receipt-generation uniqueness the repair requires"
    ),
    "tests/test_published_graph_version_pointer.py::test_v82_to_83_restores_exact_checkpoint_version_before_newer_draft": (
        "v82 to v83 upgrade restores the wrong checkpoint version"
    ),
    # Parse / publication pipeline.
    "tests/test_graph_parser_identity_recovery.py::test_reuses_identity_when_event_groups_pass_and_failure_only_hits_some_sources": (
        "identity recovery does not reuse the identity for partially failed sources"
    ),
    "tests/test_graph_parser_identity_recovery.py::test_compact_wire_merges_need_not_equal_the_canonical_audit_partition": (
        "compact wire merges differ from the canonical audit partition"
    ),
    "tests/test_knowledge_graph_parse_cleanup.py::ParseGenerationCleanupTests::test_prior_parse_candidates_remain_until_replacement_is_published": (
        "prior parse candidates disappear before the replacement is published"
    ),
    "tests/test_parse_replacement.py::D1ParseReplacementTests::test_replacement_copies_exact_dependency_and_preserves_human_history": (
        "parse replacement is blocked by a protected fact relation index mismatch"
    ),
    # Query / learning behaviour.
    "tests/test_association_entity_source_scope.py::RetrievalSearchStableScopeTests::test_explicit_source_and_folder_selectors_keep_or_semantics": (
        "explicit source/folder selectors do not keep OR semantics"
    ),
    "tests/test_retrieval_search_scope.py::RetrievalSearchStableScopeTests::test_explicit_source_and_folder_selectors_keep_or_semantics": (
        "explicit source/folder selectors do not keep OR semantics"
    ),
    "tests/test_planning_study_reading.py::test_saved_legacy_request_reuses_owner_and_current_key_takes_precedence[False]": (
        "legacy planning request owner/key precedence is inverted"
    ),
    "tests/test_query_equivalent_operations.py::test_cache_compilation_contract_keeps_old_duplicate_learning_plans_separate[writing]": (
        "cache compilation keeps old duplicate learning plans in one bucket"
    ),
    "tests/test_query_grounded_scene_read.py::test_long_discovery_evidence_still_obeys_scene_window_budget": (
        "long discovery evidence exceeds the scene window budget"
    ),
    "tests/test_query_material_sharing.py::test_shared_text_does_not_allow_foreign_candidate_decisions": (
        "shared text still allows foreign candidate decisions"
    ),
    "tests/test_query_register_observations.py::test_execution_cache_contract_only_changes_for_manual_observation_scope[learning-canon_fact-None]": (
        "execution cache contract changes outside manual observation scope"
    ),
    "tests/test_query_register_observations.py::test_execution_cache_contract_only_changes_for_manual_observation_scope[learning-learning_method-None]": (
        "execution cache contract changes outside manual observation scope"
    ),
}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        reason = _KNOWN_FAILURES.get(item.nodeid)
        if reason is not None:
            item.add_marker(pytest.mark.xfail(strict=True, reason=reason))
