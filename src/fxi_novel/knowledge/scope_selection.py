"""Shared SQL construction for selecting sources by ID or folder scope."""

import json
from typing import Any


def current_prose_projection_filter(source_alias: str) -> str:
    """Exclude adopted-prose snapshots that no longer match the committed chapter head."""
    metadata = (
        f"CASE WHEN json_valid({source_alias}.document_metadata_json) "
        f"THEN {source_alias}.document_metadata_json ELSE '{{}}' END"
    )
    event_payload = (
        "CASE WHEN json_valid(prose_event.event_json) "
        "THEN prose_event.event_json ELSE '{}' END"
    )
    return f"""
        (
            NOT (
                COALESCE({source_alias}.source_uri, '') LIKE 'prose://%'
                OR json_type({metadata}, '$.prose_projection') IS 'object'
            )
            OR EXISTS (
                SELECT 1
                FROM prose_chapter_head AS prose_head
                JOIN prose_lifecycle_event AS prose_event
                  ON prose_event.event_id = json_extract({metadata}, '$.prose_projection.event_id')
                 AND prose_event.event_type = 'adopted'
                JOIN knowledge_prose_event_receipt AS prose_receipt
                  ON prose_receipt.event_id = prose_event.event_id
                JOIN source_current_snapshot AS prose_current
                  ON prose_current.source_id = {source_alias}.id
                JOIN source_snapshot AS prose_snapshot
                  ON prose_snapshot.id = prose_current.snapshot_id
                WHERE prose_head.work_id = {source_alias}.work_id
                  AND prose_head.chapter_id = json_extract({metadata}, '$.prose_projection.chapter_id')
                  AND prose_head.head_version = json_extract({metadata}, '$.prose_projection.head_version')
                  AND prose_head.adopted_artifact_id = json_extract({metadata}, '$.prose_projection.artifact_id')
                  AND prose_head.adopted_revision = json_extract({metadata}, '$.prose_projection.revision')
                  AND prose_head.adopted_hash = json_extract({metadata}, '$.prose_projection.content_hash')
                  AND prose_snapshot.content_hash = json_extract({metadata}, '$.prose_projection.content_hash')
                  AND prose_event.work_id = {source_alias}.work_id
                  AND prose_event.chapter_id = prose_head.chapter_id
                  AND prose_event.head_version = prose_head.head_version
                  AND prose_event.artifact_id = prose_head.adopted_artifact_id
                  AND prose_event.revision = prose_head.adopted_revision
                  AND prose_event.content_hash = prose_head.adopted_hash
                  AND prose_receipt.work_id = prose_event.work_id
                  AND prose_receipt.chapter_id = prose_event.chapter_id
                  AND prose_receipt.head_version = prose_event.head_version
                  AND prose_receipt.event_type = 'adopted'
                  AND prose_receipt.artifact_id = prose_event.artifact_id
                  AND prose_receipt.revision = prose_event.revision
                  AND prose_receipt.content_hash = prose_event.content_hash
                  AND json_type({metadata}, '$.prose_projection.branch_id') = 'text'
                  AND length(trim(json_extract({metadata}, '$.prose_projection.branch_id'))) > 0
                  AND length(json_extract({metadata}, '$.prose_projection.branch_id')) <= 120
                  AND json_extract({metadata}, '$.prose_projection.branch_id')
                      NOT GLOB '*[^A-Za-z0-9._:-]*'
                  AND json_extract({event_payload}, '$.branch_id')
                      = json_extract({metadata}, '$.prose_projection.branch_id')
                  AND json_extract({event_payload}, '$.current_head.branch_id')
                      = json_extract({metadata}, '$.prose_projection.branch_id')
                  AND json_extract({event_payload}, '$.current_head.work_id') = prose_event.work_id
                  AND json_extract({event_payload}, '$.current_head.chapter_id') = prose_event.chapter_id
                  AND json_extract({event_payload}, '$.current_head.head_version') = prose_event.head_version
                  AND json_extract({event_payload}, '$.current_head.adopted_artifact_id') = prose_event.artifact_id
                  AND json_extract({event_payload}, '$.current_head.adopted_revision') = prose_event.revision
                  AND json_extract({event_payload}, '$.current_head.adopted_hash') = prose_event.content_hash
                  AND (
                      SELECT COUNT(*) FROM prose_lifecycle_event AS unique_event
                      WHERE unique_event.work_id = prose_event.work_id
                        AND unique_event.chapter_id = prose_event.chapter_id
                        AND unique_event.head_version = prose_event.head_version
                        AND unique_event.event_type = 'adopted'
                  ) = 1
            )
        )
    """


