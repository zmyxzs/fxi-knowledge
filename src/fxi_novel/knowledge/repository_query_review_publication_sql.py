"""Publication predicates for exact query-reviewed candidate receipts."""

from __future__ import annotations

import re

from .scope_selection import current_prose_projection_filter


def query_reviewed_claim_tables_exist(connection) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='knowledge_query_reviewed_claim_publication'"
    ).fetchone() is not None


def query_review_pin_scope_membership_sql(
    pin_alias: str = "query_pin", source_alias: str = "source",
) -> str:
    """Return current registration proof for one physical source and scope pin."""
    if any(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", alias) is None
           for alias in (pin_alias, source_alias)):
        raise ValueError("SQL aliases must be simple identifiers")
    return f"""(
        json_extract({pin_alias}.value, '$.logical_scope_work_id') IS NULL
        OR {source_alias}.space = 'learning' AND (
            {source_alias}.work_id IS json_extract(
                {pin_alias}.value, '$.logical_scope_work_id'
            )
            AND EXISTS (
                SELECT 1 FROM knowledge_project AS physical_learning
                WHERE physical_learning.id = {source_alias}.work_id
                  AND physical_learning.space = 'learning'
                  AND physical_learning.status = 'active'
            )
            OR EXISTS (
                SELECT 1
                FROM knowledge_project AS logical_learning
                JOIN knowledge_learning_work_folder AS assigned
                  ON assigned.work_id = logical_learning.id
                JOIN knowledge_folder AS root_folder
                  ON root_folder.id = assigned.folder_id
                JOIN knowledge_folder AS source_folder
                  ON source_folder.id = {source_alias}.folder_id
                 AND source_folder.space = {source_alias}.space
                 AND source_folder.work_id IS {source_alias}.work_id
                WHERE logical_learning.id = json_extract(
                          {pin_alias}.value, '$.logical_scope_work_id'
                      )
                  AND logical_learning.space = 'learning'
                  AND logical_learning.status = 'active'
                  AND root_folder.work_id IS source_folder.work_id
                  AND (source_folder.path = root_folder.path COLLATE NOCASE
                    OR substr(source_folder.path, 1, length(root_folder.path) + 1)
                       = root_folder.path || '/' COLLATE NOCASE)
            )
            OR EXISTS (
                SELECT 1
                FROM knowledge_project AS logical_writing
                JOIN writing_learning_source_reference AS explicit_source
                  ON explicit_source.work_id = logical_writing.id
                 AND explicit_source.source_id = {source_alias}.id
                WHERE logical_writing.id = json_extract(
                          {pin_alias}.value, '$.logical_scope_work_id'
                      )
                  AND logical_writing.space = 'writing'
                  AND logical_writing.status = 'active'
            )
            OR EXISTS (
                SELECT 1
                FROM knowledge_project AS logical_writing
                JOIN writing_learning_scope_reference AS learning_scope
                  ON learning_scope.work_id = logical_writing.id
                 AND learning_scope.learning_work_id IS {source_alias}.work_id
                JOIN knowledge_folder AS root_folder ON root_folder.id = learning_scope.folder_id
                JOIN knowledge_folder AS source_folder
                  ON source_folder.id = {source_alias}.folder_id
                 AND source_folder.space = {source_alias}.space
                 AND source_folder.work_id IS {source_alias}.work_id
                WHERE logical_writing.id = json_extract(
                          {pin_alias}.value, '$.logical_scope_work_id'
                      )
                  AND logical_writing.space = 'writing'
                  AND logical_writing.status = 'active'
                  AND root_folder.work_id IS source_folder.work_id
                  AND (source_folder.path = root_folder.path COLLATE NOCASE
                    OR substr(source_folder.path, 1, length(root_folder.path) + 1)
                       = root_folder.path || '/' COLLATE NOCASE)
            )
            OR EXISTS (
                SELECT 1
                FROM writing_base_work_reference AS base_reference
                JOIN writing_base_source_reference AS base_source
                  ON base_source.work_id = base_reference.work_id
                 AND base_source.source_id = {source_alias}.id
                JOIN knowledge_project AS logical_writing
                  ON logical_writing.id = base_reference.work_id
                 AND logical_writing.space = 'writing'
                 AND logical_writing.status = 'active'
                WHERE base_reference.work_id = json_extract(
                          {pin_alias}.value, '$.logical_scope_work_id'
                      )
                  AND base_reference.base_space = {source_alias}.space
                  AND base_reference.base_work_id IS {source_alias}.work_id
            )
            OR EXISTS (
                SELECT 1
                FROM writing_base_work_reference AS base_reference
                JOIN writing_base_scope_reference AS base_scope
                  ON base_scope.work_id = base_reference.work_id
                JOIN knowledge_folder AS root_folder ON root_folder.id = base_scope.folder_id
                JOIN knowledge_folder AS source_folder
                  ON source_folder.id = {source_alias}.folder_id
                 AND source_folder.space = {source_alias}.space
                 AND source_folder.work_id IS {source_alias}.work_id
                JOIN knowledge_project AS logical_writing
                  ON logical_writing.id = base_reference.work_id
                 AND logical_writing.space = 'writing'
                 AND logical_writing.status = 'active'
                WHERE base_reference.work_id = json_extract(
                          {pin_alias}.value, '$.logical_scope_work_id'
                      )
                  AND base_reference.base_space = {source_alias}.space
                  AND base_reference.base_work_id IS {source_alias}.work_id
                  AND root_folder.work_id IS source_folder.work_id
                  AND (source_folder.path = root_folder.path COLLATE NOCASE
                    OR substr(source_folder.path, 1, length(root_folder.path) + 1)
                       = root_folder.path || '/' COLLATE NOCASE)
            )
        )
        OR {source_alias}.space = 'writing' AND EXISTS (
            SELECT 1 FROM knowledge_project AS logical_writing
            WHERE logical_writing.id = json_extract(
                      {pin_alias}.value, '$.logical_scope_work_id'
                  )
              AND logical_writing.space = 'writing'
              AND logical_writing.status = 'active'
              AND logical_writing.id = {source_alias}.work_id
        )
    )"""


