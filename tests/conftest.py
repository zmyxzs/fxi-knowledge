"""Known pre-existing failures, registered as strict xfail.

These 11 cases fail identically in the source project (``D:\\Projects\\Fxi-novel``)
with the same test bodies, so the extraction did not introduce them. Keeping
them as ``xfail(strict=True)`` gives ``pytest -q`` a clean baseline and turns an
accidental fix into a loud ``XPASS`` instead of a silent pass.

Nine cases registered here first were later repaired and removed: the purge
batching fixture, the coverage-delta segment fixture, the row-repair runner's
nullable schema, the event-resolution monkeypatch target, the material-sharing
message assertion, the embedding receipt counter assertion, the two v81
backfill-repair cases (which set user_version backwards on an already-migrated
database and re-ran every later migration), and the parse-generation cleanup
fixture (which still wrote the pre-``category`` candidate content shape).

Two more were deleted because they only exercised upgrades from v81/v82
databases: this checkout does not support old databases, and the current
``add_text_source`` needs ``source.edit_revision`` (added in v97).

Maintenance rules:

* A case that starts passing must be removed from ``_KNOWN_FAILURES`` (strict
  xfail reports it as ``XPASS`` otherwise) and README's baseline updated.
* A case that is renamed or deleted must have its entry updated too; a stale
  entry simply stops matching and the new test fails normally.
"""

from __future__ import annotations

import pytest


_KNOWN_FAILURES: dict[str, str] = {
    # Parse / publication pipeline.
    "tests/test_graph_parser_identity_recovery.py::test_reuses_identity_when_event_groups_pass_and_failure_only_hits_some_sources": (
        "identity recovery does not reuse the identity for partially failed sources"
    ),
    "tests/test_graph_parser_identity_recovery.py::test_compact_wire_merges_need_not_equal_the_canonical_audit_partition": (
        "compact wire merges differ from the canonical audit partition"
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
