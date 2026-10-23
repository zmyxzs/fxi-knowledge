"""Graph candidate review and content update writes."""

import re

from .parser_review_ownership import unstamped_semantic_revision_is_owned

from .repository_graph_candidate_writing_scope import (
    validate_writing_candidate_manifest_write_scope,
)
from .repository_parse_publication import preserve_published_graph_version_before_append
from .repository_support import *
from .repository_conversation_effects import (
    assert_conversation_write_allowed, knowledge_effect_state, record_knowledge_effect,
)


class KnowledgeGraphCandidateReviewWriteRepositoryMixin:
    """Append author review decisions and validated candidate updates."""

    def _validate_candidate_manifest_write_scope(
        self,
        connection: sqlite3.Connection,
        *,
        space: str,
        physical_work_id: str | None,
        requested_work_id: str | None,
        requested_manifest_pin: dict[str, Any] | None,
        folder_id: str | None,
        source_ids: list[str] | None,
    ) -> dict[str, dict[str, Any]]:
        """Validate learning or writing source pins inside the candidate-write transaction."""
        if requested_manifest_pin is not None and requested_work_id is None:
            raise ValueError("a manifest-pinned candidate write requires a logical work ID")
        if requested_work_id is None:
            if space != "learning" and physical_work_id is not None:
                raise ValueError("non-learning candidate writes require an explicit physical work scope")
            return {}
        if not source_ids:
            raise ValueError("scoped candidate writes require exact source IDs")

        if space == "writing":
            return validate_writing_candidate_manifest_write_scope(
                self,
                connection,
                physical_work_id=physical_work_id,
                requested_work_id=requested_work_id,
                requested_manifest_pin=requested_manifest_pin,
                folder_id=folder_id,
                source_ids=source_ids,
            )

        if space != "learning":
            if requested_manifest_pin is not None:
                raise ValueError("learning manifest pins cannot authorize candidate writes in another space")
            if requested_work_id != physical_work_id:
                raise ValueError("non-learning candidate writes require the exact physical work scope")
            rows = connection.execute(
                f"""
                SELECT source.id, source.work_id, source.folder_id, current.snapshot_id
                FROM source
                JOIN source_current_snapshot AS current ON current.source_id = source.id
                WHERE source.space = ? AND source.work_id IS ?
                  AND source.deleted_at IS NULL
                  AND source.id IN ({','.join('?' for _ in source_ids)})
                """,
                (space, physical_work_id, *source_ids),
            ).fetchall()
            sources = {str(row["id"]): row for row in rows}
            if set(sources) != set(source_ids):
                raise ValueError("candidate sources do not match the exact physical work scope")
            if any(row["work_id"] != physical_work_id for row in rows):
                raise ValueError("candidate source ownership changed before its write")
            if folder_id is not None:
                folder = connection.execute(
                    "SELECT path FROM knowledge_folder WHERE id = ? AND space = ? AND work_id IS ?",
                    (folder_id, space, physical_work_id),
                ).fetchone()
                if folder is None:
                    raise ValueError("candidate folder is outside the exact physical work scope")
                folder_path = str(folder["path"])
                scoped = connection.execute(
                    f"""
                    SELECT source.id
                    FROM source
                    JOIN knowledge_folder AS source_folder ON source_folder.id = source.folder_id
                    WHERE source.id IN ({','.join('?' for _ in source_ids)})
                      AND source.space = ? AND source.work_id IS ?
                      AND (
                          source.folder_id = ?
                          OR (
                              source_folder.space = ? AND source_folder.work_id IS ?
                              AND substr(source_folder.path, 1, length(?) + 1) = ? || '/'
                          )
                      )
                    """,
                    (
                        *source_ids, space, physical_work_id, folder_id,
                        space, physical_work_id, folder_path, folder_path,
                    ),
                ).fetchall()
                if {str(row["id"]) for row in scoped} != set(source_ids):
                    raise ValueError("candidate sources are outside the selected physical folder scope")
            return {}

        registered = connection.execute(
            "SELECT 1 FROM knowledge_learning_work_folder WHERE work_id = ? LIMIT 1",
            (requested_work_id,),
        ).fetchone() is not None
        if requested_manifest_pin is None:
            if registered or requested_work_id != physical_work_id:
                raise ValueError("registered learning-work candidate writes require a manifest pin")
            return {}

        pin = requested_manifest_pin
        revision = pin.get("manifest_revision")
        manifest_hash = pin.get("manifest_hash")
        if (
            pin.get("status") != "verified"
            or pin.get("manifest_coordinate_status") != "verified"
            or pin.get("work_id") != requested_work_id
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or not isinstance(manifest_hash, str)
            or not re.fullmatch(r"[0-9a-f]{64}", manifest_hash)
        ):
            raise ValueError("candidate write manifest pin is missing or unverified")

        scope = self.resolve_learning_work_scope_in_connection(
            connection,
            requested_work_id,
            source_ids=source_ids,
        )
        if scope["invalid_source_ids"] or set(scope["source_ids"]) != set(source_ids):
            raise ValueError("candidate source is outside the selected learning work")
        physical_owners = set(scope["physical_work_ids"])
        if physical_owners != {physical_work_id}:
            raise ValueError("candidate sources do not share the selected physical owner")
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
                raise ValueError("candidate source is outside the selected learning folder")

        try:
            order = self._read_chapter_order(connection, requested_work_id, revision)
        except ValueError as exc:
            raise ValueError("candidate write manifest revision is unavailable") from exc
        if (
            order["coordinate_status"] != "verified"
            or not order["is_current_revision"]
            or not order["is_complete_for_current_scope"]
            or order["manifest_hash"] != manifest_hash
        ):
            raise ValueError("candidate write manifest changed before identity updates were saved")
        entries = {str(row["source_id"]): row for row in order["entries"]}
        source_rows = {
            str(row["source_id"]): row
            for row in scope["sources"]
            if str(row["source_id"]) in set(source_ids)
        }
        if set(source_rows) != set(source_ids):
            raise ValueError("candidate write source scope could not be resolved exactly")
        positions: dict[str, dict[str, Any]] = {}
        for source_id in source_ids:
            source = source_rows[source_id]
            entry = entries.get(source_id)
            if (
                entry is None
                or not entry["snapshot_is_current"]
                or entry["snapshot_id"] != source.get("snapshot_id")
                or entry["content_hash"] != source.get("content_hash")
                or not self.is_learning_source_manifest_pin_current_in_connection(
                    connection,
                    work_id=requested_work_id,
                    manifest_revision=revision,
                    manifest_hash=manifest_hash,
                    source_id=source_id,
                    snapshot_id=str(entry["snapshot_id"]),
                    content_hash=str(entry["content_hash"]),
                    narrative_ordinal=int(entry["narrative_ordinal"]),
                )
            ):
                raise ValueError("candidate write manifest pin is stale or mismatched")
            positions[source_id] = {
                "domain": "narrative_order",
                "space": "learning",
                "work_id": requested_work_id,
                "branch_id": None,
                "manifest_revision": revision,
                "manifest_hash": manifest_hash,
                "manifest_coordinate_status": "verified",
                "source_id": source_id,
                "snapshot_id": str(entry["snapshot_id"]),
                "content_hash": str(entry["content_hash"]),
                "chapter_title": str(entry["chapter_title"]),
                "narrative_ordinal": int(entry["narrative_ordinal"]),
                "offset": 0,
                "offset_unit": "unicode_codepoint",
                "coordinate_basis": "verified",
            }
        return positions

    @staticmethod
    def _validate_candidate_content_manifest_position(
        content: dict[str, Any],
        *,
        requested_work_id: str,
        space: str,
        manifest_position: dict[str, Any],
        source_id: str,
    ) -> None:
        position = content.get("narrative_position")
        if not isinstance(position, dict):
            raise ValueError("candidate has no verified narrative position")
        fields = (
            "domain", "space", "work_id", "branch_id", "manifest_revision",
            "manifest_hash", "manifest_coordinate_status", "source_id", "snapshot_id",
            "content_hash", "narrative_ordinal", "coordinate_basis",
        )
        if (
            position.get("domain") != "narrative_order"
            or position.get("space") != space
            or position.get("work_id") != requested_work_id
            or position.get("manifest_coordinate_status") != "verified"
            or (space == "writing" and not position.get("branch_id"))
            or (space == "learning" and position.get("branch_id") is not None)
            or position.get("source_id") != source_id
            or any(position.get(field) != manifest_position.get(field) for field in fields)
            or position.get("offset") != content.get("evidence_quote_start")
            or position.get("offset_unit") != "unicode_codepoint"
        ):
            raise ValueError("candidate narrative position does not match the current manifest pin")

    def review_graph_candidate_identity_reference(
        self,
        *,
        space: str,
        work_id: str | None,
        folder_id: str | None,
        fact_item_id: str,
        fact_expected_version_id: str,
        role: str,
        audience_index: int | None,
        decision: str,
        entity_item_id: str | None = None,
        entity_expected_version_id: str | None = None,
    ) -> dict[str, Any]:
        """Append one audited author identity decision after current-evidence CAS checks."""
        validate_scope(space, work_id)
        if role not in {"subject", "target", "asserted_by", "observed_by", "holder", "audience"}:
            raise ValueError("unsupported graph relation role")
        if (role == "audience") != (audience_index is not None):
            raise ValueError("only audience bindings may include an audience index")
        if decision not in {"bind", "keep_unresolved"}:
            raise ValueError("identity decision must be bind or keep_unresolved")
        if decision == "bind":
            if not entity_item_id or not entity_expected_version_id:
                raise ValueError("binding decisions require an entity candidate and expected version")
            if fact_item_id == entity_item_id:
                raise ValueError("a fact candidate cannot resolve to itself")
        elif entity_item_id is not None or entity_expected_version_id is not None:
            raise ValueError("keep_unresolved decisions cannot include an entity candidate")

        with self.database.transaction() as connection:
            assert_conversation_write_allowed(connection)
            effect_before = knowledge_effect_state(connection, fact_item_id)
            if work_id is not None:
                self._ensure_project_exists(connection, space, work_id)
            folder_path: str | None = None
            if folder_id is not None:
                folder = connection.execute(
                    "SELECT path FROM knowledge_folder WHERE id = ? AND space = ? AND work_id IS ?",
                    (folder_id, space, work_id),
                ).fetchone()
                if folder is None:
                    raise ValueError("folder must belong to the selected knowledge scope")
                folder_path = str(folder["path"])
            if folder_id is None:
                folder_clause = "AND source.folder_id IS NULL"
                folder_parameters: list[Any] = []
            else:
                folder_clause = """
                  AND (
                      source.folder_id = ?
                      OR EXISTS (
                          SELECT 1 FROM knowledge_folder AS descendant
                          WHERE descendant.id = source.folder_id
                            AND descendant.space = ? AND descendant.work_id IS ?
                            AND substr(descendant.path, 1, length(?) + 1) = ? || '/'
                      )
                  )
                """
                folder_parameters = [folder_id, space, work_id, folder_path, folder_path]

            def current_candidate(item_id: str) -> sqlite3.Row | None:
                return connection.execute(
                    f"""
                    SELECT item.id AS item_id, item.status, version.id AS version_id,
                           version.version, version.content_json, receipt.id AS receipt_id,
                           source.id AS source_id, current.snapshot_id
                    FROM knowledge_item AS item
                    JOIN knowledge_version AS version ON version.item_id = item.id
                      AND version.version = (
                          SELECT MAX(latest.version) FROM knowledge_version AS latest
                          WHERE latest.item_id = item.id
                      )
                    JOIN knowledge_parse_receipt AS receipt
                      ON receipt.id = item.origin_parse_receipt_id
                    JOIN source ON source.id = receipt.source_id
                    JOIN source_current_snapshot AS current ON current.source_id = source.id
                    WHERE item.id = ? AND item.space = ? AND item.work_id IS ?
                      AND item.kind = 'knowledge_graph_candidate' AND item.status = 'draft'
                      AND receipt.space = ? AND receipt.work_id IS ?
                      AND receipt.status = 'completed'
                      AND receipt.snapshot_id = current.snapshot_id
                      AND source.deleted_at IS NULL
                      AND source.space = ? AND source.work_id IS ?
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
                          OR json_extract(version.content_json, '$.branch_id')
                             = json_extract(
                                 CASE WHEN json_valid(source.document_metadata_json)
                                      THEN source.document_metadata_json ELSE '{{}}' END,
                                 '$.prose_projection.branch_id'
                             )
                      )
                      AND EXISTS (
                          SELECT 1
                          FROM knowledge_version_evidence AS ve
                          JOIN evidence_span AS evidence ON evidence.id = ve.evidence_span_id
                          JOIN source_snapshot AS snapshot ON snapshot.id = evidence.snapshot_id
                          WHERE ve.knowledge_version_id = version.id
                            AND snapshot.source_id = source.id
                            AND snapshot.id = current.snapshot_id
                            AND CAST(json_extract(version.content_json, '$.evidence_quote_start') AS INTEGER)
                                >= evidence.start_offset
                            AND CAST(json_extract(version.content_json, '$.evidence_quote_end') AS INTEGER)
                                <= evidence.end_offset
                      )
                      {folder_clause}
                    """,
                    (
                        item_id, space, work_id, space, work_id, space, work_id,
                        *folder_parameters,
                    ),
                ).fetchone()

            fact = current_candidate(fact_item_id)
            entity = current_candidate(entity_item_id) if entity_item_id is not None else None
            if fact is None or (decision == "bind" and entity is None):
                raise KnowledgeItemVersionConflictError(
                    "fact or entity candidate is no longer current in the selected scope"
                )
            if str(fact["version_id"]) != fact_expected_version_id:
                raise KnowledgeItemVersionConflictError("fact candidate changed after it was opened")
            if (
                entity is not None
                and str(entity["version_id"]) != entity_expected_version_id
            ):
                raise KnowledgeItemVersionConflictError("entity candidate changed after it was selected")

            content = json.loads(str(fact["content_json"]))
            if content.get("candidate_role") != "fact":
                raise ValueError("the selected candidate is not a relation fact")
            entity_content: dict[str, Any] = {}
            identity_group_id: str | None = None
            display_name: str | None = None
            if entity is not None:
                entity_content = json.loads(str(entity["content_json"]))
                if entity_content.get("candidate_role") != "entity":
                    raise ValueError("the selected identity candidate is not an entity")
                identity_group_id = str(entity_content.get("identity_group_id") or "").strip() or None
                if not identity_group_id:
                    raise ValueError("the selected entity has no current resolved identity group")
                display_name = str(
                    entity_content.get("identity_display_name")
                    or entity_content.get("entity_name")
                    or ""
                ).strip() or None
                if not display_name:
                    raise ValueError("the selected entity has no display name")

            relation_fields = {
                "subject": ("entity_ref", "identity_group_id", "identity_display_name", "entity_name"),
                "target": ("target_entity_ref", "target_identity_group_id", "target_identity_display_name", "target_entity_name"),
                "asserted_by": ("asserted_by_entity_ref", "asserted_by_identity_group_id", None, "asserted_by"),
                "observed_by": ("observed_by_entity_ref", "observed_by_identity_group_id", None, "observed_by"),
                "holder": ("holder_entity_ref", "holder_identity_group_id", None, "holder"),
            }
            original_display_name: str
            if role == "audience":
                names = content.get("audience")
                if not isinstance(names, list) or audience_index is None or not 0 <= audience_index < len(names):
                    raise ValueError("the selected audience role no longer exists")
                original_display_name = str(names[audience_index]).strip()
                refs = content.get("audience_entity_refs")
                groups = content.get("audience_identity_group_ids")
                refs = list(refs) if isinstance(refs, list) else [None] * len(names)
                groups = list(groups) if isinstance(groups, list) else [None] * len(names)
                refs.extend([None] * (len(names) - len(refs)))
                groups.extend([None] * (len(names) - len(groups)))
                if decision == "keep_unresolved":
                    groups[audience_index] = None
                else:
                    refs[audience_index] = entity_item_id
                    groups[audience_index] = identity_group_id
                content["audience_entity_refs"] = refs
                content["audience_identity_group_ids"] = groups
            else:
                ref_field, group_field, display_field, name_field = relation_fields[role]
                original_display_name = str(content.get(name_field) or "").strip()
                if role == "subject" and not original_display_name:
                    original_display_name = str(content.get("subject") or "").strip()
                if role == "target" and not original_display_name:
                    raise ValueError("the selected target role no longer has a display name")
                if decision == "keep_unresolved":
                    content[group_field] = None
                    if display_field is not None:
                        content[display_field] = None
                else:
                    content[ref_field] = entity_item_id
                    content[group_field] = identity_group_id
                    if display_field is not None:
                        content[display_field] = display_name
            if not original_display_name:
                raise ValueError("the selected relation role has no original display name")

            evidence_row = connection.execute(
                """SELECT evidence.id AS evidence_span_id
                   FROM knowledge_version_evidence AS ve
                   JOIN evidence_span AS evidence ON evidence.id = ve.evidence_span_id
                   JOIN source_snapshot AS snapshot ON snapshot.id = evidence.snapshot_id
                   WHERE ve.knowledge_version_id = ?
                     AND snapshot.source_id = ? AND snapshot.id = ?
                     AND CAST(json_extract(?, '$.evidence_quote_start') AS INTEGER)
                         >= evidence.start_offset
                     AND CAST(json_extract(?, '$.evidence_quote_end') AS INTEGER)
                         <= evidence.end_offset
                   ORDER BY evidence.start_offset, evidence.id LIMIT 1""",
                (
                    fact["version_id"], fact["source_id"], fact["snapshot_id"],
                    fact["content_json"], fact["content_json"],
                ),
            ).fetchone()
            if evidence_row is None:
                raise KnowledgeItemVersionConflictError(
                    "the relation candidate no longer has its exact current source evidence"
                )

            from fxi_novel.knowledge.models_graph import (
                KnowledgeGraphIdentityBindingProvenance,
                KnowledgeGraphIdentityReviewDecision,
            )

            if decision == "bind":
                binding = KnowledgeGraphIdentityBindingProvenance(
                    role=role,
                    audience_index=audience_index,
                    method="author_confirmed",
                    entity_item_id=str(entity_item_id),
                    entity_version=int(entity["version"]),
                    entity_ref=str(entity_item_id),
                    identity_group_id=str(identity_group_id),
                    display_name=str(display_name),
                    original_display_name=original_display_name,
                ).model_dump(mode="json")
                binding_key = (role, audience_index)
                existing_bindings = content.get("identity_bindings")
                bindings = [
                    value for value in existing_bindings
                    if isinstance(value, dict)
                    and (value.get("role"), value.get("audience_index")) != binding_key
                ] if isinstance(existing_bindings, list) else []
                bindings.append(binding)
                content["identity_bindings"] = bindings
            else:
                existing_bindings = content.get("identity_bindings")
                binding_key = (role, audience_index)
                content["identity_bindings"] = [
                    value for value in existing_bindings
                    if isinstance(value, dict)
                    and (value.get("role"), value.get("audience_index")) != binding_key
                ] if isinstance(existing_bindings, list) else []

            identity_decisions = content.get("identity_review_decisions")
            identity_decisions = list(identity_decisions) if isinstance(identity_decisions, list) else []
            if len(identity_decisions) >= 1000:
                raise ValueError("identity review history reached its per-candidate limit")
            decision_content = KnowledgeGraphIdentityReviewDecision(
                role=role,
                audience_index=audience_index,
                decision="bound" if decision == "bind" else "keep_unresolved",
                reviewer="author",
                reviewed_at=_now(),
                source_id=str(fact["source_id"]),
                source_snapshot_id=str(fact["snapshot_id"]),
                evidence_span_id=str(evidence_row["evidence_span_id"]),
                evidence_quote_start=int(content["evidence_quote_start"]),
                evidence_quote_end=int(content["evidence_quote_end"]),
                mention_name=original_display_name,
                entity_item_id=str(entity_item_id) if entity is not None else None,
                entity_version=int(entity["version"]) if entity is not None else None,
                identity_group_id=identity_group_id,
                display_name=display_name,
            ).model_dump(mode="json")
            identity_decisions.append(decision_content)
            content["identity_review_decisions"] = identity_decisions
            normalized_content = KnowledgeGraphCandidateContent.model_validate(content).model_dump(
                mode="json"
            )
            normalized_content["identity_source_content_hash"] = (
                knowledge_graph_identity_source_content_hash(normalized_content)
            )
            validated_content = KnowledgeGraphCandidateContent.model_validate(normalized_content)
            serialized_content = _dump_json(
                validated_content.model_dump(mode="json"),
                "author-confirmed graph identity binding",
            )

            version_id = uuid4().hex
            created_at = _now()
            next_version = int(fact["version"]) + 1
            evidence_ids = [
                str(row["evidence_span_id"])
                for row in connection.execute(
                    """SELECT evidence_span_id FROM knowledge_version_evidence
                       WHERE knowledge_version_id = ? ORDER BY evidence_span_id""",
                    (fact["version_id"],),
                ).fetchall()
            ]
            preserve_published_graph_version_before_append(connection, fact_item_id)
            connection.execute(
                """INSERT INTO knowledge_version(id, item_id, version, content_json, status, created_at)
                   VALUES (?, ?, ?, ?, 'draft', ?)""",
                (version_id, fact_item_id, next_version, serialized_content, created_at),
            )
            connection.executemany(
                """INSERT INTO knowledge_version_evidence(knowledge_version_id, evidence_span_id)
                   VALUES (?, ?)""",
                [(version_id, evidence_id) for evidence_id in evidence_ids],
            )
            updated = connection.execute(
                "UPDATE knowledge_item SET updated_at = ? WHERE id = ? AND status = 'draft'",
                (created_at, fact_item_id),
            )
            if updated.rowcount != 1:
                raise ValueError("fact candidate changed before its identity binding was saved")
            self._index_knowledge_graph_candidate(
                connection,
                fact_item_id,
                version_id,
                validated_content.model_dump(mode="json"),
            )
            record_knowledge_effect(connection, fact_item_id, effect_before)
        result = self.get_knowledge_item(fact_item_id)
        if result is None:
            raise ValueError("fact candidate disappeared after its identity binding was saved")
        return result


    def apply_graph_candidate_content_updates(
        self,
        *,
        space: str,
        work_id: str | None,
        requested_work_id: str | None = None,
        requested_manifest_pin: dict[str, Any] | None = None,
        folder_id: str | None,
        updates: dict[str, dict[str, Any]],
        expected_versions: dict[str, int] | None = None,
        status_by_item_id: dict[str, str] | None = None,
        source_ids: list[str] | None = None,
        parser_version: str | None = None,
        allow_prior_ai_exclusions: bool = False,
        reviewer_version: str | None = None,
        identity_metadata_only: bool = False,
        processing_unit_id: str | None = None,
        targeted_review_attempt_id: str | None = None,
    ) -> int:
        """Append parser-owned candidate metadata within the selected current source scope."""
        validate_scope(space, work_id)
        if not updates:
            return 0
        if source_ids is not None and (
            not source_ids or len(source_ids) != len(set(source_ids))
        ):
            raise ValueError("candidate update source_ids must be non-empty and unique")
        if expected_versions is not None and set(expected_versions) != set(updates):
            raise ValueError("candidate update versions must match updated item IDs")
        if identity_metadata_only and expected_versions is None:
            raise ValueError("identity metadata updates require expected candidate versions")
        if identity_metadata_only and status_by_item_id is not None:
            raise ValueError("identity metadata updates must preserve candidate statuses")
        if status_by_item_id is not None and set(status_by_item_id) != set(updates):
            raise ValueError("candidate update statuses must match updated item IDs")
        if allow_prior_ai_exclusions and not reviewer_version:
            raise ValueError("a reviewer version is required to reopen prior AI exclusions")
        item_statuses = status_by_item_id or {item_id: "draft" for item_id in updates}
        if any(status not in {"draft", "rejected"} for status in item_statuses.values()):
            raise ValueError("AI candidate updates may only preserve drafts or exclude candidates")
        content_json = {item_id: _dump_json(content, "identity candidate content")
                        for item_id, content in updates.items()}
        now = _now()
        with self.database.transaction() as connection:
            if targeted_review_attempt_id is not None:
                from .repository_parse_stage_audit import targeted_check_request
                attempt = connection.execute(
                    "SELECT * FROM knowledge_parse_stage_attempt WHERE id=?",
                    (targeted_review_attempt_id,),
                ).fetchone()
                if attempt is None or attempt["status"] != "validated":
                    raise ValueError("targeted update requires a successful stored check")
                refs = json.loads(attempt["candidate_refs_json"])
                payload = targeted_check_request(json.loads(attempt["request_json"]), attempt["phase"], refs)
                targets = [ref for ref in refs if ref["role"] == "target"]
                if (payload is None or set(updates) != {targets[0]["item_id"]}
                        or not self._stage_attempt_pins_current(connection, attempt)):
                    raise ValueError("targeted update input or running batch lease changed")
            if processing_unit_id is not None:
                unit = connection.execute(
                    "SELECT * FROM knowledge_parse_processing_unit WHERE id = ?",
                    (processing_unit_id,),
                ).fetchone()
                active_batch = connection.execute(
                    "SELECT * FROM knowledge_parse_batch WHERE id = ? AND status = 'running'",
                    (unit["batch_id"],),
                ).fetchone() if unit is not None else None
                if (
                    unit is None or unit["execution_status"] not in {"prepared", "running"}
                    or active_batch is None or unit["phase"] != "identity_resolution"
                ):
                    raise ValueError("identity parse processing unit is stale or inactive")
                self._validate_parse_processing_unit_current_in_transaction(
                    connection, batch=active_batch, row=unit,
                )
            if work_id is not None:
                self._ensure_project_exists(connection, space, work_id)
            manifest_positions = self._validate_candidate_manifest_write_scope(
                connection,
                space=space,
                physical_work_id=work_id,
                requested_work_id=requested_work_id,
                requested_manifest_pin=requested_manifest_pin,
                folder_id=folder_id,
                source_ids=source_ids,
            )
            folder_path: str | None = None
            if folder_id is not None:
                folder = connection.execute(
                    "SELECT path FROM knowledge_folder WHERE id = ? AND space = ? AND work_id IS ?",
                    (folder_id, space, work_id),
                ).fetchone()
                if folder is None:
                    raise ValueError("folder must belong to the selected knowledge scope")
                folder_path = str(folder["path"])
            prepared: list[tuple[str, str, str, int, str, list[str]]] = []
            for item_id, serialized_content in content_json.items():
                if folder_id is None:
                    folder_clause = (
                        "AND source.folder_id IS NULL" if source_ids is None else ""
                    )
                    folder_parameters: list[Any] = []
                else:
                    folder_clause = """
                      AND (
                          source.folder_id = ?
                          OR EXISTS (
                              SELECT 1 FROM knowledge_folder AS descendant
                              WHERE descendant.id = source.folder_id
                                AND descendant.space = ? AND descendant.work_id IS ?
                                AND substr(descendant.path, 1, length(?) + 1) = ? || '/'
                          )
                      )
                    """
                    folder_parameters = [folder_id, space, work_id, folder_path, folder_path]
                source_clause = ""
                source_parameters: list[Any] = []
                if source_ids is not None:
                    source_clause = f"AND source.id IN ({','.join('?' for _ in source_ids)})"
                    source_parameters = list(source_ids)
                parser_clause = "AND receipt.parser_version = ?" if parser_version is not None else ""
                parser_parameters = [parser_version] if parser_version is not None else []
                row = connection.execute(
                    f"""
                    SELECT item.status, item.origin_parse_receipt_id, receipt.status AS receipt_status,
                           receipt.parser_version, receipt.branch_id AS receipt_branch_id,
                           version.id AS version_id, version.version, version.content_json,
                           source.id AS source_id, source.work_id AS source_work_id,
                           source.folder_id, source.deleted_at, current.snapshot_id,
                           receipt.snapshot_id AS receipt_snapshot_id
                    FROM knowledge_item AS item
                    JOIN knowledge_version AS version ON version.item_id = item.id
                      AND version.version = (
                          SELECT MAX(latest.version) FROM knowledge_version AS latest
                          WHERE latest.item_id = item.id
                      )
                    JOIN knowledge_parse_receipt AS receipt
                      ON receipt.id = item.origin_parse_receipt_id
                    JOIN source ON source.id = receipt.source_id
                    JOIN source_current_snapshot AS current ON current.source_id = source.id
                    WHERE item.id = ? AND item.space = ? AND item.work_id IS ?
                      AND item.kind = 'knowledge_graph_candidate'
                      AND receipt.space = ? AND receipt.work_id IS ?
                      AND source.work_id IS ?
                      AND receipt.status = 'completed'
                      AND receipt.snapshot_id = current.snapshot_id
                      AND source.deleted_at IS NULL
                      {folder_clause}
                      {source_clause}
                      {parser_clause}
                    """,
                    (
                        item_id, space, work_id, space, work_id, work_id,
                        *folder_parameters, *source_parameters, *parser_parameters,
                    ),
                ).fetchone()
                if (
                    row is None
                    or row["receipt_status"] != "completed"
                    or row["snapshot_id"] != row["receipt_snapshot_id"]
                    or row["version_id"] is None
                ):
                    raise ValueError("candidate changed scope or parse state before save")
                previous_content = json.loads(row["content_json"])
                next_content = json.loads(serialized_content)
                if (
                    previous_content.get("branch_id") != row["receipt_branch_id"]
                    or next_content.get("branch_id") != row["receipt_branch_id"]
                ):
                    raise ValueError("candidate branch does not match its current parse receipt")
                if manifest_positions:
                    if requested_work_id is None:
                        raise ValueError("candidate manifest write lost its logical work scope")
                    manifest_position = manifest_positions.get(str(row["source_id"]))
                    if manifest_position is None:
                        raise ValueError("candidate source is absent from its manifest pin")
                    self._validate_candidate_content_manifest_position(
                        previous_content,
                        requested_work_id=requested_work_id,
                        space=space,
                        manifest_position=manifest_position,
                        source_id=str(row["source_id"]),
                    )
                    self._validate_candidate_content_manifest_position(
                        next_content,
                        requested_work_id=requested_work_id,
                        space=space,
                        manifest_position=manifest_position,
                        source_id=str(row["source_id"]),
                    )
                if (
                    expected_versions is not None
                    and int(row["version"]) != expected_versions[item_id]
                ):
                    raise ValueError("candidate changed after semantic review and was preserved")
                prior_decisions = previous_content.get("identity_review_decisions") or []
                if prior_decisions:
                    if (next_content.get("identity_review_decisions") or []) != prior_decisions:
                        raise ValueError(
                            "candidate review cannot replace author identity review history"
                        )
                    identity_link_fields = (
                        "entity_ref", "entity_name", "identity_group_id", "identity_display_name",
                        "target_entity_ref", "target_entity_name", "target_identity_group_id",
                        "target_identity_display_name", "subject", "asserted_by",
                        "asserted_by_entity_ref", "asserted_by_identity_group_id", "observed_by",
                        "observed_by_entity_ref", "observed_by_identity_group_id", "holder",
                        "holder_entity_ref", "holder_identity_group_id", "audience",
                        "audience_entity_refs", "audience_identity_group_ids",
                        "evidence_quote_start", "evidence_quote_end",
                    )
                    if not identity_metadata_only and any(
                        previous_content.get(field) != next_content.get(field)
                        for field in identity_link_fields
                    ):
                        raise ValueError(
                            "candidate review cannot invalidate an author identity decision; preserve this candidate"
                        )
                if identity_metadata_only:
                    if previous_content.get("candidate_role") == "fact":
                        protected_roles = {
                            (str(value.get("role")), value.get("audience_index"))
                            for value in [
                                *(previous_content.get("identity_review_decisions") or []),
                                *(previous_content.get("identity_bindings") or []),
                            ]
                            if isinstance(value, dict)
                        }
                        fields_by_role = {
                            "subject": ("identity_group_id", "identity_display_name"),
                            "target": ("target_identity_group_id", "target_identity_display_name"),
                            "asserted_by": ("asserted_by_identity_group_id",),
                            "observed_by": ("observed_by_identity_group_id",),
                            "holder": ("holder_identity_group_id",),
                        }
                        for role, audience_index in protected_roles:
                            if role == "audience" and isinstance(audience_index, int):
                                previous_groups = previous_content.get("audience_identity_group_ids") or []
                                next_groups = list(next_content.get("audience_identity_group_ids") or [])
                                if audience_index < len(previous_groups):
                                    next_groups.extend([None] * (audience_index + 1 - len(next_groups)))
                                    next_groups[audience_index] = previous_groups[audience_index]
                                    next_content["audience_identity_group_ids"] = next_groups
                            else:
                                for field in fields_by_role.get(role, ()):
                                    next_content[field] = previous_content.get(field)
                        KnowledgeGraphCandidateContent.model_validate(next_content)
                        serialized_content = _dump_json(
                            next_content,
                            "protected author graph identity decisions",
                        )
                    previous_bindings = previous_content.get("identity_bindings") or []
                    next_bindings = next_content.get("identity_bindings") or []
                    if previous_bindings != next_bindings:
                        raise ValueError(
                            "parser identity reconciliation cannot change author-confirmed bindings"
                        )
                    previous_decisions = previous_content.get("identity_review_decisions") or []
                    next_decisions = next_content.get("identity_review_decisions") or []
                    if previous_decisions != next_decisions:
                        raise ValueError(
                            "parser identity reconciliation cannot change author identity review decisions"
                        )
                    identity_fields = set(KNOWLEDGE_GRAPH_IDENTITY_METADATA_FIELDS) - {
                        "semantic_review", "identity_bindings", "identity_review_decisions"
                    }
                    changed_fields = {
                        key
                        for key in previous_content.keys() | next_content.keys()
                        if key not in identity_fields
                        and key not in {"identity_bindings", "identity_review_decisions"}
                        and previous_content.get(key) != next_content.get(key)
                    }
                    normalized_entity_kind_change = (
                        previous_content.get("candidate_role") == "entity"
                        and next_content.get("candidate_role") == "entity"
                        and all(
                            next_content.get(field)
                            == normalize_knowledge_graph_entity_kind(
                                str(previous_content.get(field) or "other")
                            )
                            for field in ("category", "entity_kind")
                        )
                        and changed_fields.issubset({"category", "entity_kind"})
                    )
                    if changed_fields and not normalized_entity_kind_change:
                        def preview_identity_guard_value(value: Any) -> Any:
                            if isinstance(value, dict):
                                return {
                                    "type": "object",
                                    "keys": sorted(value),
                                    "assessment": value.get("assessment"),
                                    "reviewer_version": value.get("reviewer_version"),
                                }
                            if isinstance(value, list):
                                return {"type": "array", "length": len(value)}
                            if value is None or isinstance(value, (bool, int, float)):
                                return value
                            return {"type": type(value).__name__}

                        changed_details = {
                            field: {
                                "previous_present": field in previous_content,
                                "next_present": field in next_content,
                                "previous": preview_identity_guard_value(
                                    previous_content.get(field)
                                ),
                                "next": preview_identity_guard_value(next_content.get(field)),
                            }
                            for field in sorted(changed_fields)
                        }
                        raise ValueError(
                            "identity reconciliation may only update identity metadata; "
                            f"candidate {item_id} also changed {changed_details}"
                        )
                previous_review = previous_content.get("semantic_review")
                can_reopen_ai_exclusion = (
                    allow_prior_ai_exclusions
                    and row["status"] == "rejected"
                    and isinstance(previous_review, dict)
                    and previous_review.get("assessment") == "excluded"
                    and previous_review.get("reviewer_version") != reviewer_version
                    and bool(previous_review.get("reviewer_version"))
                )
                if identity_metadata_only:
                    if row["status"] not in {"draft", "rejected"}:
                        raise ValueError("identity metadata updates require an active candidate")
                elif row["status"] != "draft" and not can_reopen_ai_exclusion:
                    raise ValueError("only drafts or prior AI exclusions may be re-reviewed")
                source_hash = knowledge_graph_identity_source_content_hash(previous_content)
                recorded_hash = previous_content.get("identity_source_content_hash")
                if (
                    int(row["version"]) > 1
                    and recorded_hash != source_hash
                    and not identity_metadata_only
                    and not (recorded_hash is None and unstamped_semantic_revision_is_owned(
                        connection, item_id=item_id, version_id=str(row["version_id"])))
                ):
                    raise ValueError("candidate was edited outside the parser and was preserved")
                target_status = (
                    str(row["status"])
                    if identity_metadata_only
                    else item_statuses[item_id]
                )
                review_note = str(next_content.get("review_note") or "").strip()
                if target_status == "rejected" and not review_note:
                    raise ValueError("excluded AI candidates require a review reason")
                if target_status == "draft" and next_content.get("review_note") is not None:
                    raise ValueError("draft AI candidates cannot carry a rejection review note")
                if not identity_metadata_only:
                    normalized = KnowledgeGraphCandidateContent.model_validate(next_content).model_dump(mode="json")
                    normalized["identity_source_content_hash"] = knowledge_graph_identity_source_content_hash(normalized)
                    serialized_content = _dump_json(normalized, "audited parser candidate content")
                evidence_ids = [
                    str(evidence_row["evidence_span_id"])
                    for evidence_row in connection.execute(
                        """SELECT evidence_span_id FROM knowledge_version_evidence
                           WHERE knowledge_version_id = ? ORDER BY evidence_span_id""",
                        (row["version_id"],),
                    ).fetchall()
                ]
                prepared.append((
                    item_id,
                    serialized_content,
                    target_status,
                    str(row["status"]),
                    int(row["version"]) + 1,
                    now,
                    evidence_ids,
                ))
            for item_id, serialized_content, target_status, previous_status, version_number, created_at, evidence_ids in prepared:
                assert_conversation_write_allowed(connection)
                effect_before = knowledge_effect_state(connection, item_id)
                version_id = uuid4().hex
                preserve_published_graph_version_before_append(connection, item_id)
                connection.execute(
                    """INSERT INTO knowledge_version(id, item_id, version, content_json, status, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (version_id, item_id, version_number, serialized_content, target_status, created_at),
                )
                connection.executemany(
                    """INSERT INTO knowledge_version_evidence(knowledge_version_id, evidence_span_id)
                       VALUES (?, ?)""",
                    [(version_id, evidence_id) for evidence_id in evidence_ids],
                )
                connection.execute(
                    "UPDATE knowledge_item SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
                    (target_status, created_at, item_id, previous_status),
                )
                self._index_knowledge_graph_candidate(
                    connection,
                    item_id,
                    version_id,
                    json.loads(serialized_content),
                )
                record_knowledge_effect(connection, item_id, effect_before)
            if processing_unit_id is not None:
                self._complete_parse_processing_unit_in_transaction(
                    connection,
                    unit_id=processing_unit_id,
                    outcome={
                        "kind": "candidate_updates_applied",
                        "candidate_ids": sorted(content_json),
                        "updated_count": len(prepared),
                    },
                )
        return len(content_json)

    def verified_candidate_semantic_baselines(self, rows):
        with self.database.connection() as connection:
            return {str(row["item_id"]): str(row["version_id"]) for row in rows
                    if unstamped_semantic_revision_is_owned(connection, item_id=str(row["item_id"]),
                                                           version_id=str(row["version_id"]))}