def query_reviewed_claim_candidate_gate(item_alias: str = "item") -> str:
    """Require an exact persisted receipt and every current, still-authorized quote."""
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", item_alias) is None:
        raise ValueError("SQL alias must be a simple identifier")
    pin_scope_membership = query_review_pin_scope_membership_sql()
    current_ref = f"""EXISTS (
        SELECT 1
        FROM knowledge_query_reviewed_claim_publication AS query_publication
        JOIN knowledge_query_review_receipt AS query_receipt
          ON query_receipt.id = query_publication.receipt_id
         AND query_receipt.status = 'published'
        JOIN knowledge_version AS query_version
          ON query_version.id = query_publication.candidate_version_id
         AND query_version.item_id = query_publication.candidate_item_id
        WHERE query_publication.candidate_item_id = {item_alias}.id
          AND query_publication.candidate_version_id = {item_alias}.published_graph_version_id
          AND query_publication.space = {item_alias}.space
          AND query_publication.work_id IS {item_alias}.work_id
          AND query_publication.branch_id IS json_extract(
              query_version.content_json, '$.branch_id'
          )
          AND {item_alias}.status NOT IN ('deleted', 'rejected', 'superseded')
          AND json_type(query_version.content_json, '$.query_learning_claim') = 'object'
          AND json(json_remove(
                json_extract(query_version.content_json, '$.query_learning_claim'),
                '$.requirement_id', '$.evidence_refs'
              ))
              = json(json_remove(query_publication.claim_json, '$.requirement_id', '$.evidence_refs'))
          AND json(query_publication.claim_json) = json(json_extract(
              query_receipt.proof_json,
              '$.claims[' || query_publication.claim_ordinal || ']'
          ))
          AND json(json_extract(query_publication.claim_json, '$.evidence_refs'))
              = json(query_publication.evidence_refs_json)
          AND json_extract(query_publication.claim_json, '$.requirement_id') IN (
              SELECT value FROM json_each(query_receipt.requirement_ids_json)
          )
          AND NOT EXISTS (
              SELECT 1
              FROM json_each(query_publication.evidence_refs_json) AS query_ref
              WHERE NOT EXISTS (
                  SELECT 1
                  FROM source AS source
                  JOIN source_current_snapshot AS current
                    ON current.source_id = source.id
                   AND current.snapshot_id = json_extract(query_ref.value, '$.snapshot_id')
                  JOIN source_snapshot AS snapshot
                    ON snapshot.id = current.snapshot_id
                   AND snapshot.source_id = source.id
                  WHERE source.id = json_extract(query_ref.value, '$.source_id')
                    AND source.deleted_at IS NULL
                    AND source.space = json_extract(query_ref.value, '$.space')
                    AND source.work_id IS json_extract(query_ref.value, '$.work_id')
                    AND snapshot.content_hash = json_extract(query_ref.value, '$.content_hash')
                    AND json_extract(query_ref.value, '$.end_offset')
                        - json_extract(query_ref.value, '$.start_offset')
                        = length(json_extract(query_ref.value, '$.quote'))
                    AND substr(snapshot.content,
                               json_extract(query_ref.value, '$.start_offset') + 1,
                               json_extract(query_ref.value, '$.end_offset')
                                 - json_extract(query_ref.value, '$.start_offset'))
                        = json_extract(query_ref.value, '$.quote')
                    AND {current_prose_projection_filter('source')}
                    AND (
                        NOT (
                            COALESCE(source.source_uri, '') LIKE 'prose://%'
                            OR json_type(
                                CASE WHEN json_valid(source.document_metadata_json)
                                     THEN source.document_metadata_json ELSE '{{}}' END,
                                '$.prose_projection'
                            ) IS 'object'
                        )
                        OR json_extract(
                            CASE WHEN json_valid(source.document_metadata_json)
                                 THEN source.document_metadata_json ELSE '{{}}' END,
                            '$.prose_projection.branch_id'
                        ) = json_extract(query_ref.value, '$.branch_id')
                    )
                    AND EXISTS (
                        SELECT 1 FROM json_each(query_receipt.scope_pins_json) AS query_pin
                        WHERE json_extract(query_pin.value, '$.space') = source.space
                          AND json_extract(query_pin.value, '$.work_id') IS source.work_id
                          AND json_extract(query_pin.value, '$.branch_id')
                              IS json_extract(query_ref.value, '$.branch_id')
                          AND EXISTS (
                              SELECT 1 FROM json_each(
                                  CASE WHEN json_type(query_pin.value, '$.source_ids') = 'array'
                                       THEN json_extract(query_pin.value, '$.source_ids')
                                       ELSE '[]' END
                              ) AS selected_source
                              WHERE selected_source.value = source.id
                          )
                          AND {pin_scope_membership}
                    )
              )
          )
          AND EXISTS (
              SELECT 1 FROM json_each(query_publication.evidence_refs_json)
          )
    )"""
    return current_ref