def current_graph_candidate_evidence_filter(
    item_id_expression: str,
    version_id_expression: str,
    *,
    source_scope_predicate: str | None = None,
) -> str:
    """Require a graph candidate to retain at least one current, readable citation.

    The expressions and optional predicate are internal SQL fragments supplied by
    repository queries. Candidate evidence is historical by design, so readers must
    bind the evidence span back to the source's current snapshot before treating the
    candidate as current. CROSS JOIN keeps this correlated lookup anchored at the
    candidate/version index instead of scanning every source in the same space.
    """
    scope_predicate = (
        f" AND ({source_scope_predicate})" if source_scope_predicate else ""
    )
    return f"""
        EXISTS (
            SELECT 1
            FROM knowledge_card_candidate_evidence AS current_evidence
            CROSS JOIN evidence_span AS current_span
              ON current_span.id = current_evidence.evidence_span_id
            CROSS JOIN source_snapshot AS current_snapshot
              ON current_snapshot.id = current_span.snapshot_id
             AND current_snapshot.source_id = current_evidence.source_id
            CROSS JOIN source AS current_source
              ON current_source.id = current_evidence.source_id
            CROSS JOIN knowledge_item AS current_candidate
              ON current_candidate.id = current_evidence.candidate_item_id
             AND current_candidate.space = current_source.space
             AND current_candidate.work_id IS current_source.work_id
            CROSS JOIN knowledge_version AS current_version
              ON current_version.id = current_evidence.candidate_version_id
             AND current_version.item_id = current_candidate.id
            CROSS JOIN source_current_snapshot AS current_pointer
              ON current_pointer.source_id = current_source.id
             AND current_pointer.snapshot_id = current_snapshot.id
            WHERE current_evidence.candidate_item_id = {item_id_expression}
              AND current_evidence.candidate_version_id = {version_id_expression}
              AND current_source.deleted_at IS NULL
              AND {current_prose_projection_filter('current_source')}
              AND (
                  NOT (
                       COALESCE(current_source.source_uri, '') LIKE 'prose://%'
                      OR json_type(
                          CASE WHEN json_valid(current_source.document_metadata_json)
                               THEN current_source.document_metadata_json ELSE '{{}}' END,
                          '$.prose_projection'
                      ) IS 'object'
                  )
                  OR json_extract(current_version.content_json, '$.branch_id')
                     = json_extract(
                         CASE WHEN json_valid(current_source.document_metadata_json)
                              THEN current_source.document_metadata_json ELSE '{{}}' END,
                         '$.prose_projection.branch_id'
                     )
              )
              {scope_predicate}
        )
    """


