"""Resolve parse request identity without changing physical source ownership."""

from .repository_support import *
from .query_contracts import EvidenceRef


class KnowledgeParseRequestScopeRepositoryMixin:
    def _validate_parse_batch_manifest_pin(
        self,
        connection: sqlite3.Connection,
        *,
        space: str,
        requested_work_id: str | None,
        work_id: str | None,
        branch_id: str | None,
        folder_id: str | None,
        source_ids: list[str],
        manifest_pin: dict[str, Any],
    ) -> None:
        if space == "writing":
            if (
                not requested_work_id
                or work_id != requested_work_id
                or not branch_id
                or manifest_pin.get("status") != "verified"
                or manifest_pin.get("space") != "writing"
                or manifest_pin.get("work_id") != requested_work_id
                or manifest_pin.get("branch_id") != branch_id
                or manifest_pin.get("manifest_coordinate_status") != "verified"
            ):
                raise ValueError("writing parse manifest pin must match its exact work and branch")
            return
        if space != "learning" or requested_work_id is None:
            raise ValueError("verified narrative manifest pins require a registered work scope")
        if (
            manifest_pin.get("status") != "verified"
            or manifest_pin.get("space") not in {None, "learning"}
            or manifest_pin.get("work_id") != requested_work_id
        ):
            raise ValueError("parse batch manifest pin is not verified for its requested work")
        scope = self.resolve_learning_work_scope_in_connection(
            connection, requested_work_id, source_ids=source_ids,
        )
        if scope["invalid_source_ids"] or set(scope["source_ids"]) != set(source_ids):
            raise ProseLifecycleEventSupersededError(
                "parse batch selected sources left the registered learning-work scope"
            )
        if folder_id is not None:
            folder_scope = self.resolve_learning_work_scope_in_connection(
                connection,
                requested_work_id,
                folder_ids=[folder_id],
                source_ids=[],
            )
            if (
                folder_scope["invalid_folder_ids"]
                or not set(source_ids).issubset(set(folder_scope["source_ids"]))
            ):
                raise ProseLifecycleEventSupersededError(
                    "parse batch folder selection left the registered learning-work scope"
                )
        owners = set(scope["physical_work_ids"])
        if len(owners) != 1 or next(iter(owners)) != work_id:
            raise ValueError("parse batch sources must have exactly one physical owner")

    def _validate_parse_batch_manifest_sources_in_connection(
        self,
        connection: sqlite3.Connection,
        *,
        requested_work_id: str,
        branch_id: str | None,
        sources: list[dict[str, Any]],
        manifest_pin: dict[str, Any],
    ) -> None:
        positions = manifest_pin.get("positions")
        if not isinstance(positions, dict) or set(positions) != {
            str(source["source_id"]) for source in sources
        }:
            raise ValueError("parse batch manifest pin must cover every selected source exactly")
        revision = manifest_pin.get("manifest_revision")
        manifest_hash = manifest_pin.get("manifest_hash")
        if manifest_pin.get("space") == "writing":
            if not branch_id or manifest_pin.get("branch_id") != branch_id:
                raise ValueError("writing manifest pin does not match the batch branch")
            current_inputs: list[dict[str, Any]] = []
            for source in sources:
                source_id = str(source["source_id"])
                source_row = connection.execute(
                    "SELECT content,content_hash FROM source_snapshot WHERE id=? AND source_id=?",
                    (source.get("snapshot_id"), source_id),
                ).fetchone()
                if source_row is None:
                    raise ProseLifecycleEventSupersededError(
                        "writing parse source snapshot disappeared before batch queueing"
                    )
                evidence = connection.execute(
                    """SELECT id,start_offset,end_offset FROM evidence_span
                       WHERE snapshot_id=? ORDER BY start_offset,end_offset,id LIMIT 1""",
                    (source.get("snapshot_id"),),
                ).fetchone()
                if evidence is None:
                    raise ValueError("writing parse source has no exact evidence span")
                content = str(source_row["content"])
                start = int(evidence["start_offset"])
                end = int(evidence["end_offset"])
                quote = content[start:end]
                reference = EvidenceRef.from_exact_span(
                    space="writing", work_id=requested_work_id, branch_id=branch_id,
                    source_id=source_id, snapshot_id=str(source["snapshot_id"]),
                    content_hash=str(source_row["content_hash"]),
                    start_offset=start, quote=quote, purpose="parse_candidate",
                )
                current_inputs.append({
                    "source_id": source_id,
                    "snapshot_id": str(source["snapshot_id"]),
                    "content_hash": str(source_row["content_hash"]),
                    "snapshot_content": content,
                    "evidence_refs": [reference],
                })
            resolved = self.writing_narrative_positions(
                connection=connection,
                manifest_pin={
                    "status": "verified", "space": "writing",
                    "work_id": requested_work_id, "branch_id": branch_id,
                    "manifest_revision": revision, "manifest_hash": manifest_hash,
                    "manifest_coordinate_status": "verified",
                },
                work_id=requested_work_id,
                branch_id=branch_id,
                sources=current_inputs,
            )
            expected_positions = manifest_pin.get("positions")
            if not isinstance(expected_positions, dict):
                raise ValueError("writing parse batch manifest pin omitted exact source positions")
            for source_id, position in resolved["positions"].items():
                expected = expected_positions.get(source_id)
                if not isinstance(expected, dict):
                    raise ValueError("writing parse batch source position is missing")
                # Batch enqueue uses the first evidence span as its anchor. Only
                # source/manifest identity is compared here; per-segment offsets
                # are revalidated when prepared_parse builds its exact inputs.
                if any(expected.get(key) != position.get(key) for key in (
                    "space", "work_id", "branch_id", "manifest_revision", "manifest_hash",
                    "manifest_coordinate_status", "source_id", "snapshot_id", "content_hash",
                    "narrative_ordinal", "coordinate_basis",
                )):
                    raise ProseLifecycleEventSupersededError(
                        "writing parse batch source pin changed before queueing"
                    )
            expected_source_pins = manifest_pin.get("source_pins")
            if expected_source_pins != resolved.get("source_pins"):
                raise ProseLifecycleEventSupersededError(
                    "writing adopted artifact identity changed before batch queueing"
                )
            return
        for source in sources:
            source_id = str(source["source_id"])
            position = positions[source_id]
            if (
                not isinstance(position, dict)
                or position.get("work_id") != requested_work_id
                or position.get("source_id") != source_id
                or position.get("snapshot_id") != source.get("snapshot_id")
            ):
                raise ValueError("parse batch manifest position does not match its source snapshot")
            if not self.is_learning_source_manifest_pin_current_in_connection(
                connection,
                work_id=requested_work_id,
                manifest_revision=revision,
                manifest_hash=manifest_hash,
                source_id=source_id,
                snapshot_id=str(source.get("snapshot_id") or ""),
                content_hash=str(position.get("content_hash") or ""),
                narrative_ordinal=position.get("narrative_ordinal"),
            ):
                raise ProseLifecycleEventSupersededError(
                    "parse batch chapter-order manifest changed before queueing"
                )

    def resolve_knowledge_parse_request_scope(
        self,
        *,
        space: str,
        requested_work_id: str | None,
        folder_id: str | None,
        sources: list[dict[str, Any]],
        branch_id: str | None = None,
    ) -> dict[str, Any]:
        """Resolve caller scope to exact current snapshots and physical owner."""
        validate_scope(space, requested_work_id)
        if not 1 <= len(sources) <= 1000:
            raise ValueError("parse requests must select between 1 and 1000 sources")
        source_ids = [str(source.get("source_id") or "") for source in sources]
        if any(not source_id for source_id in source_ids) or len(set(source_ids)) != len(source_ids):
            raise ValueError("parse request source IDs must be unique and non-empty")
        with self.database.connection() as connection:
            rows = connection.execute(
                """SELECT source.id, source.space, source.work_id, source.folder_id,
                          source.deleted_at, current.snapshot_id
                   FROM source
                   LEFT JOIN source_current_snapshot AS current ON current.source_id=source.id
                   WHERE source.id IN (""" + ",".join("?" for _ in source_ids) + ")",
                source_ids,
            ).fetchall()
            by_id = {str(row["id"]): row for row in rows}
            if len(by_id) != len(source_ids):
                raise ValueError("parse request contains a missing source")
            for selected in sources:
                source_id = str(selected["source_id"])
                row = by_id[source_id]
                if (
                    row["deleted_at"] is not None
                    or row["space"] != space
                    or row["snapshot_id"] != str(selected.get("snapshot_id") or "")
                    or row["folder_id"] != selected.get("folder_id")
                ):
                    raise ProseLifecycleEventSupersededError(
                        "parse request source or current snapshot changed during scope resolution"
                    )

            is_registered_learning_work = bool(
                space == "learning" and requested_work_id is not None
            )
            manifest_pin = None
            if space == "writing":
                if not requested_work_id or not branch_id:
                    raise ValueError("writing parse scope requires an explicit work and branch")
                physical_owners = {row["work_id"] for row in by_id.values()}
                if physical_owners != {requested_work_id}:
                    raise ValueError("writing parse sources must belong to the requested work")
                physical_work_id = requested_work_id
                selected_pins: list[dict[str, Any]] = []
                for source in sources:
                    source_id = str(source["source_id"])
                    snapshot = connection.execute(
                        "SELECT content,content_hash FROM source_snapshot WHERE id=? AND source_id=?",
                        (source.get("snapshot_id"), source_id),
                    ).fetchone()
                    if snapshot is None:
                        raise ProseLifecycleEventSupersededError(
                            "writing parse source snapshot changed during scope resolution"
                        )
                    evidence = connection.execute(
                        """SELECT id,start_offset,end_offset FROM evidence_span
                           WHERE snapshot_id=? ORDER BY start_offset,end_offset,id LIMIT 1""",
                        (source.get("snapshot_id"),),
                    ).fetchone()
                    if evidence is None:
                        raise ValueError("writing parse source has no exact evidence span")
                    content = str(snapshot["content"])
                    start = int(evidence["start_offset"])
                    end = int(evidence["end_offset"])
                    reference = EvidenceRef.from_exact_span(
                        space="writing", work_id=requested_work_id, branch_id=branch_id,
                        source_id=source_id, snapshot_id=str(source["snapshot_id"]),
                        content_hash=str(snapshot["content_hash"]),
                        start_offset=start, quote=content[start:end], purpose="parse_candidate",
                    )
                    selected_pins.append({
                        "source_id": source_id,
                        "snapshot_id": str(source["snapshot_id"]),
                        "content_hash": str(snapshot["content_hash"]),
                        "snapshot_content": content,
                        "evidence_refs": [reference],
                    })
                resolved = self.writing_narrative_positions(
                    connection=connection,
                    manifest_pin=None,
                    work_id=requested_work_id,
                    branch_id=branch_id,
                    sources=selected_pins,
                )
                manifest_pin = {
                    **resolved["manifest_pin"],
                    "positions": resolved["positions"],
                    "source_pins": resolved["source_pins"],
                }
            elif is_registered_learning_work:
                scope = self.resolve_learning_work_scope_in_connection(
                    connection, requested_work_id, source_ids=source_ids,
                )
                if scope["invalid_source_ids"] or set(scope["source_ids"]) != set(source_ids):
                    raise ValueError("selected sources are outside the registered learning-work scope")
                physical_owners = scope["physical_work_ids"]
                if len(physical_owners) != 1:
                    raise ValueError("parse request sources have multiple or no physical owners")
                physical_work_id = physical_owners[0]
                manifest_pin = self.resolve_learning_source_manifest_pin(
                    source_ids=source_ids,
                    scope_work_id=requested_work_id,
                    folder_id=folder_id,
                )
                if manifest_pin.get("status") != "verified":
                    raise ValueError(
                        "registered learning-work parsing requires a verified current chapter manifest"
                    )
                manifest_pin = {"space": "learning", **manifest_pin}
            else:
                physical_owners = {row["work_id"] for row in by_id.values()}
                if len(physical_owners) != 1:
                    raise ValueError("parse request sources have multiple physical owners")
                physical_work_id = next(iter(physical_owners))
                if physical_work_id != requested_work_id:
                    raise ValueError("selected sources do not belong to the requested parse scope")
            resolved_sources = [
                {
                    **source,
                    **({"narrative_position": manifest_pin["positions"][str(source["source_id"])]}
                       if manifest_pin is not None else {}),
                }
                for source in sources
            ]
            return {
                "space": space,
                "requested_work_id": requested_work_id,
                "physical_work_id": physical_work_id,
                "folder_id": folder_id,
                "manifest_pin": manifest_pin,
                "sources": resolved_sources,
            }
