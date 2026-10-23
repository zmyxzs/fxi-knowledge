"""Publication eligibility for graph candidates backed by parser receipts."""

from __future__ import annotations

import re

from .parse_candidate_qualification import extraction_ready_sql, qualified_candidate_sql
from .parse_publication_checkpoint import published_checkpoint_sql
from .scope_selection import current_prose_projection_filter
from .repository_query_review_publication_sql import query_reviewed_claim_candidate_gate


def _published_parse_receipt_gate(
    receipt_alias: str,
    candidate_alias: str,
    batch_item_alias: str,
    batch_alias: str,
    *,
    allow_checkpoint: bool = True,
) -> str:
    """Return the candidate-specific qualification and publication-stage gate."""
    aliases = (receipt_alias, candidate_alias, batch_item_alias, batch_alias)
    if any(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", alias) is None for alias in aliases):
        raise ValueError("SQL aliases must be simple identifiers")
    empty_metadata_json = "'{}'"
    current = f"""(
        {qualified_candidate_sql(receipt_alias, candidate_alias)}
        AND {batch_alias}.space = {receipt_alias}.space
        AND {batch_alias}.work_id IS {receipt_alias}.work_id
        AND {batch_alias}.branch_id IS {receipt_alias}.branch_id
        AND {receipt_alias}.parse_batch_id = {batch_alias}.id
        AND json_extract({batch_item_alias}.result_json, '$.receipt_id') = {receipt_alias}.id
        AND json_extract({batch_item_alias}.result_json, '$.generation') = {receipt_alias}.generation
        AND {batch_item_alias}.status IN ('completed', 'reused')
        AND json_extract({batch_item_alias}.stages_json, '$.check.status') = 'completed'
        AND json_extract({batch_item_alias}.stages_json, '$.resolution.status') = 'completed'
        AND json_extract({batch_item_alias}.stages_json, '$.publish.status') = 'completed'
        AND {receipt_alias}.parser_version = json_extract(
            {batch_item_alias}.stages_json, '$.publish.result.parser_version'
        )
        AND EXISTS (
            SELECT 1
            FROM source AS publication_source
            JOIN source_current_snapshot AS publication_current
              ON publication_current.source_id = publication_source.id
             AND publication_current.snapshot_id = {receipt_alias}.snapshot_id
            JOIN source_snapshot AS publication_snapshot
              ON publication_snapshot.id = publication_current.snapshot_id
             AND publication_snapshot.source_id = publication_source.id
            WHERE publication_source.id = {receipt_alias}.source_id
              AND publication_source.deleted_at IS NULL
              AND publication_source.space = {receipt_alias}.space
              AND publication_source.work_id IS {receipt_alias}.work_id
              AND {current_prose_projection_filter('publication_source')}
              AND (
                  NOT (
                      COALESCE(publication_source.source_uri, '') LIKE 'prose://%'
                      OR json_type(
                          CASE WHEN json_valid(publication_source.document_metadata_json)
                               THEN publication_source.document_metadata_json ELSE {empty_metadata_json} END,
                          '$.prose_projection'
                      ) IS 'object'
                  )
                  OR json_extract(
                      CASE WHEN json_valid(publication_source.document_metadata_json)
                           THEN publication_source.document_metadata_json ELSE {empty_metadata_json} END,
                      '$.prose_projection.branch_id'
                  ) = {receipt_alias}.branch_id
              )
        )
    )"""
    if not allow_checkpoint:
        return current
    previous = published_checkpoint_sql(
        receipt_alias, batch_item_alias, batch_alias, candidate_alias,
    )
    return f"({current} OR {previous})"


def published_graph_candidate_version_join(
    version_alias: str = "version", item_alias: str = "item",
) -> str:
    """Select the exact domain-owner-published immutable candidate version."""
    if any(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", alias) is None
           for alias in (version_alias, item_alias)):
        raise ValueError("SQL aliases must be simple identifiers")
    return (
        f"{version_alias}.id = {item_alias}.published_graph_version_id "
        f"AND {version_alias}.item_id = {item_alias}.id"
    )


def pin_published_graph_candidate_version(
    connection, item_id: str, version_id: str,
) -> None:
    """Apply an owner-authorized exact publication pin in the caller's transaction.

    This helper validates candidate/version ownership and lifecycle only. The
    parse publication owner remains responsible for deciding that the version
    passed all review and scope gates before invoking it. A changed pin also
    invalidates dependents of the exact previously published version.
    """
    previous = connection.execute(
        "SELECT published_graph_version_id FROM knowledge_item "
        "WHERE id = ? AND kind = 'knowledge_graph_candidate'",
        (item_id,),
    ).fetchone()
    previous_version_id = (
        str(previous["published_graph_version_id"])
        if previous is not None and previous["published_graph_version_id"] is not None
        else None
    )
    cursor = connection.execute(
        """UPDATE knowledge_item
           SET published_graph_version_id = ?
           WHERE id = ? AND kind = 'knowledge_graph_candidate'
             AND status NOT IN ('deleted', 'rejected', 'superseded')
             AND EXISTS (
                 SELECT 1 FROM knowledge_version AS version
                 WHERE version.id = ? AND version.item_id = knowledge_item.id
             )""",
        (version_id, item_id, version_id),
    )
    if cursor.rowcount != 1:
        raise ValueError("candidate changed before its published version could be switched")
    if previous_version_id is not None and previous_version_id != version_id:
        from .repository_knowledge_dependencies import KnowledgeDependencyRepositoryMixin

        KnowledgeDependencyRepositoryMixin._invalidate_dependency_versions_in_connection(
            connection,
            knowledge_version_ids=(previous_version_id,),
            reason="knowledge_version_replaced",
        )


def _reviewed_claim_candidate_gate(item_alias: str) -> str:
    """Read immutable publication proofs with current source and version guards.

    The insert owner validates live completed check units before publishing.
    A later retry can replace those mutable units without retracting an already
    published version; its immutable proof remains the publication authority.
    """
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", item_alias) is None:
        raise ValueError("SQL aliases must be simple identifiers")
    return f"""EXISTS (
        SELECT 1
        FROM knowledge_reviewed_claim_publication AS reviewed_claim
        JOIN knowledge_parse_batch_item AS reviewed_item
          ON reviewed_item.batch_id = reviewed_claim.batch_id
         AND reviewed_item.ordinal = reviewed_claim.ordinal
         AND reviewed_item.source_id = reviewed_claim.source_id
         AND reviewed_item.snapshot_id = reviewed_claim.snapshot_id
        JOIN knowledge_parse_batch AS reviewed_batch
          ON reviewed_batch.id = reviewed_claim.batch_id
        JOIN knowledge_parse_receipt AS reviewed_receipt
          ON reviewed_receipt.id = reviewed_claim.parse_receipt_id
        JOIN source AS reviewed_source ON reviewed_source.id = reviewed_claim.source_id
        JOIN source_current_snapshot AS reviewed_current
          ON reviewed_current.source_id = reviewed_source.id
         AND reviewed_current.snapshot_id = reviewed_claim.snapshot_id
        JOIN source_snapshot AS reviewed_snapshot
          ON reviewed_snapshot.id = reviewed_current.snapshot_id
         AND reviewed_snapshot.source_id = reviewed_source.id
        WHERE reviewed_claim.candidate_item_id = {item_alias}.id
          AND reviewed_claim.candidate_version_id = {item_alias}.published_graph_version_id
          AND reviewed_claim.basis = 'reviewed_claim'
          AND {item_alias}.status NOT IN ('deleted', 'rejected', 'superseded')
          AND {item_alias}.origin_parse_receipt_id = reviewed_claim.parse_receipt_id
          AND {item_alias}.space = reviewed_claim.space
          AND {item_alias}.work_id IS reviewed_claim.work_id
          AND {item_alias}.kind = 'knowledge_graph_candidate'
          AND reviewed_claim.space = reviewed_receipt.space
          AND reviewed_claim.work_id IS reviewed_receipt.work_id
          AND reviewed_claim.branch_id IS reviewed_receipt.branch_id
          AND reviewed_claim.source_id = reviewed_receipt.source_id
          AND reviewed_claim.snapshot_id = reviewed_receipt.snapshot_id
          AND reviewed_claim.generation = reviewed_receipt.generation
          AND reviewed_receipt.status = 'completed'
          -- A reused extraction receipt can move to another execution batch.
          -- Its exact, immutable reviewed-claim proof remains authoritative.
          AND reviewed_batch.space = reviewed_claim.space
          AND reviewed_batch.work_id IS reviewed_claim.work_id
          AND reviewed_batch.branch_id IS reviewed_claim.branch_id
          AND reviewed_source.deleted_at IS NULL
          AND reviewed_source.space = reviewed_claim.space
          AND reviewed_source.work_id IS reviewed_claim.work_id
          AND ({current_prose_projection_filter('reviewed_source')})
          AND (
              NOT (
                  COALESCE(reviewed_source.source_uri, '') LIKE 'prose://%'
                  OR json_type(
                      CASE WHEN json_valid(reviewed_source.document_metadata_json)
                           THEN reviewed_source.document_metadata_json ELSE '{{}}' END,
                      '$.prose_projection'
                  ) IS 'object'
              )
              OR json_extract(
                  CASE WHEN json_valid(reviewed_source.document_metadata_json)
                       THEN reviewed_source.document_metadata_json ELSE '{{}}' END,
                  '$.prose_projection.branch_id'
              ) = reviewed_claim.branch_id
          )
          AND ({qualified_candidate_sql('reviewed_receipt', item_alias)})
          AND json_extract(reviewed_item.result_json, '$.receipt_id') = reviewed_claim.parse_receipt_id
          AND json_extract(reviewed_item.result_json, '$.generation') = reviewed_claim.generation
          AND json_extract(reviewed_claim.proof_json, '$.schema_version') = 1
          AND json_extract(reviewed_claim.proof_json, '$.basis') = 'reviewed_claim'
          AND json_extract(reviewed_claim.proof_json, '$.stage') = 'check'
          AND json_extract(reviewed_claim.proof_json, '$.input_hash') = reviewed_claim.check_input_hash
          AND json_extract(reviewed_claim.proof_json, '$.result_hash') = reviewed_claim.check_result_hash
          AND json_extract(reviewed_claim.proof_json, '$.candidate_item_id') = reviewed_claim.candidate_item_id
          AND json_extract(reviewed_claim.proof_json, '$.candidate_version_id') = reviewed_claim.candidate_version_id
          AND EXISTS (
              SELECT 1
              FROM json_each(
                  CASE WHEN json_type(
                      reviewed_claim.proof_json, '$.units'
                  ) = 'array' THEN json_extract(
                      reviewed_claim.proof_json, '$.units'
                  ) ELSE '[]' END
              ) AS checked_unit
              WHERE json_extract(checked_unit.value, '$.source_status') IN ('completed', 'reused')
          )
    )"""


def published_graph_candidate_filter(
    item_alias: str = "item", *, require_version_pointer: bool = True,
    allow_checkpoint: bool = True, include_reviewed_claims: bool = True,
    include_query_reviewed_claims: bool = True,
) -> str:
    """Return SQL that excludes parser drafts until audit and batch publish finish.

    Hand-authored candidates without a parser receipt retain their existing
    lifecycle. Parser-derived candidates require a complete semantic audit and
    the exact batch item to have a completed publish stage. Migration bootstrap
    callers predating reviewed-claim receipts can pass
    ``include_reviewed_claims=False``.
    """
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", item_alias) is None:
        raise ValueError("SQL alias must be a simple identifier")
    gate = _published_parse_receipt_gate(
        "publication_receipt", item_alias, "publication_item", "publication_batch",
        allow_checkpoint=allow_checkpoint,
    )
    pointer = "1"
    if require_version_pointer:
        pointer = f"EXISTS (SELECT 1 FROM knowledge_version AS published_version " \
                  f"WHERE published_version.id = {item_alias}.published_graph_version_id " \
                  f"AND published_version.item_id = {item_alias}.id)"
    # The pointer-free/no-checkpoint form is a legacy bootstrap query used by
    # schema migration 81 before reviewed-claim receipts exist. Keep its
    # original complete-publication-only semantics so a fresh database can
    # execute the historical backfill before migration 89 creates its table.
    reviewed_claim = (
        _reviewed_claim_candidate_gate(item_alias)
        if include_reviewed_claims and (require_version_pointer or allow_checkpoint)
        else "0"
    )
    query_reviewed_claim = (
        query_reviewed_claim_candidate_gate(item_alias)
        if include_query_reviewed_claims and require_version_pointer
        else "0"
    )
    query_claim_basis = f"""EXISTS (
        SELECT 1 FROM knowledge_version AS query_basis_version
        WHERE query_basis_version.id = {item_alias}.published_graph_version_id
          AND query_basis_version.item_id = {item_alias}.id
          AND json_type(query_basis_version.content_json, '$.query_learning_claim') = 'object'
    )"""
    return f"""(
        {item_alias}.status NOT IN ('deleted', 'rejected', 'superseded')
        AND ({pointer})
        AND (
          (({query_claim_basis}) AND ({query_reviewed_claim}))
          OR (NOT ({query_claim_basis}) AND (
            {item_alias}.origin_parse_receipt_id IS NULL
        OR EXISTS (
            SELECT 1
            FROM knowledge_parse_receipt AS publication_receipt
            JOIN knowledge_parse_batch_item AS publication_item
              ON publication_item.source_id = publication_receipt.source_id
             AND publication_item.snapshot_id = publication_receipt.snapshot_id
             AND json_extract(publication_item.result_json, '$.receipt_id')
                 = publication_receipt.id
            JOIN knowledge_parse_batch AS publication_batch
              ON publication_batch.id = publication_item.batch_id
            WHERE publication_receipt.id = {item_alias}.origin_parse_receipt_id
              AND {item_alias}.space = publication_receipt.space
              AND {item_alias}.work_id IS publication_receipt.work_id
              AND {gate}
        )
        OR {reviewed_claim}
          ))
        )
    )"""


def published_parse_receipts_cte(
    source_ids_relation: str = "selected_source_ids",
    source_id_column: str = "id",
) -> str:
    """Return a CTE of publish-eligible receipts for an exact source-ID relation.

    The input relation must expose one source ID column (duplicates are allowed).
    Its row scope is supplied by the caller; this helper applies the same
    receipt/batch/stage gate as ``published_graph_candidate_filter``.
    """
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", source_ids_relation) is None:
        raise ValueError("source ID relation must be a simple SQL identifier")
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", source_id_column) is None:
        raise ValueError("source ID column must be a simple SQL identifier")
    ready = extraction_ready_sql("publication_receipt")
    checkpoint = published_checkpoint_sql(
        "publication_receipt", "publication_item", "publication_batch",
    )
    return f"""published_parse_receipts AS MATERIALIZED (
        SELECT DISTINCT publication_receipt.id AS receipt_id,
               publication_receipt.source_id AS source_id
        FROM {source_ids_relation} AS selected_source
        JOIN knowledge_parse_receipt AS publication_receipt
          ON publication_receipt.source_id = selected_source.{source_id_column}
        JOIN knowledge_parse_batch_item AS publication_item
          ON publication_item.source_id = publication_receipt.source_id
         AND publication_item.snapshot_id = publication_receipt.snapshot_id
         AND json_extract(publication_item.result_json, '$.receipt_id')
             = publication_receipt.id
        JOIN knowledge_parse_batch AS publication_batch
          ON publication_batch.id = publication_item.batch_id
        WHERE ({ready}
          AND publication_batch.space = publication_receipt.space
          AND publication_batch.work_id IS publication_receipt.work_id
          AND publication_batch.branch_id IS publication_receipt.branch_id
          AND publication_receipt.parse_batch_id = publication_batch.id
          AND json_extract(publication_item.result_json, '$.generation') = publication_receipt.generation
          AND publication_item.status IN ('completed', 'reused')
          AND json_extract(publication_item.stages_json, '$.check.status') = 'completed'
          AND json_extract(publication_item.stages_json, '$.resolution.status') = 'completed'
          AND json_extract(publication_item.stages_json, '$.publish.status') = 'completed'
          AND publication_receipt.parser_version = json_extract(
              publication_item.stages_json, '$.publish.result.parser_version'
          )) OR {checkpoint}
        UNION
        SELECT DISTINCT reviewed_claim.parse_receipt_id AS receipt_id,
               reviewed_claim.source_id AS source_id
        FROM {source_ids_relation} AS selected_source
        JOIN knowledge_reviewed_claim_publication AS reviewed_claim
          ON reviewed_claim.source_id = selected_source.{source_id_column}
        JOIN knowledge_item AS reviewed_candidate
          ON reviewed_candidate.id = reviewed_claim.candidate_item_id
         AND reviewed_candidate.published_graph_version_id = reviewed_claim.candidate_version_id
        WHERE {_reviewed_claim_candidate_gate('reviewed_candidate')}
    )"""


def graph_candidate_is_published(connection, item_id: str) -> bool:
    """Check one candidate using the same predicate used by public read paths."""
    eligible = published_graph_candidate_filter("item")
    row = connection.execute(
        f"SELECT {eligible} AS is_published FROM knowledge_item AS item WHERE item.id = ?",
        (item_id,),
    ).fetchone()
    return row is not None and bool(row["is_published"])


_GRAPH_CANDIDATE_CAPABILITIES: dict[str, dict[str, bool]] = {
    "authored": {
        "base_claim": True,
        "original_text": True,
        "semantic_review": True,
        "identity": True,
        "proposition_resolution": True,
        "typed_event": True,
        "typed_quantity": True,
        "typed_rule": True,
        "typed_instance": True,
    },
    "parse_pipeline": {
        "base_claim": True,
        "original_text": True,
        "semantic_review": True,
        "identity": True,
        "proposition_resolution": True,
        "typed_event": True,
        "typed_quantity": True,
        "typed_rule": True,
        "typed_instance": True,
    },
    "reviewed_claim": {
        "base_claim": True,
        "original_text": True,
        "semantic_review": True,
        "identity": False,
        "proposition_resolution": False,
        "typed_event": False,
        "typed_quantity": False,
        "typed_rule": False,
        "typed_instance": False,
        "query_learning_claim": False,
    },
    "query_reviewed_claim": {
        "base_claim": True,
        "original_text": True,
        "semantic_review": False,
        "identity": False,
        "proposition_resolution": False,
        "typed_event": False,
        "typed_quantity": False,
        "typed_rule": False,
        "typed_instance": False,
        "query_learning_claim": True,
    },
}


def graph_candidate_publication_capabilities(basis: str | None) -> dict[str, object]:
    """Return the readable interpretation layers for one publication basis."""
    capabilities = _GRAPH_CANDIDATE_CAPABILITIES.get(basis or "")
    if capabilities is None:
        return {
            "published": False,
            "basis": None,
            "allowed_interpretations": [],
            "capabilities": {key: False for key in next(iter(_GRAPH_CANDIDATE_CAPABILITIES.values()))},
        }
    return {
        "published": True,
        "basis": basis,
        "allowed_interpretations": [
            name for name in (
                "identity", "review", "proposition_resolution", "typed_event",
                "typed_quantity", "typed_rule", "typed_instance",
            )
            if name == "review" or capabilities.get(name, False)
        ],
        "capabilities": dict(capabilities),
    }


def graph_candidate_publication_basis_sql(
    item_alias: str = "item", *, include_query_reviewed_claims: bool = True,
) -> str:
    """Return a scalar SQL expression for the exact published version's basis.

    Use this in both one-item detail reads and batch projections so a large
    vector page does not need an N+1 basis lookup. ``NULL`` means the current
    exact version has no readable publication receipt.
    """
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", item_alias) is None:
        raise ValueError("SQL alias must be a simple identifier")
    full_gate = _published_parse_receipt_gate(
        "basis_receipt", item_alias, "basis_item", "basis_batch",
    )
    query_gate = (
        query_reviewed_claim_candidate_gate(item_alias)
        if include_query_reviewed_claims else "0"
    )
    exact_pointer = f"""EXISTS (
        SELECT 1 FROM knowledge_version AS basis_version
        WHERE basis_version.id = {item_alias}.published_graph_version_id
          AND basis_version.item_id = {item_alias}.id
    )"""
    query_claim_version = f"""EXISTS (
        SELECT 1 FROM knowledge_version AS query_basis_version
        WHERE query_basis_version.id = {item_alias}.published_graph_version_id
          AND query_basis_version.item_id = {item_alias}.id
          AND json_type(query_basis_version.content_json, '$.query_learning_claim') = 'object'
    )""" if include_query_reviewed_claims else "0"
    return f"""CASE
        WHEN {item_alias}.status IN ('deleted', 'rejected', 'superseded')
          OR NOT ({exact_pointer}) THEN NULL
        WHEN {query_claim_version} THEN
          CASE WHEN {query_gate} THEN 'query_reviewed_claim' ELSE NULL END
        WHEN {item_alias}.origin_parse_receipt_id IS NULL THEN 'authored'
        WHEN EXISTS (
            SELECT 1
            FROM knowledge_parse_receipt AS basis_receipt
            JOIN knowledge_parse_batch_item AS basis_item
              ON basis_item.source_id = basis_receipt.source_id
             AND basis_item.snapshot_id = basis_receipt.snapshot_id
             AND json_extract(basis_item.result_json, '$.receipt_id') = basis_receipt.id
            JOIN knowledge_parse_batch AS basis_batch
              ON basis_batch.id = basis_item.batch_id
            WHERE basis_receipt.id = {item_alias}.origin_parse_receipt_id
              AND {full_gate}
        ) THEN 'parse_pipeline'
        WHEN {_reviewed_claim_candidate_gate(item_alias)} THEN 'reviewed_claim'
        ELSE NULL
    END"""


def graph_candidate_publication_basis(
    connection, item_id: str, version_id: str,
) -> dict[str, object]:
    """Describe the exact version's publication basis and readable overlays.

    A reviewed-claim receipt proves access to the base proposition and its
    review decision. Identity and typed interpretation overlays remain gated
    until the complete parse publisher has authorized that exact public item.
    """
    row = connection.execute(
        f"""SELECT item.id, ({graph_candidate_publication_basis_sql('item')}) AS basis
            FROM knowledge_item AS item
            WHERE item.id = ? AND item.published_graph_version_id = ?""",
        (item_id, version_id),
    ).fetchone()
    basis = str(row["basis"]) if row is not None and row["basis"] else None
    return {
        **graph_candidate_publication_capabilities(basis),
        "candidate_item_id": item_id,
        "candidate_version_id": version_id,
    }


def preserve_published_graph_version_before_append(
    connection, item_id: str,
) -> str | None:
    """Capture an existing public head before an append can change MAX(version).

    Existing pointers are authoritative. For pre-pointer rows, first apply the
    legacy publication gate to the current head, then consult a valid retry
    checkpoint for its exact prior version. Unpublished drafts remain unpinned.
    """
    item = connection.execute(
        "SELECT id, kind, space, work_id, status, origin_parse_receipt_id, "
        "published_graph_version_id FROM knowledge_item WHERE id = ?",
        (item_id,),
    ).fetchone()
    if item is None or item["kind"] != "knowledge_graph_candidate":
        return None
    if item["status"] in {"deleted", "rejected", "superseded"}:
        return None
    if item["published_graph_version_id"]:
        return str(item["published_graph_version_id"])

    legacy_gate = published_graph_candidate_filter(
        "candidate", require_version_pointer=False, allow_checkpoint=False,
    )
    legacy = connection.execute(
        f"""
        SELECT version.id AS version_id
        FROM knowledge_item AS candidate
        JOIN knowledge_version AS version ON version.item_id = candidate.id
          AND version.version = (
              SELECT MAX(latest.version) FROM knowledge_version AS latest
              WHERE latest.item_id = candidate.id
          )
        WHERE candidate.id = ? AND {legacy_gate}
        """,
        (item_id,),
    ).fetchone()
    version_id = str(legacy["version_id"]) if legacy is not None else None

    if version_id is None and item["origin_parse_receipt_id"]:
        from .parse_publication_checkpoint import published_checkpoint_sql

        checkpoint_gate = published_checkpoint_sql(
            "receipt", "checkpoint_item", "checkpoint_batch",
        )
        checkpoint = connection.execute(
            f"""
            SELECT pinned_version.id AS version_id
            FROM knowledge_parse_receipt AS receipt
            JOIN knowledge_parse_batch_item AS checkpoint_item
              ON checkpoint_item.batch_id = receipt.parse_batch_id
             AND checkpoint_item.source_id = receipt.source_id
             AND checkpoint_item.snapshot_id = receipt.snapshot_id
            JOIN knowledge_parse_batch AS checkpoint_batch
              ON checkpoint_batch.id = checkpoint_item.batch_id
            JOIN json_each(
                json_extract(checkpoint_item.result_json, '$.publication_checkpoint'),
                '$.candidate_versions'
            ) AS pinned ON pinned.key = ? AND pinned.type = 'text'
            JOIN knowledge_version AS pinned_version
              ON pinned_version.id = pinned.value
             AND pinned_version.item_id = ?
            JOIN knowledge_item AS candidate ON candidate.id = ?
            WHERE receipt.id = candidate.origin_parse_receipt_id
              AND receipt.id = ?
              AND candidate.space = receipt.space
              AND candidate.work_id IS receipt.work_id
              AND candidate.status NOT IN ('deleted', 'rejected', 'superseded')
              AND {checkpoint_gate}
            ORDER BY checkpoint_item.updated_at DESC
            LIMIT 1
            """,
            (item_id, item_id, item_id, item["origin_parse_receipt_id"]),
        ).fetchone()
        if checkpoint is not None:
            version_id = str(checkpoint["version_id"])

    if version_id is None:
        return None
    updated = connection.execute(
        "UPDATE knowledge_item SET published_graph_version_id = ? "
        "WHERE id = ? AND published_graph_version_id IS NULL "
        "AND status NOT IN ('deleted', 'rejected', 'superseded')",
        (version_id, item_id),
    )
    return version_id if updated.rowcount == 1 else None