def current_version_evidence_filter(
    item_id_expression: str,
    version_id_expression: str,
    *,
    source_scope_predicate: str | None = None,
) -> str:
    """Require current citations, starting at the indexed version's evidence.

    SQLite may otherwise reorder these joins to start with all same-space sources
    for each item. The explicit join order retains every freshness/scope predicate.
    """
    scope_predicate = (
        f" AND ({source_scope_predicate})" if source_scope_predicate else ""
    )
    return f"""
        EXISTS (
            SELECT 1
            FROM knowledge_version_evidence AS current_version_evidence
            CROSS JOIN evidence_span AS current_span
              ON current_span.id = current_version_evidence.evidence_span_id
            CROSS JOIN source_snapshot AS current_snapshot
              ON current_snapshot.id = current_span.snapshot_id
            CROSS JOIN source AS current_source
              ON current_source.id = current_snapshot.source_id
            CROSS JOIN source_current_snapshot AS current_pointer
              ON current_pointer.source_id = current_source.id
             AND current_pointer.snapshot_id = current_snapshot.id
            CROSS JOIN knowledge_version AS current_version
              ON current_version.id = current_version_evidence.knowledge_version_id
             AND current_version.item_id = {item_id_expression}
            CROSS JOIN knowledge_item AS current_item
              ON current_item.id = current_version.item_id
             AND current_item.space = current_source.space
             AND current_item.work_id IS current_source.work_id
            WHERE current_version_evidence.knowledge_version_id = {version_id_expression}
              AND current_item.id = {item_id_expression}
              AND current_source.deleted_at IS NULL
              AND {current_prose_projection_filter('current_source')}
              AND (
                  NOT (
                      COALESCE(current_source.source_uri, '') LIKE 'prose://%'
                      OR json_type(
                          CASE WHEN json_valid(current_source.document_metadata_json)
                               THEN current_source.document_metadata_json ELSE '{{}}' END,
                          '$.prose_projection'
                      ) IS 'object'
                  )
                  OR json_extract(current_version.content_json, '$.branch_id')
                     = json_extract(
                         CASE WHEN json_valid(current_source.document_metadata_json)
                              THEN current_source.document_metadata_json ELSE '{{}}' END,
                         '$.prose_projection.branch_id'
                     )
              )
              {scope_predicate}
        )
    """


def source_selection_filter(
    source_alias: str,
    source_ids: list[str] | None,
    folder_ids: list[str] | None,
    parameters: list[Any],
) -> str:
    """Build source ID/folder scope plus a current adopted-prose-head check."""
    alternatives: list[str] = []
    if source_ids:
        alternatives.append(
            f"{source_alias}.id IN ("
            "SELECT CAST(selected_source.value AS TEXT) "
            "FROM json_each(?) AS selected_source)"
        )
        parameters.append(json.dumps(source_ids, ensure_ascii=False, separators=(",", ":")))
    if folder_ids:
        alternatives.append(
            f"EXISTS ("
            "SELECT 1 FROM knowledge_folder AS selected_folder "
            "JOIN knowledge_folder AS source_folder "
            "ON source_folder.space = selected_folder.space "
            "AND source_folder.work_id IS selected_folder.work_id "
            f"WHERE selected_folder.id IN ({', '.join('?' for _ in folder_ids)}) "
            f"AND selected_folder.space = {source_alias}.space "
            f"AND selected_folder.work_id IS {source_alias}.work_id "
            f"AND {source_alias}.folder_id = source_folder.id "
            "AND (source_folder.path = selected_folder.path COLLATE NOCASE "
            "OR substr(source_folder.path, 1, length(selected_folder.path) + 1) "
            "= selected_folder.path || '/' COLLATE NOCASE)"
            ")"
        )
        parameters.extend(folder_ids)
    predicates: list[str] = []
    if alternatives:
        predicates.append("(" + " OR ".join(alternatives) + ")")
    elif source_ids is not None or folder_ids is not None:
        predicates.append("0")
    predicates.append(current_prose_projection_filter(source_alias))
    return " AND " + " AND ".join(predicates)


