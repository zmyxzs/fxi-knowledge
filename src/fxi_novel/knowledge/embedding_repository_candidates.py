"""Candidate-level semantic embedding projection and retrieval operations."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
from typing import Any
from uuid import uuid4
import weakref

import numpy as np

from fxi_novel.knowledge.embedding_knowledge_items import (
    KNOWLEDGE_CANDIDATE_TYPES,
    knowledge_candidate_content_hash,
    knowledge_candidate_type,
    serialize_knowledge_candidate,
)
from fxi_novel.knowledge.embedding_read_cache import (
    DenseSearchMatrix,
    DenseSearchMatrixCache,
)
from fxi_novel.knowledge.embedding_repository_candidate_support import (
    EmbeddingCandidateSupportMixin,
)
from fxi_novel.knowledge.embedding_repository_support import now
from fxi_novel.knowledge.models import validate_scope
from fxi_novel.knowledge.repository_parse_publication import (
    graph_candidate_publication_basis_sql, published_graph_candidate_filter,
)
from .candidate_query_projection import candidate_retrieval_projection
from .embedding_candidate_components import load_candidate_component_projections
from .query_contracts import EvidenceRef


_MAX_SOURCE_SNAPSHOT_PINS = 10_000
_PROJECTION_PAGE_SIZE = 300
_ENTITY_NAME_PAGE_SIZE = 300
_CANDIDATE_CACHE_CREATION_LOCK = threading.Lock()


class EmbeddingCandidateRepositoryMixin(EmbeddingCandidateSupportMixin):
    """Candidate vector reads and queue operations sharing the embedding tables."""

    def _knowledge_candidate_matrix_cache(self) -> DenseSearchMatrixCache:
        cache = getattr(self, "_knowledge_candidate_matrix_cache_value", None)
        if cache is not None:
            return cache
        with _CANDIDATE_CACHE_CREATION_LOCK:
            cache = getattr(self, "_knowledge_candidate_matrix_cache_value", None)
            if cache is None:
                cache = DenseSearchMatrixCache(
                    self.database.path,
                    revision_column="knowledge_item_revision",
                )
                self._knowledge_candidate_matrix_cache_value = cache
                weakref.finalize(self, DenseSearchMatrixCache.close, cache)
        return cache

    @staticmethod
    def _normalize_allowed_snapshots(
        allowed_source_snapshots: list[dict[str, str]],
    ) -> tuple[list[dict[str, str]], set[tuple[str, str]]]:
        if not isinstance(allowed_source_snapshots, list) or len(allowed_source_snapshots) > _MAX_SOURCE_SNAPSHOT_PINS:
            raise ValueError("candidate vector scope is limited to 10,000 source snapshots")
        normalized: list[dict[str, str]] = []
        for item in allowed_source_snapshots:
            if not isinstance(item, dict):
                raise ValueError("candidate source/snapshot pins must be objects")
            source_id = str(item.get("source_id", "")).strip()
            snapshot_id = str(item.get("snapshot_id", "")).strip()
            if not source_id or not snapshot_id:
                raise ValueError("candidate source/snapshot pins require both IDs")
            normalized.append({"source_id": source_id, "snapshot_id": snapshot_id})
        normalized = list({(row["source_id"], row["snapshot_id"]): row for row in normalized}.values())
        return normalized, {(row["source_id"], row["snapshot_id"]) for row in normalized}

    @staticmethod
    def _candidate_type_filter(candidate_types: list[str]) -> str:
        clauses: list[str] = []
        if "entity" in candidate_types:
            clauses.append("json_extract(version.content_json, '$.candidate_role') = 'entity'")
        if "fact" in candidate_types:
            # Optional payloads are qualified after evidence is loaded. Any
            # published claim can have a usable plain-fact projection.
            clauses.append("json_extract(version.content_json, '$.candidate_role') = 'fact'")
        if "event" in candidate_types:
            clauses.append("(json_extract(version.content_json, '$.candidate_role') = 'fact' AND json_extract(version.content_json, '$.category') = 'event' AND json_type(version.content_json, '$.typed_event') IS 'object')")
        if "rule" in candidate_types:
            clauses.append("(json_extract(version.content_json, '$.candidate_role') = 'fact' AND json_extract(version.content_json, '$.category') = 'rule' AND json_type(version.content_json, '$.typed_rule') IS 'object')")
        return "(" + " OR ".join(clauses) + ")" if clauses else "0"

    @staticmethod
    def _candidate_rows_sql(*, candidate_types: list[str], selected_items: bool = False) -> str:
        published = published_graph_candidate_filter("item")
        basis = graph_candidate_publication_basis_sql("item")
        type_filter = EmbeddingCandidateRepositoryMixin._candidate_type_filter(candidate_types)
        item_filter = "AND item.id IN (SELECT CAST(value AS TEXT) FROM json_each(?))" if selected_items else ""
        return f"""
            WITH allowed(source_id, snapshot_id) AS MATERIALIZED (
                SELECT CAST(json_extract(pin.value, '$.source_id') AS TEXT),
                       CAST(json_extract(pin.value, '$.snapshot_id') AS TEXT)
                FROM json_each(?) AS pin
            ),
            visible_candidates AS MATERIALIZED (
                SELECT item.id AS item_id, item.space, item.work_id, item.status,
                       candidate.candidate_version_id AS version_id,
                       version.content_json, ({basis}) AS publication_basis
                FROM knowledge_card_candidate AS candidate
                JOIN knowledge_item AS item ON item.id = candidate.candidate_item_id
                JOIN knowledge_version AS version
                  ON version.id = candidate.candidate_version_id
                 AND version.item_id = candidate.candidate_item_id
                WHERE item.kind = 'knowledge_graph_candidate'
                  AND item.status NOT IN ('deleted', 'rejected')
                  AND item.space = ? AND item.work_id IS ?
                  {item_filter}
                  AND {published}
                  AND {type_filter}
                  AND EXISTS (
                      SELECT 1 FROM knowledge_version_evidence AS scoped_link
                      JOIN evidence_span AS scoped_span
                        ON scoped_span.id = scoped_link.evidence_span_id
                      JOIN source_current_snapshot AS scoped_current
                        ON scoped_current.snapshot_id = scoped_span.snapshot_id
                      JOIN allowed AS scoped_pin
                        ON scoped_pin.source_id = scoped_current.source_id
                       AND scoped_pin.snapshot_id = scoped_current.snapshot_id
                      WHERE scoped_link.knowledge_version_id = version.id
                  )
            )
            SELECT visible.item_id, visible.space, visible.work_id, visible.status,
                   visible.version_id, visible.content_json, visible.publication_basis,
                   link.evidence_span_id, span.snapshot_id, span.start_offset,
                   span.end_offset, span.quote_hash,
                   snapshot.content_hash AS snapshot_hash,
                   substr(snapshot.content, span.start_offset + 1,
                          span.end_offset - span.start_offset) AS quote,
                   source.id AS source_id, source.space AS source_space,
                   source.work_id AS source_work_id, source.deleted_at,
                   current.snapshot_id AS current_snapshot_id,
                   CASE WHEN allowed.source_id IS NULL THEN 0 ELSE 1 END AS in_allowed_scope
            FROM visible_candidates AS visible
            JOIN knowledge_version_evidence AS link
              ON link.knowledge_version_id = visible.version_id
            JOIN evidence_span AS span ON span.id = link.evidence_span_id
            JOIN source_snapshot AS snapshot ON snapshot.id = span.snapshot_id
            JOIN source AS source ON source.id = snapshot.source_id
            LEFT JOIN source_current_snapshot AS current
              ON current.source_id = source.id
            LEFT JOIN allowed ON allowed.source_id = source.id
                             AND allowed.snapshot_id = snapshot.id
            ORDER BY visible.item_id, span.snapshot_id, span.start_offset,
                     span.end_offset, span.id
        """

    def _load_knowledge_candidate_inputs(
        self,
        connection: sqlite3.Connection,
        *,
        space: str,
        work_id: str | None,
        allowed_source_snapshots: list[dict[str, str]],
        candidate_types: list[str],
        item_ids: list[str] | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, int], int]:
        allowed, allowed_set = self._normalize_allowed_snapshots(allowed_source_snapshots)
        type_counts = {candidate_type: 0 for candidate_type in KNOWLEDGE_CANDIDATE_TYPES}
        if not allowed:
            return [], type_counts, 0
        pins_json = json.dumps(allowed, ensure_ascii=False, separators=(",", ":"))
        if item_ids is not None and not item_ids:
            return [], type_counts, 0
        parameters = (pins_json, space, work_id)
        if item_ids is not None:
            parameters += (json.dumps(sorted(set(item_ids))),)
        rows = connection.execute(
            self._candidate_rows_sql(candidate_types=candidate_types, selected_items=item_ids is not None),
            parameters,
        ).fetchall()
        component_projections = load_candidate_component_projections(connection, rows)
        grouped: dict[str, dict[str, Any]] = {}
        invalid_count = 0
        for row in rows:
            item_id = str(row["item_id"])
            entry = grouped.get(item_id)
            if entry is None:
                try:
                    component = component_projections[item_id]
                    if component.get("error"):
                        raise ValueError(component["error"])
                    content = component["content"]
                    if not isinstance(content, dict):
                        raise ValueError("candidate content is not an object")
                    candidate_type = knowledge_candidate_type(content)
                    if candidate_type is None:
                        raise ValueError("candidate kind is not searchable")
                    entry = {
                        "item_id": item_id,
                        "version_id": str(row["version_id"]),
                        "content": content,
                        "candidate_type": candidate_type,
                        "publication_basis": component["publication_basis"],
                        "publication_gaps": component["publication_gaps"],
                        "evidence_refs": [],
                        "evidence_keys": set(),
                        "invalid": False,
                    }
                except (TypeError, ValueError, json.JSONDecodeError):
                    grouped[item_id] = {"invalid": True}
                    continue
                grouped[item_id] = entry
            if entry.get("invalid"):
                continue
            source_id = str(row["source_id"])
            snapshot_id = str(row["snapshot_id"])
            quote = str(row["quote"] or "")
            valid = (
                bool(row["in_allowed_scope"])
                and (source_id, snapshot_id) in allowed_set
                and str(row["current_snapshot_id"] or "") == snapshot_id
                and row["deleted_at"] is None
                and row["source_space"] == space
                and row["source_work_id"] == work_id
                and row["space"] == space
                and row["work_id"] == work_id
                and bool(quote)
                and hashlib.sha256(quote.encode("utf-8")).hexdigest() == str(row["quote_hash"])
            )
            if not valid:
                entry["invalid"] = True
                continue
            evidence_key = (
                source_id, snapshot_id, str(row["snapshot_hash"]),
                int(row["start_offset"]), int(row["end_offset"]), str(row["quote_hash"]),
            )
            if evidence_key in entry["evidence_keys"]:
                continue
            entry["evidence_keys"].add(evidence_key)
            entry["evidence_refs"].append({
                "id": str(row["evidence_span_id"]),
                "space": space,
                "work_id": work_id,
                "branch_id": entry["content"].get("branch_id"),
                "source_id": source_id,
                "snapshot_id": snapshot_id,
                "content_hash": str(row["snapshot_hash"]),
                "start_offset": int(row["start_offset"]),
                "end_offset": int(row["end_offset"]),
                "quote_hash": str(row["quote_hash"]),
                "quote": quote,
            })

        referenced_ids = {
            reference
            for entry in grouped.values()
            if not entry.get("invalid")
            for reference in self._candidate_related_entity_refs(entry["content"])
        }
        related_names = self._load_candidate_entity_names(
            connection,
            space=space,
            work_id=work_id,
            allowed_source_snapshots=allowed,
            entity_refs=referenced_ids,
        )

        candidates: list[dict[str, Any]] = []
        for entry in grouped.values():
            if entry.get("invalid") or not entry.get("evidence_refs"):
                invalid_count += 1
                continue
            try:
                entry["content"], entry["capability_gaps"] = candidate_retrieval_projection(
                    entry["content"],
                    tuple(EvidenceRef.model_validate(ref) for ref in entry["evidence_refs"]),
                )
                entry["capability_gaps"].extend(entry.pop("publication_gaps"))
                candidate_type, serialized_text, response_payload = serialize_knowledge_candidate(
                    entry["content"], related_names=related_names
                )
                if candidate_type not in candidate_types:
                    continue
                entry["candidate_type"] = candidate_type
            except (TypeError, ValueError, json.JSONDecodeError):
                invalid_count += 1
                continue
            entry["serialized_text"] = serialized_text
            entry["summary"] = response_payload["summary"]
            entry["typed_payload"] = response_payload["typed_payload"]
            entry["semantic_assessment"] = response_payload["semantic_assessment"]
            content = entry["content"]
            exact_candidate_interval = (
                int(content["evidence_quote_start"]), int(content["evidence_quote_end"])
            )
            if not any(
                reference["start_offset"] <= exact_candidate_interval[0]
                and reference["end_offset"] >= exact_candidate_interval[1]
                for reference in entry["evidence_refs"]
            ):
                invalid_count += 1
                continue
            if not self._typed_payload_evidence_matches(
                entry["typed_payload"], entry["evidence_refs"]
            ):
                invalid_count += 1
                continue
            entry["evidence_refs"].sort(key=lambda ref: (
                ref["source_id"], ref["snapshot_id"], ref["start_offset"], ref["end_offset"]
            ))
            entry["serialized_text"] = serialized_text
            entry["summary"] = response_payload["summary"]
            entry["typed_payload"] = response_payload["typed_payload"]
            entry["semantic_assessment"] = response_payload["semantic_assessment"]
            entry["content_hash"] = knowledge_candidate_content_hash(
                item_id=entry["item_id"],
                version_id=entry["version_id"],
                serialized_text=entry["serialized_text"],
                evidence_refs=entry["evidence_refs"],
            )
            entry.pop("content", None)
            entry.pop("evidence_keys", None)
            type_counts[entry["candidate_type"]] += 1
            candidates.append(entry)
        return candidates, type_counts, invalid_count

    def enqueue_knowledge_item_embedding_scope_page(
        self,
        *,
        space: str,
        work_id: str | None,
        allowed_source_snapshots: list[dict[str, str]],
        route_fingerprint: str,
        provider: str,
        model_id: str,
        limit: int = 100,
        candidate_types: list[str] | None = None,
    ) -> dict[str, Any]:
        """Queue one bounded page of current, published candidate versions."""
        if not route_fingerprint.strip() or not provider.strip() or not model_id.strip():
            raise ValueError("embedding route identity must be non-empty")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 512:
            raise ValueError("candidate embedding page size must be between 1 and 512")
        types = self._normalize_candidate_types(candidate_types)
        allowed, _allowed_set = self._normalize_allowed_snapshots(allowed_source_snapshots)
        allowed.sort(key=lambda pin: (pin["source_id"], pin["snapshot_id"]))
        now_value = now()
        # Full scope discovery is read-only. The write transaction rechecks only
        # this page's exact versions before enqueuing, preserving publication
        # and snapshot eligibility without holding a write lock for the scan.
        with self.database.connection() as connection:
            candidates, _counts, invalid_count = self._load_knowledge_candidate_inputs(
                connection, space=space, work_id=work_id,
                allowed_source_snapshots=allowed, candidate_types=types,
            )
        candidates.sort(key=lambda row: row["item_id"])
        candidate_set_hash = hashlib.sha256(json.dumps(
            [(row["item_id"], row["version_id"], row["content_hash"]) for row in candidates],
            ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        scope_json = json.dumps({
            "space": space, "work_id": work_id, "source_snapshots": allowed,
            "candidate_types": sorted(types), "candidate_set_hash": candidate_set_hash,
            "route_fingerprint": route_fingerprint, "provider": provider, "model_id": model_id,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        scope_key = hashlib.sha256(scope_json.encode("utf-8")).hexdigest()
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO embedding_projection_candidate_backfill(
                       scope_key, route_fingerprint, scope_json, cursor_item_id,
                       upper_bound_item_id, scanned_candidates, status, created_at, updated_at
                   ) VALUES (?, ?, ?, '', ?, 0, ?, ?, ?)""",
                (scope_key, route_fingerprint, scope_json,
                 candidates[-1]["item_id"] if candidates else "",
                 "pending" if candidates else "completed", now_value, now_value),
            )
            cursor = connection.execute(
                """SELECT cursor_item_id, upper_bound_item_id, scanned_candidates, status
                   FROM embedding_projection_candidate_backfill WHERE scope_key = ?""",
                (scope_key,),
            ).fetchone()
            if cursor is None:
                raise RuntimeError("candidate embedding cursor was not persisted")
            if cursor["status"] != "pending":
                return {
                    "scanned": 0, "enqueued": 0, "has_more": False,
                    "invalid_candidate_count": invalid_count, "scope_key": scope_key,
                    "status": str(cursor["status"]),
                }
            after_id, upper_bound = str(cursor["cursor_item_id"]), str(cursor["upper_bound_item_id"])
            remaining = [row for row in candidates if after_id < row["item_id"] <= upper_bound]
            page = remaining[:limit]
            has_more = len(remaining) > limit
            current, _, _ = self._load_knowledge_candidate_inputs(
                connection, space=space, work_id=work_id, allowed_source_snapshots=allowed,
                candidate_types=types, item_ids=[row["item_id"] for row in page],
            )
            current_pins = {(row["item_id"], row["version_id"], row["content_hash"]) for row in current}
            to_enqueue: list[dict[str, Any]] = []
            for candidate in page:
                item_id, content_hash = candidate["item_id"], candidate["content_hash"]
                if (item_id, candidate["version_id"], content_hash) not in current_pins:
                    continue
                projection = connection.execute(
                    """SELECT 1 FROM embedding_projection
                       WHERE target_type = 'knowledge_item' AND target_id = ?
                         AND content_hash = ? AND provider = ? AND model_id = ?
                         AND model_version = ? AND status = 'complete' LIMIT 1""",
                    (item_id, content_hash, provider, model_id, route_fingerprint),
                ).fetchone()
                if projection is not None:
                    continue
                job = connection.execute(
                    """SELECT 1 FROM embedding_projection_job
                       WHERE target_type = 'knowledge_item' AND target_id = ?
                         AND content_hash = ? AND route_fingerprint = ?
                         AND status IN ('pending', 'running', 'failed') LIMIT 1""",
                    (item_id, content_hash, route_fingerprint),
                ).fetchone()
                if job is None:
                    to_enqueue.append(candidate)
            connection.executemany(
                """INSERT OR IGNORE INTO embedding_projection_job(
                       id, target_type, target_id, content_hash, route_fingerprint,
                       provider, model_id, status, attempts, error_text, created_at, updated_at
                   ) VALUES (?, 'knowledge_item', ?, ?, ?, ?, ?, 'pending', 0, NULL, ?, ?)""",
                [(uuid4().hex, row["item_id"], row["content_hash"], route_fingerprint,
                  provider, model_id, now_value, now_value) for row in to_enqueue],
            )
            cursor_item_id = page[-1]["item_id"] if page else after_id
            connection.execute(
                """UPDATE embedding_projection_candidate_backfill
                   SET cursor_item_id = ?, scanned_candidates = scanned_candidates + ?,
                       status = ?, updated_at = ? WHERE scope_key = ? AND status = 'pending'""",
                (cursor_item_id, len(page), "pending" if has_more else "completed",
                 now_value, scope_key),
            )
        return {
            "scanned": len(page), "enqueued": len(to_enqueue), "has_more": has_more,
            "invalid_candidate_count": invalid_count, "scope_key": scope_key,
            "status": "pending" if has_more else "completed",
            "cursor_item_id": cursor_item_id, "upper_bound_item_id": upper_bound,
        }

    def retry_failed_knowledge_item_embedding_jobs(
        self,
        *,
        route_fingerprint: str,
        provider: str,
        model_id: str,
        space: str,
        work_id: str | None,
        allowed_source_snapshots: list[dict[str, str]],
    ) -> int:
        """Retry failed jobs only while their exact published version remains in scope."""
        if not route_fingerprint.strip() or not provider.strip() or not model_id.strip():
            raise ValueError("embedding route identity must be non-empty")
        allowed, _allowed_set = self._normalize_allowed_snapshots(allowed_source_snapshots)
        if not allowed:
            return 0
        now_value = now()
        with self.database.transaction() as connection:
            candidates, _counts, _invalid_count = self._load_knowledge_candidate_inputs(
                connection,
                space=space,
                work_id=work_id,
                allowed_source_snapshots=allowed,
                candidate_types=sorted(KNOWLEDGE_CANDIDATE_TYPES),
            )
            current_hashes = {
                (row["item_id"], row["content_hash"]) for row in candidates
            }
            failed = connection.execute(
                """SELECT id, target_id, content_hash FROM embedding_projection_job
                   WHERE target_type = 'knowledge_item' AND route_fingerprint = ?
                     AND provider = ? AND model_id = ? AND status = 'failed'""",
                (route_fingerprint, provider, model_id),
            ).fetchall()
            retried = 0
            for row in failed:
                if (str(row["target_id"]), str(row["content_hash"])) not in current_hashes:
                    continue
                cursor = connection.execute(
                    """UPDATE embedding_projection_job
                       SET status = 'pending', error_text = NULL, updated_at = ?
                       WHERE id = ? AND status = 'failed'""",
                    (now_value, str(row["id"])),
                )
                retried += int(cursor.rowcount)
            return retried

    @staticmethod
    def _normalize_candidate_types(candidate_types: list[str] | None) -> list[str]:
        values = (
            list(sorted(KNOWLEDGE_CANDIDATE_TYPES))
            if candidate_types is None else candidate_types
        )
        if not isinstance(values, list) or not values:
            raise ValueError("candidate_types must be a non-empty unique list of entity/fact/event/rule")
        if any(not isinstance(value, str) or value not in KNOWLEDGE_CANDIDATE_TYPES for value in values):
            raise ValueError("candidate_types must be a non-empty unique list of entity/fact/event/rule")
        if len(values) != len(set(values)):
            raise ValueError("candidate_types must be a non-empty unique list of entity/fact/event/rule")
        return list(values)

    def claim_knowledge_item_embedding_jobs(
        self,
        *,
        route_fingerprint: str,
        provider: str,
        model_id: str,
        space: str,
        work_id: str | None,
        allowed_source_snapshots: list[dict[str, str]],
        limit: int = 16,
    ) -> list[dict[str, Any]]:
        """Claim only candidate jobs whose exact published version pins still match."""
        if not 1 <= limit <= 64:
            raise ValueError("candidate embedding job batch size must be between 1 and 64")
        allowed, _allowed_set = self._normalize_allowed_snapshots(allowed_source_snapshots)
        now_value = now()
        with self.database.transaction() as connection:
            pending = connection.execute(
                """
                SELECT id AS job_id, target_id, content_hash
                FROM embedding_projection_job
                WHERE target_type = 'knowledge_item' AND route_fingerprint = ?
                  AND provider = ? AND model_id = ? AND status = 'pending'
                ORDER BY created_at, id LIMIT ?
                """,
                (route_fingerprint, provider, model_id, max(limit * 4, limit)),
            ).fetchall()
            if not pending:
                return []
            candidates, _counts, _invalid_count = self._load_knowledge_candidate_inputs(
                connection,
                space=space,
                work_id=work_id,
                allowed_source_snapshots=allowed,
                candidate_types=sorted(KNOWLEDGE_CANDIDATE_TYPES),
                item_ids=[str(row["target_id"]) for row in pending],
            )
            current_hashes = {
                (row["item_id"], row["content_hash"]): row for row in candidates
            }
            claimed: list[dict[str, Any]] = []
            for job in pending:
                key = (str(job["target_id"]), str(job["content_hash"]))
                candidate = current_hashes.get(key)
                if candidate is None:
                    connection.execute(
                        """
                        UPDATE embedding_projection_job
                        SET status = 'superseded', error_text = NULL, updated_at = ?
                        WHERE id = ? AND status = 'pending'
                        """,
                        (now_value, str(job["job_id"])),
                    )
                    continue
                connection.execute(
                    """
                    UPDATE embedding_projection_job
                    SET status = 'running', attempts = attempts + 1, updated_at = ?
                    WHERE id = ? AND status = 'pending'
                    """,
                    (now_value, str(job["job_id"])),
                )
                claimed.append({
                    "job_id": str(job["job_id"]),
                    "target_type": "knowledge_item",
                    "target_id": key[0],
                    "content_hash": key[1],
                    "text": candidate["serialized_text"],
                    "version_id": candidate["version_id"],
                    "candidate_type": candidate["candidate_type"],
                })
                if len(claimed) >= limit:
                    break
            return claimed

    def complete_knowledge_item_embedding_jobs(
        self,
        jobs: list[dict[str, Any]],
        *,
        provider: str,
        model_id: str,
        route_fingerprint: str,
        dimensions: int,
        vectors: list[bytes],
        space: str,
        work_id: str | None,
        allowed_source_snapshots: list[dict[str, str]],
    ) -> dict[str, int]:
        if len(jobs) != len(vectors) or dimensions < 1:
            raise ValueError("candidate embedding result count/dimension is invalid")
        allowed, _ = self._normalize_allowed_snapshots(allowed_source_snapshots)
        now_value = now()
        completed = superseded = 0
        with self.database.transaction() as connection:
            candidates, _counts, _invalid_count = self._load_knowledge_candidate_inputs(
                connection,
                space=space,
                work_id=work_id,
                allowed_source_snapshots=allowed,
                candidate_types=sorted(KNOWLEDGE_CANDIDATE_TYPES),
                item_ids=[str(row["target_id"]) for row in jobs],
            )
            current_hashes = {
                (row["item_id"], row["content_hash"]): row for row in candidates
            }
            for job, vector_blob in zip(jobs, vectors, strict=True):
                job_id = str(job["job_id"])
                job_row = connection.execute(
                    """
                    SELECT target_id, content_hash
                    FROM embedding_projection_job
                    WHERE id = ? AND target_type = 'knowledge_item'
                      AND route_fingerprint = ? AND status = 'running'
                    """,
                    (job_id, route_fingerprint),
                ).fetchone()
                if job_row is None:
                    continue
                key = (str(job_row["target_id"]), str(job_row["content_hash"]))
                if key not in current_hashes:
                    connection.execute(
                        """
                        UPDATE embedding_projection_job
                        SET status = 'superseded', error_text = NULL, updated_at = ?
                        WHERE id = ? AND status = 'running'
                        """,
                        (now_value, job_id),
                    )
                    superseded += 1
                    continue
                connection.execute(
                    """
                    INSERT INTO embedding_projection(
                        id, target_type, target_id, provider, model_id, model_version,
                        dimensions, content_hash, vector_blob, vector_format, status,
                        error_text, created_at, updated_at
                    ) VALUES (?, 'knowledge_item', ?, ?, ?, ?, ?, ?, ?, 'f32le-v1',
                              'complete', NULL, ?, ?)
                    ON CONFLICT(target_type, target_id, provider, model_id, model_version, content_hash)
                    DO UPDATE SET dimensions = excluded.dimensions,
                                  vector_blob = excluded.vector_blob,
                                  vector_format = excluded.vector_format,
                                  status = 'complete', error_text = NULL,
                                  updated_at = excluded.updated_at
                    """,
                    (
                        uuid4().hex, key[0], provider, model_id, route_fingerprint,
                        dimensions, key[1], vector_blob, now_value, now_value,
                    ),
                )
                connection.execute(
                    """
                    UPDATE embedding_projection_job
                    SET status = 'complete', error_text = NULL, updated_at = ?
                    WHERE id = ? AND status = 'running'
                    """,
                    (now_value, job_id),
                )
                completed += 1
        return {"completed": completed, "superseded": superseded}

    def search_knowledge_candidate_vectors(
        self,
        query_vectors: list[list[float]],
        *,
        space: str,
        work_id: str | None,
        allowed_source_snapshots: list[dict[str, str]],
        candidate_types: list[str],
        provider: str,
        model_id: str,
        route_fingerprint: str,
        dimensions: int,
        top_k: int,
    ) -> dict[str, Any]:
        validate_scope(space, work_id)
        types = self._normalize_candidate_types(candidate_types)
        allowed, _ = self._normalize_allowed_snapshots(allowed_source_snapshots)
        self._validate_candidate_query_vectors(query_vectors, dimensions, top_k)
        if not route_fingerprint.strip() or not provider.strip() or not model_id.strip():
            raise ValueError("embedding route identity must be non-empty")
        if not allowed:
            empty_coverage = self._empty_candidate_coverage(types, status="unavailable")
            return {
                "query_results": [{"hits": [], "coverage": empty_coverage} for _ in query_vectors],
                "coverage": empty_coverage,
                "allowed_source_snapshots": [],
            }

        cache = self._knowledge_candidate_matrix_cache()
        matrix: DenseSearchMatrix | None = None
        cache_key: tuple[Any, ...]
        with self.database.connection() as connection:
            connection.execute("BEGIN")
            revision = cache.read_revision(connection)
            cache_key = (
                "knowledge_item", space, work_id,
                tuple(sorted((row["source_id"], row["snapshot_id"]) for row in allowed)),
                tuple(sorted(types)), provider, model_id, route_fingerprint, dimensions,
                revision,
            )
            matrix = cache.get(cache_key) if revision is not None else None
            if matrix is None:
                candidates, eligible_by_type, invalid_evidence_count = self._load_knowledge_candidate_inputs(
                    connection,
                    space=space,
                    work_id=work_id,
                    allowed_source_snapshots=allowed,
                    candidate_types=types,
                )
                projections = self._candidate_projection_rows(
                    connection,
                    candidates,
                    provider=provider,
                    model_id=model_id,
                    route_fingerprint=route_fingerprint,
                )
                indexed_by_type = {candidate_type: 0 for candidate_type in KNOWLEDGE_CANDIDATE_TYPES}
                invalid_vectors_by_type = {candidate_type: 0 for candidate_type in KNOWLEDGE_CANDIDATE_TYPES}
                matrix_rows: list[dict[str, Any]] = []
                normalized_vectors: list[np.ndarray] = []
                for candidate in candidates:
                    projection = projections.get((candidate["item_id"], candidate["content_hash"]))
                    if projection is None:
                        continue
                    candidate_type = candidate["candidate_type"]
                    indexed_by_type[candidate_type] += 1
                    try:
                        vector = self._validate_stored_candidate_vector(projection, dimensions)
                    except (TypeError, ValueError, OverflowError):
                        invalid_vectors_by_type[candidate_type] += 1
                        continue
                    normalized_vectors.append(vector)
                    matrix_rows.append({
                        "item_id": candidate["item_id"],
                        "version_id": candidate["version_id"],
                        "candidate_type": candidate_type,
                        "content_hash": candidate["content_hash"],
                        "summary": candidate["summary"],
                        "typed_payload": candidate["typed_payload"],
                        "semantic_assessment": candidate["semantic_assessment"],
                        "evidence_refs": candidate["evidence_refs"],
                        "capability_gaps": candidate["capability_gaps"],
                    })
                type_coverage: dict[str, dict[str, Any]] = {}
                for candidate_type in sorted(KNOWLEDGE_CANDIDATE_TYPES):
                    eligible = eligible_by_type.get(candidate_type, 0)
                    indexed = indexed_by_type.get(candidate_type, 0)
                    invalid_vectors = invalid_vectors_by_type.get(candidate_type, 0)
                    missing = max(0, eligible - indexed)
                    status = (
                        "unavailable" if dimensions <= 0
                        else "not_requested" if candidate_type not in types
                        else "complete" if eligible and not missing and not invalid_vectors
                        else "partial" if indexed or invalid_vectors
                        else "missing" if eligible
                        else "empty"
                    )
                    type_coverage[candidate_type] = {
                        "eligible_count": eligible,
                        "indexed_count": indexed,
                        "missing_index_count": missing,
                        "invalid_vector_count": invalid_vectors,
                        "invalid_evidence_count": invalid_evidence_count if candidate_type in types else 0,
                        "coverage_status": status,
                    }
                vector_matrix = (
                    np.stack(normalized_vectors) if normalized_vectors
                    else np.empty((0, dimensions), dtype=np.float64)
                )
                vector_matrix.setflags(write=False)
                matrix = DenseSearchMatrix(
                    rows=tuple(matrix_rows),
                    vectors=vector_matrix,
                    eligible_count=sum(eligible_by_type.get(value, 0) for value in types),
                    indexed_count=sum(indexed_by_type.get(value, 0) for value in types),
                    valid_vector_count=len(matrix_rows),
                    invalid_vector_count=sum(invalid_vectors_by_type.get(value, 0) for value in types),
                    weight_bytes=int(vector_matrix.nbytes + sum(
                        len(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
                        for row in matrix_rows
                    )),
                    metadata={
                        "by_type": type_coverage,
                        "invalid_evidence_count": invalid_evidence_count,
                        **self._candidate_capability_coverage(candidates),
                    },
                )
                after_revision = cache.read_revision(connection)
                connection.commit()
                if revision is not None and revision == after_revision:
                    cache.put_if_unchanged(cache_key, matrix, expected_revision=revision)
            else:
                connection.commit()

        query_matrix = self._normalize_candidate_queries(query_vectors, dimensions)
        scores = matrix.vectors @ query_matrix
        valid_scores = np.all(np.isfinite(scores), axis=1)
        coverage = {
            "retrieval_mode": "dense" if matrix.valid_vector_count else "unavailable",
            "coverage_status": self._candidate_coverage_status(matrix),
            "by_type": matrix.metadata.get("by_type", {}) if matrix.metadata else {},
            "eligible_count": matrix.eligible_count,
            "indexed_count": matrix.indexed_count,
            "missing_index_count": max(0, matrix.eligible_count - matrix.indexed_count),
            "invalid_vector_count": matrix.invalid_vector_count,
            "invalid_evidence_count": int(
                matrix.metadata.get("invalid_evidence_count", 0) if matrix.metadata else 0
            ),
            "snapshot_pins_checked": True,
            "requested_types": types,
            **{key: (matrix.metadata or {}).get(key) for key in (
                "structured_capability_status", "capability_gap_count", "capability_gap_reasons",
            )},
        }
        query_results: list[dict[str, Any]] = []
        for query_index in range(query_matrix.shape[1]):
            ranked = [
                (float(scores[index, query_index]), row)
                for index, row in enumerate(matrix.rows)
                if valid_scores[index] and row["candidate_type"] in types
            ]
            ranked.sort(key=lambda pair: (
                -pair[0], pair[1]["candidate_type"],
                str(pair[1]["summary"].get("entity_name", "")).casefold(),
                pair[1]["item_id"],
            ))
            hits = [
                {
                    "item_id": row["item_id"],
                    "version_id": row["version_id"],
                    "candidate_type": row["candidate_type"],
                    "score": score,
                    "rank": rank,
                    "semantic_assessment": row["semantic_assessment"],
                    "summary": row["summary"],
                    "typed_payload": row["typed_payload"],
                    "evidence_refs": row["evidence_refs"],
                    "capability_gaps": row["capability_gaps"],
                }
                for rank, (score, row) in enumerate(ranked[:top_k], start=1)
            ]
            query_results.append({"query_index": query_index, "hits": hits, "coverage": coverage})
        return {
            "query_results": query_results,
            "coverage": coverage,
            "allowed_source_snapshots": allowed,
        }

    def get_knowledge_candidate_embedding_coverage(
        self,
        *,
        space: str,
        work_id: str | None,
        allowed_source_snapshots: list[dict[str, str]],
        candidate_types: list[str] | None = None,
        provider: str = "",
        model_id: str = "",
        route_fingerprint: str | None = None,
        dimensions: int = 0,
    ) -> dict[str, Any]:
        types = self._normalize_candidate_types(candidate_types)
        allowed, _ = self._normalize_allowed_snapshots(allowed_source_snapshots)
        with self.database.connection() as connection:
            candidates, eligible_by_type, invalid_count = self._load_knowledge_candidate_inputs(
                connection,
                space=space,
                work_id=work_id,
                allowed_source_snapshots=allowed,
                candidate_types=types,
            )
            if route_fingerprint is None:
                return {**self._candidate_coverage_payload(
                    eligible_by_type, {key: 0 for key in KNOWLEDGE_CANDIDATE_TYPES},
                    {key: 0 for key in KNOWLEDGE_CANDIDATE_TYPES}, invalid_count, types,
                    route_available=False,
                ), **self._candidate_capability_coverage(candidates)}
            projections = self._candidate_projection_rows(
                connection, candidates, provider=provider, model_id=model_id,
                route_fingerprint=route_fingerprint,
            )
            # Zero means the provider's dimension has not been requested yet.
            # An internally consistent stored scope can still be inspected;
            # actual retrieval always supplies the query vector's dimension.
            inspection_dimensions = dimensions
            if dimensions == 0:
                stored_dimensions = {int(row["dimensions"]) for row in projections.values()}
                if len(stored_dimensions) == 1:
                    inspection_dimensions = stored_dimensions.pop()
            indexed = {key: 0 for key in KNOWLEDGE_CANDIDATE_TYPES}
            invalid = {key: 0 for key in KNOWLEDGE_CANDIDATE_TYPES}
            for candidate in candidates:
                projection = projections.get((candidate["item_id"], candidate["content_hash"]))
                if projection is None:
                    continue
                candidate_type = candidate["candidate_type"]
                indexed[candidate_type] += 1
                try:
                    self._validate_stored_candidate_vector(projection, inspection_dimensions)
                except (TypeError, ValueError, OverflowError):
                    invalid[candidate_type] += 1
        return {**self._candidate_coverage_payload(
            eligible_by_type, indexed, invalid, invalid_count, types, route_available=True,
        ), **self._candidate_capability_coverage(candidates)}

    @staticmethod
    def _empty_candidate_coverage(types: list[str], *, status: str) -> dict[str, Any]:
        zero = {key: 0 for key in KNOWLEDGE_CANDIDATE_TYPES}
        return EmbeddingCandidateRepositoryMixin._candidate_coverage_payload(
            zero, zero, zero, 0, types, route_available=status != "unavailable",
        )

    @staticmethod
    def _candidate_projection_rows(
        connection: sqlite3.Connection,
        candidates: list[dict[str, Any]],
        *,
        provider: str,
        model_id: str,
        route_fingerprint: str,
    ) -> dict[tuple[str, str], dict[str, Any]]:
        projections: dict[tuple[str, str], dict[str, Any]] = {}
        pairs = [(row["item_id"], row["content_hash"]) for row in candidates]
        for offset in range(0, len(pairs), _PROJECTION_PAGE_SIZE):
            page = pairs[offset:offset + _PROJECTION_PAGE_SIZE]
            values = ",".join("(?, ?)" for _ in page)
            rows = connection.execute(
                f"""
                WITH expected(item_id, content_hash) AS (VALUES {values})
                SELECT expected.item_id, expected.content_hash,
                       projection.id AS projection_id, projection.dimensions,
                       projection.vector_blob, projection.vector_format
                FROM expected
                JOIN embedding_projection AS projection
                  ON projection.target_type = 'knowledge_item'
                 AND projection.target_id = expected.item_id
                 AND projection.content_hash = expected.content_hash
                 AND projection.provider = ? AND projection.model_id = ?
                 AND projection.model_version = ? AND projection.status = 'complete'
                """,
                (*[value for pair in page for value in pair], provider, model_id, route_fingerprint),
            ).fetchall()
            for row in rows:
                projections[(str(row["item_id"]), str(row["content_hash"]))] = dict(row)
        return projections

    @staticmethod
    def _validate_stored_candidate_vector(
        projection: dict[str, Any], dimensions: int
    ) -> np.ndarray:
        vector_dimensions = int(projection["dimensions"])
        vector_format = str(projection["vector_format"])
        vector_bytes = bytes(projection["vector_blob"])
        if (
            vector_format != "f32le-v1" or vector_dimensions != dimensions
            or len(vector_bytes) != dimensions * 4
        ):
            raise ValueError("stored candidate vector metadata is incompatible")
        vector = np.frombuffer(vector_bytes, dtype="<f4")
        if vector.shape != (dimensions,) or not np.all(np.isfinite(vector)):
            raise ValueError("stored candidate vector is invalid")
        vector64 = vector.astype(np.float64, copy=True)
        norm = float(np.linalg.norm(vector64))
        if not math.isfinite(norm) or norm <= 0:
            raise ValueError("stored candidate vector has an invalid norm")
        return vector64 / norm

__all__ = ["EmbeddingCandidateRepositoryMixin"]
