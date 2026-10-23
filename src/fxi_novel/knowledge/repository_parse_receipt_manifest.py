"""Manifest membership and CAS checks for parser receipt completion."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import ValidationError

from .repository_support import *
from .query_contracts import EvidenceRef
from .story_coordinates import NarrativePosition


class KnowledgeParseReceiptManifestRepositoryMixin:
    """Keep learning-work membership and parse publication pins transactional."""

    @staticmethod
    def _learning_current_manifest_work_ids_for_source(
        connection: sqlite3.Connection,
        source_id: str,
    ) -> set[str]:
        rows = connection.execute(
            """
            SELECT current_order.work_id
            FROM source
            CROSS JOIN knowledge_story_order_current AS current_order
            JOIN knowledge_project AS project
              ON project.id = current_order.work_id
             AND project.space = 'learning'
             AND project.status = 'active'
            WHERE source.id = ? AND source.space = 'learning'
              AND (
                  source.work_id = current_order.work_id
                  OR EXISTS (
                      SELECT 1
                      FROM knowledge_learning_work_folder AS assigned
                      JOIN knowledge_folder AS root_folder
                        ON root_folder.id = assigned.folder_id
                      JOIN knowledge_folder AS assigned_source_folder
                        ON assigned_source_folder.id = source.folder_id
                       AND assigned_source_folder.space = 'learning'
                       AND assigned_source_folder.work_id IS source.work_id
                      WHERE assigned.work_id = current_order.work_id
                        AND (
                            assigned_source_folder.path = root_folder.path COLLATE NOCASE
                            OR substr(
                                assigned_source_folder.path, 1, length(root_folder.path) + 1
                            ) = root_folder.path || '/' COLLATE NOCASE
                        )
                  )
              )
            ORDER BY current_order.work_id
            """,
            (source_id,),
        ).fetchall()
        return {str(row["work_id"]) for row in rows}

    def _parse_completion_manifest_pin_error(
        self,
        connection: sqlite3.Connection,
        *,
        source_id: str,
        snapshot_id: str,
        source_pin: NarrativePosition | None,
        candidates: Iterable[dict[str, Any]],
        registered_work_ids: set[str],
    ) -> str | None:
        """Validate candidate pins and CAS their source manifest in this write transaction."""
        if source_pin is None and (
            registered_work_ids
            or any(candidate["content"].get("narrative_position") is not None for candidate in candidates)
        ):
            return "missing_manifest_pin"
        if source_pin is not None and (
            source_pin.space != "learning"
            or source_pin.branch_id is not None
            or source_pin.source_id != source_id
            or source_pin.snapshot_id != snapshot_id
            or source_pin.work_id not in registered_work_ids
        ):
            return "stale_manifest_pin"

        if source_pin is None:
            return None

        for candidate in candidates:
            content = candidate["content"]
            raw_position = content.get("narrative_position")
            if raw_position is None:
                raise ValueError(
                    "candidate with a verified source manifest pin must retain its narrative position"
                )
            try:
                position = NarrativePosition.model_validate(raw_position)
            except (ValidationError, TypeError, ValueError) as exc:
                raise ValueError("candidate narrative position is invalid") from exc
            quote_start = content.get("evidence_quote_start")
            if (
                isinstance(quote_start, bool)
                or not isinstance(quote_start, int)
                or position.space != "learning"
                or position.branch_id is not None
                or position.coordinate_basis != "verified"
                or position.manifest_coordinate_status != "verified"
                or position.source_id != source_id
                or position.snapshot_id != snapshot_id
                or position.offset != quote_start
                or position.work_id != source_pin.work_id
                or position.manifest_revision != source_pin.manifest_revision
                or position.manifest_hash != source_pin.manifest_hash
                or position.content_hash != source_pin.content_hash
                or position.narrative_ordinal != source_pin.narrative_ordinal
            ):
                raise ValueError("candidate narrative position does not match its source evidence pin")

        active_work = connection.execute(
            "SELECT 1 FROM knowledge_project WHERE id = ? AND space = 'learning' AND status = 'active'",
            (source_pin.work_id,),
        ).fetchone()
        if active_work is None or not self.is_learning_source_manifest_pin_current_in_connection(
            connection,
            work_id=source_pin.work_id,
            manifest_revision=source_pin.manifest_revision,
            manifest_hash=source_pin.manifest_hash,
            source_id=source_pin.source_id,
            snapshot_id=source_pin.snapshot_id,
            content_hash=source_pin.content_hash,
            narrative_ordinal=source_pin.narrative_ordinal,
        ):
            return "stale_manifest_pin"
        return None

    def _parse_completion_writing_source_pin_error(
        self,
        connection: sqlite3.Connection,
        *,
        source_id: str,
        snapshot_id: str,
        work_id: str | None,
        branch_id: str | None,
        manifest_position: NarrativePosition | None,
        source_pin: Mapping[str, Any] | None,
        candidates: Iterable[dict[str, Any]],
    ) -> str | None:
        """CAS exact writing source and adopted-artifact identity before candidate writes."""
        if (
            manifest_position is None or source_pin is None
            or work_id is None or branch_id is None
        ):
            return "missing_manifest_pin"
        if (
            manifest_position.space != "writing"
            or manifest_position.work_id != work_id
            or manifest_position.branch_id != branch_id
            or manifest_position.source_id != source_id
            or manifest_position.snapshot_id != snapshot_id
            or manifest_position.manifest_coordinate_status != "verified"
            or manifest_position.coordinate_basis != "verified"
        ):
            return "stale_manifest_pin"
        required_pin_fields = {
            "work_id": work_id,
            "branch_id": branch_id,
            "source_id": source_id,
            "snapshot_id": snapshot_id,
            "content_hash": manifest_position.content_hash,
            "artifact_hash": manifest_position.content_hash,
        }
        if any(source_pin.get(key) != value for key, value in required_pin_fields.items()):
            return "stale_manifest_pin"
        if (
            not isinstance(source_pin.get("artifact_id"), str)
            or not source_pin["artifact_id"].strip()
            or type(source_pin.get("artifact_revision")) is not int
            or source_pin["artifact_revision"] < 1
            or type(source_pin.get("event_id")) is not int
        ):
            return "stale_manifest_pin"

        snapshot = connection.execute(
            "SELECT content,content_hash FROM source_snapshot WHERE id=? AND source_id=?",
            (snapshot_id, source_id),
        ).fetchone()
        if snapshot is None or str(snapshot["content_hash"]) != manifest_position.content_hash:
            return "stale_manifest_pin"
        snapshot_content = str(snapshot["content"])
        exact_references: list[EvidenceRef] = []
        prepared = list(candidates)
        if prepared:
            for candidate in prepared:
                content = candidate.get("content")
                if not isinstance(content, Mapping):
                    raise ValueError("writing parse candidate content is malformed")
                start = content.get("evidence_quote_start")
                quote = candidate.get("evidence_quote")
                if (
                    type(start) is not int or start < 0
                    or not isinstance(quote, str) or not quote
                    or snapshot_content[start:start + len(quote)] != quote
                ):
                    raise ValueError("writing parse candidate quote is not exact in its source snapshot")
                exact_references.append(EvidenceRef.from_exact_span(
                    space="writing", work_id=work_id, branch_id=branch_id,
                    source_id=source_id, snapshot_id=snapshot_id,
                    content_hash=manifest_position.content_hash,
                    start_offset=start, quote=quote, purpose="parse_candidate",
                ))
        else:
            anchor = connection.execute(
                "SELECT start_offset,end_offset FROM evidence_span "
                "WHERE snapshot_id=? ORDER BY start_offset,end_offset,id LIMIT 1",
                (snapshot_id,),
            ).fetchone()
            if anchor is None:
                return "stale_manifest_pin"
            start = int(anchor["start_offset"])
            end = int(anchor["end_offset"])
            quote = snapshot_content[start:end]
            if not quote:
                return "stale_manifest_pin"
            exact_references.append(EvidenceRef.from_exact_span(
                space="writing", work_id=work_id, branch_id=branch_id,
                source_id=source_id, snapshot_id=snapshot_id,
                content_hash=manifest_position.content_hash,
                start_offset=start, quote=quote, purpose="parse_candidate",
            ))

        manifest_pin = {
            "status": "verified", "space": "writing", "work_id": work_id,
            "branch_id": branch_id,
            "manifest_revision": manifest_position.manifest_revision,
            "manifest_hash": manifest_position.manifest_hash,
            "manifest_coordinate_status": "verified",
        }
        try:
            resolved = self.writing_narrative_positions(
                connection=connection,
                manifest_pin=manifest_pin,
                work_id=work_id,
                branch_id=branch_id,
                expected_manifest_revision=manifest_position.manifest_revision,
                expected_manifest_hash=manifest_position.manifest_hash,
                sources=[{
                    "source_id": source_id,
                    "snapshot_id": snapshot_id,
                    "content_hash": manifest_position.content_hash,
                    "snapshot_content": snapshot_content,
                    "evidence_refs": exact_references,
                }],
            )
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return "stale_manifest_pin"
        if resolved.get("source_pins", {}).get(source_id) != dict(source_pin):
            return "stale_manifest_pin"
        expected_position = NarrativePosition.model_validate(resolved["positions"][source_id])
        for candidate in prepared:
            content = candidate["content"]
            raw_position = content.get("narrative_position")
            try:
                position = NarrativePosition.model_validate(raw_position)
            except (ValidationError, TypeError, ValueError) as exc:
                raise ValueError("writing parse candidate has no verified narrative position") from exc
            if position.offset != int(content["evidence_quote_start"]):
                raise ValueError("writing candidate position does not anchor its exact quote")
            if (
                position.model_copy(update={
                    "offset": int(content["evidence_quote_start"]),
                }).model_dump(mode="json")
                != expected_position.model_copy(update={
                    "offset": int(content["evidence_quote_start"]),
                }).model_dump(mode="json")
            ):
                raise ValueError("writing candidate position differs from its exact source pin")
        return None


__all__ = ["KnowledgeParseReceiptManifestRepositoryMixin"]