def all_version_source_dependencies_filter(
    version_id_expression: str,
    source_ids: list[str],
    parameters: list[Any],
) -> str:
    """Require every declared dependency to fit an exact pre-model source scope.

    This opt-in predicate is for isolated writing reads. Ordinary browsing keeps
    its existing any-citation source matching. It checks all original evidence,
    the parse origin, and registered knowledge/prose projection dependencies,
    recursively. Empty authorization is false, never an unfiltered query.
    Internal SQL expressions must come from repository code, never user input.
    """
    if not isinstance(source_ids, list) or any(not isinstance(item, str) or not item for item in source_ids):
        raise ValueError("strict source dependencies require explicit source IDs")
    if not source_ids:
        return "0"
    parameters.append(json.dumps(sorted(set(source_ids)), ensure_ascii=False, separators=(",", ":")))
    current_projection = current_prose_projection_filter("dependency_source")
    artifact_projection = current_prose_projection_filter("artifact_source")
    return f"""
        NOT EXISTS (
            WITH RECURSIVE
            allowed_sources(id) AS (SELECT CAST(value AS TEXT) FROM json_each(?)),
            required_versions(id) AS (
                SELECT {version_id_expression}
                UNION
                SELECT dependency.knowledge_version_id
                FROM required_versions AS required
                JOIN knowledge_version AS required_version ON required_version.id = required.id
                JOIN knowledge_projection_dependency AS dependency
                  ON dependency.consumer_kind = 'knowledge_item'
                 AND dependency.consumer_id = required_version.item_id
                 AND dependency.consumer_revision IN (required_version.id, CAST(required_version.version AS TEXT))
                 AND dependency.upstream_kind = 'knowledge_version'
            )
            SELECT 1 FROM required_versions AS required
            LEFT JOIN knowledge_version AS required_version ON required_version.id = required.id
            LEFT JOIN knowledge_item AS required_item ON required_item.id = required_version.item_id
            WHERE required_version.id IS NULL OR required_item.status IN ('deleted','rejected','superseded')
               OR NOT EXISTS (
                   SELECT 1 FROM knowledge_version_evidence AS basis
                   WHERE basis.knowledge_version_id = required.id
               )
               OR EXISTS (
                   SELECT 1 FROM knowledge_version_evidence AS basis
                   LEFT JOIN evidence_span AS dependency_span ON dependency_span.id = basis.evidence_span_id
                   LEFT JOIN source_snapshot AS dependency_snapshot ON dependency_snapshot.id = dependency_span.snapshot_id
                   LEFT JOIN source AS dependency_source ON dependency_source.id = dependency_snapshot.source_id
                   LEFT JOIN source_current_snapshot AS dependency_current ON dependency_current.source_id = dependency_source.id
                   WHERE basis.knowledge_version_id = required.id
                     AND (dependency_source.id IS NULL OR dependency_source.deleted_at IS NOT NULL
                       OR dependency_source.space != required_item.space
                       OR dependency_source.work_id IS NOT required_item.work_id
                       OR dependency_source.id NOT IN (SELECT id FROM allowed_sources)
                       OR dependency_current.snapshot_id IS NOT dependency_snapshot.id
                       OR NOT ({current_projection}))
               )
               OR (required_item.origin_parse_receipt_id IS NOT NULL AND NOT EXISTS (
                   SELECT 1 FROM knowledge_parse_receipt AS origin
                   JOIN source_current_snapshot AS origin_current ON origin_current.source_id = origin.source_id
                   WHERE origin.id = required_item.origin_parse_receipt_id
                     AND origin.source_id IN (SELECT id FROM allowed_sources)
                     AND origin.snapshot_id = origin_current.snapshot_id
               ))
               OR EXISTS (
                   SELECT 1 FROM knowledge_projection_dependency AS dependency
                   WHERE dependency.consumer_kind = 'knowledge_item'
                     AND dependency.consumer_id = required_version.item_id
                     AND dependency.consumer_revision IN (required_version.id, CAST(required_version.version AS TEXT))
                     AND (dependency.status != 'current' OR
                       (dependency.upstream_kind = 'prose_artifact' AND NOT EXISTS (
                           SELECT 1 FROM source AS artifact_source
                           WHERE artifact_source.id IN (SELECT id FROM allowed_sources)
                             AND artifact_source.space = 'writing'
                             AND artifact_source.work_id = dependency.work_id
                             AND artifact_source.deleted_at IS NULL
                             AND json_extract(artifact_source.document_metadata_json, '$.prose_projection.branch_id') = dependency.branch_id
                             AND json_extract(artifact_source.document_metadata_json, '$.prose_projection.artifact_id') = dependency.artifact_id
                             AND json_extract(artifact_source.document_metadata_json, '$.prose_projection.revision') = dependency.artifact_revision
                             AND json_extract(artifact_source.document_metadata_json, '$.prose_projection.content_hash') = dependency.artifact_hash
                             AND {artifact_projection}
                       )))
               )
        )
    """
