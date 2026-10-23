"""Validation and serialization support for published candidate embeddings."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from typing import Any

import numpy as np

from fxi_novel.knowledge.repository_parse_publication import published_graph_candidate_filter
from .candidate_publication_sql import published_candidate_content_sql
from fxi_novel.knowledge.embedding_read_cache import DenseSearchMatrix
from .candidate_query_projection import typed_payload_evidence_is_authorized
from .embedding_knowledge_items import KNOWLEDGE_CANDIDATE_TYPES
from .query_contracts import EvidenceRef


_ENTITY_NAME_PAGE_SIZE = 300


class EmbeddingCandidateSupportMixin:
    """Small read helpers shared by candidate embedding repository operations."""

    @staticmethod
    def _candidate_related_entity_refs(content: dict[str, Any]) -> set[str]:
        references: set[str] = set()
        for key in (
            "entity_ref", "target_entity_ref", "asserted_by_entity_ref",
            "observed_by_entity_ref", "holder_entity_ref",
        ):
            value = content.get(key)
            if isinstance(value, str) and value.strip():
                references.add(value)
        audience = content.get("audience_entity_refs")
        if isinstance(audience, list):
            references.update(value for value in audience if isinstance(value, str) and value.strip())
        event = content.get("typed_event")
        if isinstance(event, dict):
            for key in (
                "actor_id", "asset_id", "location_id", "holder_before_id",
                "holder_after_id", "owner_before_id", "owner_after_id",
            ):
                value = event.get(key)
                if isinstance(value, str) and value.strip():
                    references.add(value)
            participants = event.get("participants")
            if isinstance(participants, list):
                references.update(
                    value.get("entity_id") for value in participants
                    if isinstance(value, dict)
                    and isinstance(value.get("entity_id"), str)
                    and value["entity_id"].strip()
                )
        rule = content.get("typed_rule")
        if isinstance(rule, dict) and isinstance(rule.get("applies_to_entity_id"), str):
            references.add(rule["applies_to_entity_id"])
        instance = content.get("typed_instance")
        if isinstance(instance, dict) and isinstance(instance.get("type_entity_id"), str):
            references.add(instance["type_entity_id"])
        return references

    def _load_candidate_entity_names(
        self,
        connection: sqlite3.Connection,
        *,
        space: str,
        work_id: str | None,
        allowed_source_snapshots: list[dict[str, str]],
        entity_refs: set[str],
    ) -> dict[str, list[str]]:
        """Resolve referenced entity IDs only from published current cards in scope."""
        allowed, _allowed_set = self._normalize_allowed_snapshots(allowed_source_snapshots)
        if not entity_refs or not allowed:
            return {}
        pins_json = json.dumps(allowed, ensure_ascii=False, separators=(",", ":"))
        refs = sorted(entity_refs)
        names: dict[str, set[str]] = {}
        published = published_graph_candidate_filter("item")
        content_sql = published_candidate_content_sql("item", "version.content_json")
        for offset in range(0, len(refs), _ENTITY_NAME_PAGE_SIZE):
            page = refs[offset:offset + _ENTITY_NAME_PAGE_SIZE]
            placeholders = ",".join("?" for _ in page)
            # Resolve the selected candidate identities before joining their
            # evidence. Starting from all allowed snapshots repeats the costly
            # publication checks across an entire imported library.
            rows = connection.execute(
                f"""
                WITH allowed(source_id, snapshot_id) AS MATERIALIZED (
                    SELECT CAST(json_extract(pin.value, '$.source_id') AS TEXT),
                           CAST(json_extract(pin.value, '$.snapshot_id') AS TEXT)
                    FROM json_each(?) AS pin
                )
                SELECT {content_sql} AS content_json, span.quote_hash,
                       span.start_offset AS span_start, span.end_offset AS span_end,
                       substr(snapshot.content, span.start_offset + 1,
                              span.end_offset - span.start_offset) AS span_quote,
                       current.snapshot_id AS current_snapshot_id,
                       source.deleted_at
                FROM knowledge_item AS item
                CROSS JOIN knowledge_card_candidate AS card ON item.id = card.candidate_item_id
                CROSS JOIN knowledge_version AS version
                  ON version.id = card.candidate_version_id
                 AND version.item_id = card.candidate_item_id
                CROSS JOIN knowledge_version_evidence AS link
                  ON link.knowledge_version_id = version.id
                CROSS JOIN evidence_span AS span ON span.id = link.evidence_span_id
                CROSS JOIN source_snapshot AS snapshot ON snapshot.id = span.snapshot_id
                CROSS JOIN source ON source.id = snapshot.source_id
                CROSS JOIN source_current_snapshot AS current
                  ON current.source_id = source.id AND current.snapshot_id = snapshot.id
                JOIN allowed ON allowed.source_id = source.id
                            AND allowed.snapshot_id = snapshot.id
                WHERE item.kind = 'knowledge_graph_candidate'
                  AND item.status NOT IN ('deleted', 'rejected')
                  AND item.space = ? AND item.work_id IS ?
                  AND json_extract(version.content_json, '$.candidate_role') = 'entity'
                  AND json_extract(version.content_json, '$.entity_ref') IN ({placeholders})
                  AND CAST(json_extract(version.content_json, '$.evidence_quote_start') AS INTEGER)
                      >= span.start_offset
                  AND CAST(json_extract(version.content_json, '$.evidence_quote_end') AS INTEGER)
                      <= span.end_offset
                  AND {published}
                  AND source.space = ? AND source.work_id IS ? AND source.deleted_at IS NULL
                """,
                (pins_json, space, work_id, *page, space, work_id),
            ).fetchall()
            for row in rows:
                quote = str(row["span_quote"] or "")
                if (
                    not quote
                    or not row["current_snapshot_id"]
                    or row["deleted_at"] is not None
                    or hashlib.sha256(quote.encode("utf-8")).hexdigest() != str(row["quote_hash"])
                ):
                    continue
                try:
                    content = json.loads(str(row["content_json"]))
                    if not isinstance(content, dict):
                        continue
                    reference = content.get("entity_ref")
                    if not isinstance(reference, str) or reference not in entity_refs:
                        continue
                    entity_name = content.get("identity_display_name") or content.get("entity_name")
                    aliases = content.get("identity_aliases")
                    values = names.setdefault(reference, set())
                    if isinstance(entity_name, str) and entity_name.strip():
                        values.add(entity_name.strip())
                    if isinstance(aliases, list):
                        values.update(value.strip() for value in aliases if isinstance(value, str) and value.strip())
                    name_roles = content.get("identity_name_roles")
                    if isinstance(name_roles, list):
                        values.update(
                            value["display_name"].strip() for value in name_roles
                            if isinstance(value, dict)
                            and isinstance(value.get("display_name"), str)
                            and value["display_name"].strip()
                        )
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
        return {
            reference: sorted(values, key=lambda value: (len(value), value.casefold()))[:20]
            for reference, values in names.items()
            if values
        }

    @staticmethod
    def _typed_payload_evidence_matches(
        typed_payload: dict[str, Any], evidence_refs: list[dict[str, Any]]
    ) -> bool:
        try:
            authorized = tuple(EvidenceRef.model_validate(row) for row in evidence_refs)
        except (TypeError, ValueError):
            return False
        return typed_payload_evidence_is_authorized(typed_payload, authorized)

    @staticmethod
    def _normalize_candidate_queries(
        query_vectors: list[list[float]], dimensions: int
    ) -> np.ndarray:
        matrix = np.asarray(query_vectors, dtype=np.float64).T
        scales = np.max(np.abs(matrix), axis=0)
        if np.any(~np.isfinite(scales)) or np.any(scales <= 0):
            raise ValueError("candidate query vector must have a finite non-zero norm")
        scaled = matrix / scales[None, :]
        norms = np.linalg.norm(scaled, axis=0)
        if np.any(~np.isfinite(norms)) or np.any(norms <= 0):
            raise ValueError("candidate query vector must have a finite non-zero norm")
        normalized = scaled / norms[None, :]
        if normalized.shape[0] != dimensions:
            raise ValueError("candidate query vector dimensions do not match the active route")
        return normalized

    @staticmethod
    def _validate_candidate_query_vectors(
        query_vectors: list[list[float]], dimensions: int, top_k: int
    ) -> None:
        if not isinstance(query_vectors, list) or not 1 <= len(query_vectors) <= 5:
            raise ValueError("candidate vector search accepts one to five queries")
        if not 1 <= dimensions <= 65_536 or not 1 <= top_k <= 100:
            raise ValueError("candidate vector dimensions or top_k are invalid")
        for vector in query_vectors:
            if not isinstance(vector, list) or len(vector) != dimensions or any(
                not isinstance(value, (int, float)) or not math.isfinite(float(value))
                for value in vector
            ):
                raise ValueError("candidate query vector is non-finite or has an invalid dimension")

    @staticmethod
    def _candidate_coverage_status(matrix: DenseSearchMatrix) -> str:
        invalid_evidence = int((matrix.metadata or {}).get("invalid_evidence_count", 0))
        if not matrix.eligible_count:
            return "missing_or_invalid" if invalid_evidence else "empty"
        if not matrix.valid_vector_count:
            return "missing_or_invalid"
        if matrix.indexed_count != matrix.eligible_count or matrix.invalid_vector_count or invalid_evidence:
            return "partial"
        return "complete"

    @staticmethod
    def _candidate_capability_coverage(candidates: list[dict[str, Any]]) -> dict[str, Any]:
        reasons: dict[str, int] = {}
        affected = 0
        for candidate in candidates:
            gaps = candidate.get("capability_gaps", [])
            affected += bool(gaps)
            for reason in gaps:
                reasons[reason] = reasons.get(reason, 0) + 1
        return {
            "structured_capability_status": "partial" if affected else "complete",
            "capability_gap_count": affected,
            "capability_gap_reasons": reasons,
        }

    @staticmethod
    def _candidate_coverage_payload(
        eligible: dict[str, int],
        indexed: dict[str, int],
        invalid: dict[str, int],
        invalid_evidence_count: int,
        requested_types: list[str],
        *,
        route_available: bool,
    ) -> dict[str, Any]:
        by_type: dict[str, dict[str, Any]] = {}
        for candidate_type in sorted(KNOWLEDGE_CANDIDATE_TYPES):
            eligible_count = eligible.get(candidate_type, 0)
            indexed_count = indexed.get(candidate_type, 0)
            invalid_count = invalid.get(candidate_type, 0)
            missing = max(0, eligible_count - indexed_count)
            status = (
                "not_requested" if candidate_type not in requested_types
                else "unavailable" if not route_available
                else "complete" if eligible_count and not missing and not invalid_count
                else "partial" if indexed_count or invalid_count
                else "missing" if eligible_count
                else "empty"
            )
            by_type[candidate_type] = {
                "eligible_count": eligible_count,
                "indexed_count": indexed_count,
                "missing_index_count": missing,
                "invalid_vector_count": invalid_count,
                "invalid_evidence_count": invalid_evidence_count if candidate_type in requested_types else 0,
                "coverage_status": status,
            }
        return {
            "by_type": by_type,
            "status": "unavailable" if not route_available else "partial" if invalid_evidence_count or any(
                row["coverage_status"] in {"partial", "missing"} for row in by_type.values()
            ) else "complete",
            "eligible_count": sum(eligible.get(value, 0) for value in requested_types),
            "indexed_count": sum(indexed.get(value, 0) for value in requested_types),
            "missing_index_count": sum(
                max(0, eligible.get(value, 0) - indexed.get(value, 0)) for value in requested_types
            ),
            "invalid_vector_count": sum(invalid.get(value, 0) for value in requested_types),
            "invalid_evidence_count": invalid_evidence_count,
            "requested_types": requested_types,
        }


__all__ = ["EmbeddingCandidateSupportMixin"]
