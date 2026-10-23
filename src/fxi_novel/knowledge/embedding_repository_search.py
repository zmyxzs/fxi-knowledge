"""Read current embedding scopes and ranked dense candidates."""

import json
import math
import threading
from typing import Any
import weakref

import numpy as np

from fxi_novel.knowledge.embedding_read_cache import (
    DenseSearchMatrix,
    DenseSearchMatrixCache,
)
from fxi_novel.knowledge.models import (
    EvidenceReference,
    SearchResult,
    SnapshotReference,
    SourceReference,
    validate_scope,
)
from fxi_novel.knowledge.scope_selection import (
    current_prose_projection_filter,
    source_selection_filter,
)

_DENSE_CACHE_CREATION_LOCK = threading.Lock()
_DENSE_ROW_FIELDS = (
    "chunk_id", "chunk_text", "start_offset", "end_offset", "offset_unit",
    "source_id", "source_title", "source_uri", "space", "work_id",
    "document_metadata_json", "snapshot_id", "snapshot_hash", "snapshot_length",
    "evidence_id", "quote_hash",
)


class EmbeddingSearchRepositoryMixin:
    """Read-only dense retrieval operations for the embedding repository facade."""

    def _dense_search_matrix_cache(self) -> DenseSearchMatrixCache:
        cache = getattr(self, "_dense_matrix_cache", None)
        if cache is not None:
            return cache
        with _DENSE_CACHE_CREATION_LOCK:
            cache = getattr(self, "_dense_matrix_cache", None)
            if cache is None:
                cache = DenseSearchMatrixCache(self.database.path)
                self._dense_matrix_cache = cache
                weakref.finalize(self, DenseSearchMatrixCache.close, cache)
        return cache

    def list_current_snapshot_refs(
        self,
        *,
        space: str,
        work_id: str | None,
        source_ids: list[str] | None = None,
        folder_ids: list[str] | None = None,
    ) -> list[dict[str, str]]:
        """Resolve an explicit selection to current source/snapshot pairs."""
        validate_scope(space, work_id)
        source_ids = list(dict.fromkeys(source_ids)) if source_ids is not None else None
        folder_ids = list(dict.fromkeys(folder_ids)) if folder_ids is not None else None
        if source_ids is not None and (
            len(source_ids) > 10_000
            or any(not isinstance(item, str) or not item.strip() for item in source_ids)
        ):
            raise ValueError("source_ids must contain at most 10,000 non-empty IDs")
        if folder_ids is not None and (
            len(folder_ids) > 1_000
            or any(not isinstance(item, str) or not item.strip() for item in folder_ids)
        ):
            raise ValueError("folder_ids must contain at most 1,000 non-empty IDs")
        if source_ids == [] and not folder_ids:
            return []

        parameters: list[Any] = [space, work_id]
        source_filter = source_selection_filter("s", source_ids, folder_ids, parameters)
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT s.id AS source_id, current.snapshot_id
                FROM source_current_snapshot AS current
                JOIN source_snapshot AS ss
                  ON ss.id = current.snapshot_id AND ss.source_id = current.source_id
                JOIN source AS s ON s.id = current.source_id
                WHERE s.space = ? AND s.work_id IS ? AND s.deleted_at IS NULL
                  AND (s.work_id IS NULL OR EXISTS (
                      SELECT 1 FROM knowledge_project AS p
                      WHERE p.id = s.work_id AND p.space = s.space AND p.status = 'active'
                  ))
                """ + source_filter + " ORDER BY s.id, current.snapshot_id",
                parameters,
            ).fetchall()
        return [
            {"source_id": str(row["source_id"]), "snapshot_id": str(row["snapshot_id"])}
            for row in rows
        ]

    def search_dense_candidates(
        self,
        query_vector: list[float],
        *,
        space: str,
        work_id: str | None,
        allowed_source_snapshots: list[dict[str, str]],
        provider: str,
        model_id: str,
        route_fingerprint: str,
        dimensions: int,
        top_k: int,
    ) -> dict[str, Any]:
        """Return exact cosine candidates from only caller-authorized current snapshots."""
        return self.search_dense_candidates_batch(
            [query_vector],
            space=space,
            work_id=work_id,
            allowed_source_snapshots=allowed_source_snapshots,
            provider=provider,
            model_id=model_id,
            route_fingerprint=route_fingerprint,
            dimensions=dimensions,
            top_k=top_k,
        )[0]

    def search_dense_candidates_batch(
        self,
        query_vectors: list[list[float]],
        *,
        space: str,
        work_id: str | None,
        allowed_source_snapshots: list[dict[str, str]],
        provider: str,
        model_id: str,
        route_fingerprint: str,
        dimensions: int,
        top_k: int,
    ) -> list[dict[str, Any]]:
        """Score several query variants in one scope scan and return per-query rankings."""
        validate_scope(space, work_id)
        if not route_fingerprint.strip() or not provider.strip() or not model_id.strip():
            raise ValueError("embedding route identity must be non-empty")
        if not 1 <= dimensions <= 65_536:
            raise ValueError("query vector dimensions do not match the active embedding route")
        if not isinstance(query_vectors, list) or not 1 <= len(query_vectors) <= 8:
            raise ValueError("dense query batch must contain between one and eight vectors")
        if not 1 <= top_k <= 100:
            raise ValueError("dense candidate top_k must be between 1 and 100")

        for query_vector in query_vectors:
            if not isinstance(query_vector, list) or len(query_vector) != dimensions:
                raise ValueError("query vector dimensions do not match the active embedding route")
            if any(
                not isinstance(value, (int, float)) or not math.isfinite(float(value))
                for value in query_vector
            ):
                raise ValueError("query vector must contain only finite numbers")
        query_matrix = np.asarray(query_vectors, dtype=np.float64)
        query_scales = np.max(np.abs(query_matrix), axis=1)
        if np.any(~np.isfinite(query_scales)) or np.any(query_scales <= 0):
            raise ValueError("query vector must have a finite non-zero norm")
        scaled_queries = query_matrix / query_scales[:, None]
        query_norms = np.linalg.norm(scaled_queries, axis=1)
        if np.any(~np.isfinite(query_norms)) or np.any(query_norms <= 0):
            raise ValueError("query vector must have a finite non-zero norm")
        normalized_queries = scaled_queries / query_norms[:, None]
        if len(allowed_source_snapshots) > 10_000:
            raise ValueError("allowed source/snapshot scope is limited to 10,000 pairs")

        normalized_refs: list[tuple[str, str]] = []
        for item in allowed_source_snapshots:
            if not isinstance(item, dict):
                raise ValueError("allowed source/snapshot entries must be objects")
            source_id = str(item.get("source_id", "")).strip()
            snapshot_id = str(item.get("snapshot_id", "")).strip()
            if not source_id or not snapshot_id:
                raise ValueError("allowed source/snapshot entries require both IDs")
            normalized_refs.append((source_id, snapshot_id))
        normalized_refs = list(dict.fromkeys(normalized_refs))
        if not normalized_refs:
            coverage = {
                "retrieval_mode": "lexical_only",
                "coverage_status": "lexical_only_no_allowed_snapshots",
                "allowed_snapshot_count": 0,
                "eligible_chunk_count": 0,
                "indexed_chunk_count": 0,
                "valid_vector_count": 0,
                "missing_index_count": 0,
                "invalid_vector_count": 0,
                "snapshot_watermark_checked": True,
            }
            return [{"hits": [], "coverage": coverage} for _ in normalized_queries]

        def candidate_order(hit: tuple[float, Any]) -> tuple[Any, ...]:
            score, row = hit
            return (
                -score,
                str(row["source_title"]).casefold(),
                int(row["start_offset"]),
                str(row["chunk_id"]),
            )
        matrix_cache = self._dense_search_matrix_cache()
        matrix_key = (
            space,
            work_id,
            tuple(sorted(normalized_refs)),
            provider,
            model_id,
            route_fingerprint,
            dimensions,
        )
        dense_matrix = matrix_cache.get(matrix_key)
        if dense_matrix is None:
            snapshot_revision: int | None = None
            after_scan_revision: int | None = None
            eligible_count = 0
            indexed_count = 0
            valid_vector_count = 0
            invalid_vector_count = 0
            matrix_rows: list[dict[str, Any]] = []
            normalized_vector_values: list[np.ndarray] = []

            # Read one SQLite snapshot. Pages stay below SQLite's historical
            # bind-variable limit, while the matrix is shared by all query variants.
            with self.database.connection() as connection:
                connection.execute("BEGIN")
                snapshot_revision = matrix_cache.read_revision(connection)
                for start in range(0, len(normalized_refs), 400):
                    page = normalized_refs[start:start + 400]
                    values_sql = ",".join("(?, ?)" for _ in page)
                    cursor = connection.execute(
                        f"""
                        WITH allowed(source_id, snapshot_id) AS (VALUES {values_sql})
                        SELECT c.id AS chunk_id, c.text AS chunk_text,
                               c.start_offset, c.end_offset, c.offset_unit,
                               s.id AS source_id, s.title AS source_title, s.source_uri,
                               s.space, s.work_id, s.document_metadata_json,
                               ss.id AS snapshot_id, ss.content_hash AS snapshot_hash,
                               length(ss.content) AS snapshot_length,
                               e.id AS evidence_id, e.quote_hash,
                               projection.dimensions AS vector_dimensions,
                               projection.vector_blob, projection.vector_format,
                               projection.id AS projection_id
                        FROM allowed
                        JOIN source AS s ON s.id = allowed.source_id
                        JOIN source_snapshot AS ss
                          ON ss.id = allowed.snapshot_id AND ss.source_id = s.id
                        JOIN source_current_snapshot AS current
                          ON current.source_id = s.id AND current.snapshot_id = ss.id
                        JOIN chunk AS c ON c.snapshot_id = ss.id
                        JOIN evidence_span AS e ON e.id = (
                          SELECT MIN(candidate.id) FROM evidence_span AS candidate
                          WHERE candidate.chunk_id = c.id
                        )
                        LEFT JOIN embedding_projection AS projection
                          ON projection.target_type = ?
                         AND projection.target_id = c.id
                         AND projection.content_hash = c.content_hash
                         AND projection.provider = ? AND projection.model_id = ?
                         AND projection.model_version = ? AND projection.status = 'complete'
                        WHERE s.space = ? AND s.work_id IS ? AND s.deleted_at IS NULL
                          AND (s.work_id IS NULL OR EXISTS (
                              SELECT 1 FROM knowledge_project AS p
                              WHERE p.id = s.work_id AND p.space = s.space
                                AND p.status = 'active'
                          ))
                        """ + "\n AND " + current_prose_projection_filter("s") + """
                        ORDER BY c.id
                        """,
                        (
                            *(value for pair in page for value in pair),
                            "chunk", provider, model_id, route_fingerprint, space, work_id,
                        ),
                    )
                    while True:
                        rows = cursor.fetchmany(256)
                        if not rows:
                            break
                        for row in rows:
                            eligible_count += 1
                            if row["projection_id"] is None:
                                continue
                            indexed_count += 1
                            try:
                                vector_dimensions = int(row["vector_dimensions"])
                                vector_format = str(row["vector_format"])
                                vector_bytes = bytes(row["vector_blob"])
                                if (
                                    vector_format != "f32le-v1"
                                    or vector_dimensions != dimensions
                                    or len(vector_bytes) != dimensions * 4
                                ):
                                    raise ValueError(
                                        "stored embedding metadata is incompatible"
                                    )
                                vector = np.frombuffer(vector_bytes, dtype="<f4")
                                if vector.shape != (dimensions,) or not np.all(np.isfinite(vector)):
                                    raise ValueError("stored embedding vector is invalid")
                            except (TypeError, ValueError, OverflowError):
                                invalid_vector_count += 1
                                continue
                            vector64 = vector.astype(np.float64, copy=True)
                            vector_norm = float(np.linalg.norm(vector64))
                            if not math.isfinite(vector_norm) or vector_norm <= 0:
                                invalid_vector_count += 1
                                continue
                            normalized_vector_values.append(vector64 / vector_norm)
                            matrix_rows.append({
                                field: row[field] for field in _DENSE_ROW_FIELDS
                            })
                            valid_vector_count += 1
                after_scan_revision = matrix_cache.read_revision(connection)
                connection.commit()

            vectors = (
                np.stack(normalized_vector_values)
                if normalized_vector_values
                else np.empty((0, dimensions), dtype=np.float64)
            )
            vectors.setflags(write=False)
            metadata_bytes = sum(
                len(str(row["chunk_text"]).encode("utf-8")) + 512
                for row in matrix_rows
            )
            dense_matrix = DenseSearchMatrix(
                rows=tuple(matrix_rows),
                vectors=vectors,
                eligible_count=eligible_count,
                indexed_count=indexed_count,
                valid_vector_count=valid_vector_count,
                invalid_vector_count=invalid_vector_count,
                weight_bytes=int(vectors.nbytes + metadata_bytes),
            )
            if snapshot_revision is not None and snapshot_revision == after_scan_revision:
                matrix_cache.put_if_unchanged(
                    matrix_key,
                    dense_matrix,
                    expected_revision=snapshot_revision,
                )

        score_matrix = dense_matrix.vectors @ normalized_queries.T
        valid_score_rows = np.all(np.isfinite(score_matrix), axis=1)
        score_invalid_count = int(np.count_nonzero(~valid_score_rows))
        eligible_count = dense_matrix.eligible_count
        indexed_count = dense_matrix.indexed_count
        valid_vector_count = max(0, dense_matrix.valid_vector_count - score_invalid_count)
        invalid_vector_count = dense_matrix.invalid_vector_count + score_invalid_count
        ranked_by_query: list[list[tuple[float, dict[str, Any]]]] = []
        for query_index in range(len(normalized_queries)):
            ranked = [
                (float(score_matrix[index, query_index]), row)
                for index, row in enumerate(dense_matrix.rows)
                if valid_score_rows[index]
            ]
            ranked_by_query.append(sorted(ranked, key=candidate_order)[:top_k])
        missing_count = max(0, eligible_count - indexed_count)
        if eligible_count == 0:
            coverage_status = "lexical_only_no_current_chunks"
            retrieval_mode = "lexical_only"
        elif valid_vector_count == 0:
            coverage_status = (
                "lexical_only_invalid_or_incompatible_index"
                if indexed_count else "lexical_only_index_unavailable"
            )
            retrieval_mode = "lexical_only"
        elif missing_count or invalid_vector_count:
            coverage_status = "dense_partial"
            retrieval_mode = "dense"
        else:
            coverage_status = "dense_complete"
            retrieval_mode = "dense"
        coverage = {
            "retrieval_mode": retrieval_mode,
            "coverage_status": coverage_status,
            "allowed_snapshot_count": len(normalized_refs),
            "eligible_chunk_count": eligible_count,
            "indexed_chunk_count": indexed_count,
            "valid_vector_count": valid_vector_count,
            "missing_index_count": missing_count,
            "invalid_vector_count": invalid_vector_count,
            "snapshot_watermark_checked": True,
        }
        results: list[dict[str, Any]] = []
        for best_hits in ranked_by_query:
            hits = [
                SearchResult(
                    chunk_id=str(row["chunk_id"]),
                    text=str(row["chunk_text"]),
                    score=score,
                    rank=rank,
                    retrieval_mode="dense",
                    source=SourceReference(
                        id=str(row["source_id"]),
                        title=str(row["source_title"]),
                        source_uri=row["source_uri"],
                        space=str(row["space"]),
                        work_id=row["work_id"],
                        document_metadata=json.loads(str(row["document_metadata_json"])),
                    ),
                    snapshot=SnapshotReference(
                        id=str(row["snapshot_id"]),
                        content_hash=str(row["snapshot_hash"]),
                        content_length=int(row["snapshot_length"]),
                    ),
                    evidence=EvidenceReference(
                        id=str(row["evidence_id"]),
                        quote_hash=row["quote_hash"],
                        text=str(row["chunk_text"]),
                    ),
                    start_offset=int(row["start_offset"]),
                    end_offset=int(row["end_offset"]),
                    offset_unit=str(row["offset_unit"]),
                )
                for rank, (score, row) in enumerate(best_hits, start=1)
            ]
            results.append({"hits": hits, "coverage": coverage})
        return results
