"""Knowledge persistence methods for temporal records."""

from .repository_support import *
from .scope_selection import all_version_source_dependencies_filter
from .story_observations import current_register_pin_source_ids


class KnowledgeTemporalRecordRepositoryMixin:
    """Temporal records operations for the public knowledge repository."""

    @staticmethod
    def _collect_hard_delete_references(
        connection: sqlite3.Connection,
        *,
        space: str,
        work_id: str | None,
        item_id: str | None = None,
        target_item_ids: Iterable[str] | None = None,
        ignored_dependent_item_ids: Iterable[str] | None = None,
        version_ids: set[str],
        limit: int = 20,
    ) -> tuple[int, list[dict[str, Any]]]:
        queries: list[tuple[str, tuple[Any, ...]]] = []
        target_ids = sorted(set(target_item_ids or ([item_id] if item_id else [])))
        if not target_ids:
            return 0, []
        target_json = json.dumps(target_ids, ensure_ascii=False, separators=(",", ":"))
        ignored_json = json.dumps(
            sorted(set(ignored_dependent_item_ids or [])),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        version_json = json.dumps(sorted(version_ids), ensure_ascii=False, separators=(",", ":"))

        def add_version_reference_query(
            *, sql_path: str, field: str, params: tuple[Any, ...], writing_only: bool,
        ) -> None:
            scope = "AND dependent.space = 'writing'" if writing_only else ""
            ctes = """WITH delete_targets(id) AS (
                    SELECT CAST(value AS TEXT) FROM json_each(?)
                ), ignored_dependents(id) AS (
                    SELECT CAST(value AS TEXT) FROM json_each(?)
                )"""
            query_params: list[Any] = [target_json, ignored_json]
            if "delete_versions" in sql_path:
                ctes = """WITH delete_targets(id) AS (
                        SELECT CAST(value AS TEXT) FROM json_each(?)
                    ), ignored_dependents(id) AS (
                        SELECT CAST(value AS TEXT) FROM json_each(?)
                    ), delete_versions(id) AS (
                        SELECT CAST(value AS TEXT) FROM json_each(?)
                    )"""
                query_params.append(version_json)
            query_params.extend(params)
            queries.append((
                f"""
                {ctes}
                SELECT dependent.id AS item_id, dependent.kind AS item_kind,
                       version.id AS version_id, version.status AS version_status,
                       '{field}' AS reference_field, {sql_path} AS reference_target_id
                FROM knowledge_item AS dependent
                JOIN knowledge_version AS version ON version.item_id = dependent.id
                WHERE dependent.id NOT IN (SELECT id FROM delete_targets)
                  AND dependent.id NOT IN (SELECT id FROM ignored_dependents) {scope}
                  AND {sql_path}
                """,
                tuple(query_params),
            ))

        if space == "writing" and work_id is not None:
            for field in (
                "identity_item_id", "entity_item_id", "source_entity_id", "target_entity_id",
            ):
                add_version_reference_query(
                    sql_path="json_extract(version.content_json, '$.%s') IN (SELECT id FROM delete_targets)" % field,
                    field=field,
                    params=(),
                    writing_only=True,
                )
            add_version_reference_query(
                sql_path="json_extract(version.content_json, '$.about_record_id') IN (SELECT id FROM delete_targets)",
                field="about_record_id",
                params=(),
                writing_only=True,
            )
            add_version_reference_query(
                sql_path="json_extract(version.content_json, '$.source_candidate_item_id') IN (SELECT id FROM delete_targets)",
                field="source_candidate_item_id",
                params=(),
                writing_only=True,
            )
            add_version_reference_query(
                sql_path="json_extract(version.content_json, '$.source_candidate_version_id') IN (SELECT id FROM delete_versions)",
                field="source_candidate_version_id",
                params=(),
                writing_only=True,
            )

        linked_scope = "dependent.space = 'writing'"
        linked_item_sql = f"""
            WITH delete_targets(id) AS (
                SELECT CAST(value AS TEXT) FROM json_each(?)
            ), ignored_dependents(id) AS (
                SELECT CAST(value AS TEXT) FROM json_each(?)
            )
            SELECT dependent.id AS item_id, dependent.kind AS item_kind,
                   version.id AS version_id, version.status AS version_status,
                   'linked_learning_candidates.candidate_item_id' AS reference_field,
                   json_extract(linked.value, '$.candidate_item_id') AS reference_target_id
            FROM knowledge_item AS dependent
            JOIN knowledge_version AS version ON version.item_id = dependent.id
            JOIN json_each(version.content_json, '$.linked_learning_candidates') AS linked
            WHERE dependent.id NOT IN (SELECT id FROM delete_targets)
              AND dependent.id NOT IN (SELECT id FROM ignored_dependents) AND {linked_scope}
              AND json_extract(linked.value, '$.candidate_item_id') IN (SELECT id FROM delete_targets)
        """
        queries.append((linked_item_sql, (target_json, ignored_json)))
        linked_version_sql = f"""
            WITH delete_targets(id) AS (
                SELECT CAST(value AS TEXT) FROM json_each(?)
            ), ignored_dependents(id) AS (
                SELECT CAST(value AS TEXT) FROM json_each(?)
            ), delete_versions(id) AS (
                SELECT CAST(value AS TEXT) FROM json_each(?)
            )
            SELECT dependent.id AS item_id, dependent.kind AS item_kind,
                   version.id AS version_id, version.status AS version_status,
                   'linked_learning_candidates.candidate_version_id' AS reference_field,
                   json_extract(linked.value, '$.candidate_version_id') AS reference_target_id
            FROM knowledge_item AS dependent
            JOIN knowledge_version AS version ON version.item_id = dependent.id
            JOIN json_each(version.content_json, '$.linked_learning_candidates') AS linked
            WHERE dependent.id NOT IN (SELECT id FROM delete_targets)
              AND dependent.id NOT IN (SELECT id FROM ignored_dependents) AND {linked_scope}
              AND json_extract(linked.value, '$.candidate_version_id') IN (SELECT id FROM delete_versions)
        """
        queries.append((linked_version_sql, (target_json, ignored_json, version_json)))

        explicit_relation_sql = """
            WITH delete_targets(id) AS (
                SELECT CAST(value AS TEXT) FROM json_each(?)
            ), ignored_dependents(id) AS (
                SELECT CAST(value AS TEXT) FROM json_each(?)
            ), relation_references AS (
                SELECT relation.target_item_id AS item_id,
                       relation.source_item_id AS reference_target_id,
                       'knowledge_relation.source_item_id' AS reference_field
                FROM knowledge_relation AS relation
                WHERE relation.source_item_id IN (SELECT id FROM delete_targets)
                UNION ALL
                SELECT relation.source_item_id AS item_id,
                       relation.target_item_id AS reference_target_id,
                       'knowledge_relation.target_item_id' AS reference_field
                FROM knowledge_relation AS relation
                WHERE relation.target_item_id IN (SELECT id FROM delete_targets)
            )
            SELECT dependent.id AS item_id, dependent.kind AS item_kind,
                   version.id AS version_id, version.status AS version_status,
                   reference.reference_field, reference.reference_target_id
            FROM relation_references AS reference
            JOIN knowledge_item AS dependent ON dependent.id = reference.item_id
            LEFT JOIN knowledge_version AS version
              ON version.id = (
                  SELECT latest.id FROM knowledge_version AS latest
                  WHERE latest.item_id = dependent.id
                  ORDER BY latest.version DESC LIMIT 1
              )
            WHERE dependent.id NOT IN (SELECT id FROM delete_targets)
              AND dependent.id NOT IN (SELECT id FROM ignored_dependents)
        """
        queries.append((explicit_relation_sql, (target_json, ignored_json)))

        dependency_reference_sql = """
            WITH delete_targets(id) AS (
                SELECT CAST(value AS TEXT) FROM json_each(?)
            ), ignored_dependents(id) AS (
                SELECT CAST(value AS TEXT) FROM json_each(?)
            ), delete_versions(id) AS (
                SELECT CAST(value AS TEXT) FROM json_each(?)
            )
            SELECT dependency.consumer_id AS item_id,
                   dependency.consumer_kind AS item_kind,
                   dependency.consumer_revision AS version_id,
                   dependency.status AS version_status,
                   'knowledge_projection_dependency:' || dependency.purpose AS reference_field,
                   COALESCE(dependency.knowledge_version_id, dependency.knowledge_item_id)
                       AS reference_target_id
            FROM knowledge_projection_dependency AS dependency
            WHERE dependency.status = 'current'
              AND dependency.consumer_kind != 'assistant_turn'
              AND (
                  dependency.knowledge_item_id IN (SELECT id FROM delete_targets)
                  OR dependency.knowledge_version_id IN (SELECT id FROM delete_versions)
              )
              AND NOT (
                  dependency.consumer_kind = 'knowledge_item'
                  AND dependency.consumer_id IN (SELECT id FROM ignored_dependents)
              )
        """
        queries.append((dependency_reference_sql, (target_json, ignored_json, version_json)))

        graph_reference_sql = """
            WITH delete_targets(id) AS (
                SELECT lower(CAST(value AS TEXT)) FROM json_each(?)
            ), ignored_dependents(id) AS (
                SELECT CAST(value AS TEXT) FROM json_each(?)
            )
            SELECT dependent.id AS item_id, dependent.kind AS item_kind,
                   version.id AS version_id, version.status AS version_status,
                   'knowledge_graph_relation_reference.entity_ref:' || relation.relation_role
                       AS reference_field,
                   lower(relation.reference_id) AS reference_target_id
            FROM knowledge_graph_relation_reference AS relation
            JOIN knowledge_item AS dependent ON dependent.id = relation.candidate_item_id
            JOIN knowledge_version AS version ON version.id = relation.candidate_version_id
            WHERE relation.reference_kind = 'entity_ref'
              AND lower(relation.reference_id) IN (SELECT id FROM delete_targets)
              AND dependent.id NOT IN (SELECT id FROM delete_targets)
              AND dependent.id NOT IN (SELECT id FROM ignored_dependents)
        """
        queries.append((graph_reference_sql, (target_json, ignored_json)))

        references: list[dict[str, Any]] = []
        reference_count = 0
        for sql, params in queries:
            count = int(connection.execute(
                f"SELECT COUNT(*) FROM ({sql}) AS hard_delete_references", params
            ).fetchone()[0])
            reference_count += count
            if len(references) >= limit:
                continue
            rows = connection.execute(
                f"SELECT * FROM ({sql}) AS hard_delete_references LIMIT ?",
                (*params, limit - len(references)),
            ).fetchall()
            references.extend(dict(row) for row in rows)
        return reference_count, references

    def create_temporal_record(
        self,
        *,
        work_id: str,
        content: TemporalRecordContent,
        evidence_span_ids: Iterable[str],
    ) -> dict[str, Any]:
        """Persist one candidate record through the existing versioned item owner."""
        validated = TemporalRecordContent.model_validate(content)
        evidence_ids = tuple(dict.fromkeys(evidence_span_ids))
        submitted_evidence_ids = set(evidence_ids)
        for coordinate in (
            validated.story_time_coordinate,
            validated.knowledge_coordinate,
        ):
            if coordinate is not None and not set(coordinate.basis_evidence_ids).issubset(
                submitted_evidence_ids
            ):
                raise ValueError(
                    "verified temporal-coordinate evidence must be attached to this exact record version"
                )
        if validated.about_record_id is not None:
            related = self.get_knowledge_item(validated.about_record_id)
            if (
                related is None
                or related["space"] != "writing"
                or related["work_id"] != work_id
                or related["kind"] != "temporal_record"
            ):
                raise ValueError("about_record_id must reference a temporal record in this work")
            related_content = TemporalRecordContent.model_validate(related["content"])
            if related_content.branch_id != validated.branch_id:
                raise ValueError("about_record_id must reference a record in the same branch")
        return self.create_knowledge_item(
            space="writing",
            work_id=work_id,
            kind="temporal_record",
            status="draft",
            content=validated.model_dump(mode="json"),
            evidence_span_ids=evidence_ids,
        )

    def list_verified_temporal_coordinates(
        self,
        work_id: str,
        *,
        branch_id: str,
        domain: str,
        limit: int = 500,
    ) -> dict[str, Any]:
        """Read only reviewed, evidence-bound coordinates from existing temporal records."""
        if domain not in {"story_time", "reader_knowledge"}:
            raise ValueError("domain must be 'story_time' or 'reader_knowledge'")
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        page = self.list_temporal_records(
            work_id,
            branch_id=branch_id,
            limit=limit,
            include_rejected=False,
        )
        from .story_coordinates import StoryCoordinates

        coordinates: list[dict[str, Any]] = []
        excluded_unreviewed = 0
        excluded_unresolved_anchor_count = 0
        for record in page["records"]:
            if record["status"] != "reviewed":
                excluded_unreviewed += 1
                continue
            readiness = StoryCoordinates.coordinate_readiness(record, domain=domain)
            if readiness.status != "verified":
                continue
            anchors = list(readiness.coordinate.anchor_record_ids)
            if anchors:
                placeholders = ", ".join("?" for _ in anchors)
                with self.database.connection() as connection:
                    anchor_rows = connection.execute(
                        f"""
                        SELECT item.id
                        FROM knowledge_item AS item
                        JOIN knowledge_version AS version ON version.item_id = item.id
                          AND version.version = (
                              SELECT MAX(latest.version) FROM knowledge_version AS latest
                              WHERE latest.item_id = item.id
                          )
                        WHERE item.id IN ({placeholders})
                          AND item.space = 'writing' AND item.work_id = ?
                          AND item.kind = 'temporal_record' AND item.status = 'reviewed'
                          AND COALESCE(json_extract(version.content_json, '$.branch_id'), 'main') = ?
                        """,
                        (*anchors, work_id, branch_id),
                    ).fetchall()
                if {str(row["id"]) for row in anchor_rows} != set(anchors):
                    excluded_unresolved_anchor_count += 1
                    continue
            basis = set(readiness.evidence_ids)
            coordinates.append(
                {
                    "record_id": record["id"],
                    "version_id": record["version_id"],
                    "branch_id": record["content"]["branch_id"],
                    "domain": domain,
                    "coordinate": readiness.coordinate.model_dump(mode="json"),
                    "evidence": [
                        evidence for evidence in record["evidence"]
                        if evidence["id"] in basis
                    ],
                }
            )
        return {
            "work_id": work_id,
            "branch_id": branch_id,
            "domain": domain,
            "coordinates": coordinates,
            "stored_record_count": page["stored_record_count"],
            "has_more": page["has_more"],
            "excluded_unreviewed_count": excluded_unreviewed,
            "excluded_unresolved_anchor_count": excluded_unresolved_anchor_count,
            "coverage_status": "partial" if page["has_more"] else "complete",
        }

    def list_temporal_records(
        self,
        work_id: str,
        *,
        branch_id: str,
        limit: int,
        include_rejected: bool,
        register_source_pins: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Read one bounded branch page, then its exact source spans.

        Without register source pins, counts cover current, non-deleted item
        versions in the requested branch. ``stored_record_count`` includes rejected
        records only when requested; ``rejected_record_count`` counts every rejected
        record in that branch.
        ``has_more`` is determined from the extra item row and does not read that
        sentinel row's evidence. When register_source_pins is supplied, only
        reviewed writing.register observations with exact adopted source evidence
        and complete declared lineage inside those current source pins
        enter the page and its counts; unrelated records cannot consume its limit.
        """
        validate_scope("writing", work_id)
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        if not isinstance(branch_id, str) or not branch_id.strip():
            raise ValueError("branch_id must not be blank")
        if not isinstance(include_rejected, bool):
            raise ValueError("include_rejected must be a boolean")

        branch_sql = "COALESCE(json_extract(v.content_json, '$.branch_id'), 'main') = ?"
        status_sql = "" if include_rejected else "AND i.status != 'rejected'"
        register_sql = ""
        register_args: tuple[Any, ...] = ()
        if register_source_pins is not None:
            register_sql = """
                AND i.status = 'reviewed'
                AND json_extract(v.content_json, '$.predicate') = 'writing.register'
                AND EXISTS (
                    SELECT 1
                    FROM knowledge_version_evidence AS register_ve
                    JOIN evidence_span AS register_e ON register_e.id = register_ve.evidence_span_id
                    JOIN source_snapshot AS register_ss ON register_ss.id = register_e.snapshot_id
                    JOIN json_each(?) AS register_pin
                      ON register_ss.source_id = json_extract(register_pin.value, '$.source_id')
                     AND register_ss.id = json_extract(register_pin.value, '$.snapshot_id')
                     AND register_ss.content_hash = json_extract(register_pin.value, '$.content_hash')
                    WHERE register_ve.knowledge_version_id = v.id
                      AND json_extract(v.content_json, '$.value.source_id') = register_ss.source_id
                      AND json_extract(v.content_json, '$.value.snapshot_id') = register_ss.id
                      AND json_extract(v.content_json, '$.value.content_hash') = register_ss.content_hash
                      AND register_e.start_offset <= json_extract(v.content_json, '$.value.start')
                      AND register_e.end_offset >= json_extract(v.content_json, '$.value.end')
                      AND substr(register_ss.content,
                                 json_extract(v.content_json, '$.value.start') + 1,
                                 json_extract(v.content_json, '$.value.end') -
                                 json_extract(v.content_json, '$.value.start')) =
                          json_extract(v.content_json, '$.value.quote')
                )
            """
            register_args = (json.dumps(register_source_pins, ensure_ascii=False),)
        with self.database.connection() as connection:
            self._ensure_project_exists(connection, "writing", work_id)
            if register_source_pins is not None:
                dependency_args = []
                dependency_gate = all_version_source_dependencies_filter(
                    "v.id", current_register_pin_source_ids(connection, register_source_pins), dependency_args)
                register_sql += f" AND {dependency_gate}"
                register_args = (*register_args, *dependency_args)
            counts = connection.execute(
                f"""
                SELECT COUNT(*) AS record_count,
                       COALESCE(SUM(CASE WHEN i.status = 'rejected' THEN 1 ELSE 0 END), 0)
                           AS rejected_count
                FROM knowledge_item AS i
                JOIN knowledge_version AS v ON v.item_id = i.id
                  AND v.version = (SELECT MAX(latest.version)
                                   FROM knowledge_version AS latest
                                   WHERE latest.item_id = i.id)
                WHERE i.space = 'writing' AND i.work_id = ?
                  AND i.kind = 'temporal_record' AND i.status != 'deleted'
                  AND {branch_sql}
                  {register_sql}
                """,
                (work_id, branch_id, *register_args),
            ).fetchone()
            all_count = int(counts["record_count"])
            rejected_count = int(counts["rejected_count"])
            stored_count = all_count if include_rejected else all_count - rejected_count

            page_rows = connection.execute(
                f"""
                SELECT i.id, i.space, i.work_id, i.status, i.created_at, i.updated_at,
                       v.id AS version_id, v.version, v.content_json
                FROM knowledge_item AS i
                JOIN knowledge_version AS v ON v.item_id = i.id
                  AND v.version = (SELECT MAX(latest.version)
                                   FROM knowledge_version AS latest
                                   WHERE latest.item_id = i.id)
                WHERE i.space = 'writing' AND i.work_id = ?
                  AND i.kind = 'temporal_record' AND i.status != 'deleted'
                  AND COALESCE(json_extract(v.content_json, '$.branch_id'), 'main') = ?
                  {status_sql}
                  {register_sql}
                ORDER BY i.created_at, i.id
                LIMIT ?
                """,
                (work_id, branch_id, *register_args, limit + 1),
            ).fetchall()

            has_more = len(page_rows) > limit
            selected_rows = page_rows[:limit]
            evidence_by_version: dict[str, list[dict[str, Any]]] = {
                str(row["version_id"]): [] for row in selected_rows
            }
            if evidence_by_version:
                version_ids = list(evidence_by_version)
                placeholders = ", ".join("?" for _ in version_ids)
                evidence_rows = connection.execute(
                    f"""
                    SELECT ve.knowledge_version_id AS version_id,
                           e.id AS evidence_span_id, e.snapshot_id,
                           e.start_offset, e.end_offset, e.quote_hash,
                           ss.content_hash AS source_version_hash,
                           s.id AS source_id, s.title AS source_title,
                           substr(ss.content, e.start_offset + 1,
                                  e.end_offset - e.start_offset) AS quote
                    FROM knowledge_version_evidence AS ve
                    JOIN evidence_span AS e ON e.id = ve.evidence_span_id
                    LEFT JOIN source_snapshot AS ss ON ss.id = e.snapshot_id
                    LEFT JOIN source AS s ON s.id = ss.source_id
                    WHERE ve.knowledge_version_id IN ({placeholders})
                    ORDER BY ve.knowledge_version_id, e.start_offset, e.id
                    """,
                    tuple(version_ids),
                ).fetchall()
                for evidence_row in evidence_rows:
                    evidence_by_version[str(evidence_row["version_id"])].append(
                        dict(evidence_row)
                    )

        by_id: dict[str, dict[str, Any]] = {}
        for row in selected_rows:
            item_id = str(row["id"])
            version_id = str(row["version_id"])
            item = {
                key: row[key]
                for key in (
                    "id", "space", "work_id", "status", "created_at", "updated_at",
                    "version_id", "version",
                )
            }
            item["content"] = json.loads(row["content_json"])
            item["evidence"] = []
            item["evidence_span_ids"] = []
            for evidence in evidence_by_version[version_id]:
                evidence_id = str(evidence["evidence_span_id"])
                item["evidence_span_ids"].append(evidence_id)
                item["evidence"].append(
                    {
                        "id": evidence_id,
                        "snapshot_id": evidence["snapshot_id"],
                        "source_version_hash": evidence["source_version_hash"],
                        "source_id": evidence["source_id"],
                        "source_title": evidence["source_title"],
                        "start_offset": evidence["start_offset"],
                        "end_offset": evidence["end_offset"],
                        "quote_hash": evidence["quote_hash"],
                        "quote": evidence["quote"],
                    }
                )
            by_id[item_id] = item
        return {
            "records": list(by_id.values()),
            "stored_record_count": stored_count,
            "rejected_record_count": rejected_count,
            "has_more": has_more,
        }

    def list_register_observations_for_evidence(
        self, *, work_id: str, branch_id: str, evidence_refs: list[dict[str, Any]],
        allowed_source_ids: list[str], limit: int = 80,
    ) -> dict[str, Any]:
        """Associate reviewed observations with exact retrieved spans, before LIMIT.

        This is a partial candidate read, not a full state register or semantic
        support decision. All declared dependencies must fit the caller's source
        boundary; nearby observations do not inherit a hit's support judgment.
        """
        if not evidence_refs or not allowed_source_ids:
            return {"records": [], "has_more": False}
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("register candidate limit must be between 1 and 500")
        dependency_args = []
        dependency_gate = all_version_source_dependencies_filter("v.id", allowed_source_ids, dependency_args)
        with self.database.connection() as connection:
            rows = connection.execute(f"""
                SELECT DISTINCT i.id, v.id AS version_id
                FROM json_each(?) AS hit
                JOIN source_snapshot AS ss
                  ON ss.source_id = json_extract(hit.value, '$.source_id')
                 AND ss.id = json_extract(hit.value, '$.snapshot_id')
                 AND ss.content_hash = json_extract(hit.value, '$.content_hash')
                JOIN source_current_snapshot AS current
                  ON current.source_id = ss.source_id AND current.snapshot_id = ss.id
                JOIN source AS s ON s.id = ss.source_id
                JOIN evidence_span AS e ON e.snapshot_id = ss.id
                JOIN knowledge_version_evidence AS ve ON ve.evidence_span_id = e.id
                JOIN knowledge_version AS v ON v.id = ve.knowledge_version_id
                JOIN knowledge_item AS i ON i.id = v.item_id
                WHERE i.space = 'writing' AND i.work_id = ?
                  AND s.space = i.space AND s.work_id IS i.work_id AND s.deleted_at IS NULL
                  AND i.kind = 'temporal_record' AND i.status = 'reviewed'
                  AND v.version = (SELECT MAX(latest.version) FROM knowledge_version AS latest WHERE latest.item_id = i.id)
                  AND json_extract(v.content_json, '$.branch_id') = ?
                  AND json_extract(v.content_json, '$.predicate') = 'writing.register'
                  AND json_extract(v.content_json, '$.value.source_id') = ss.source_id
                  AND json_extract(v.content_json, '$.value.snapshot_id') = ss.id
                  AND json_extract(v.content_json, '$.value.content_hash') = ss.content_hash
                  AND json_extract(v.content_json, '$.value.start') < json_extract(hit.value, '$.end_offset')
                  AND json_extract(v.content_json, '$.value.end') > json_extract(hit.value, '$.start_offset')
                  AND substr(ss.content, json_extract(hit.value, '$.start_offset') + 1,
                             json_extract(hit.value, '$.end_offset') - json_extract(hit.value, '$.start_offset'))
                        = json_extract(hit.value, '$.quote')
                  AND substr(ss.content, json_extract(v.content_json, '$.value.start') + 1,
                             json_extract(v.content_json, '$.value.end') - json_extract(v.content_json, '$.value.start'))
                        = json_extract(v.content_json, '$.value.quote')
                  AND {dependency_gate}
                ORDER BY i.id LIMIT ?
            """, (json.dumps(evidence_refs, ensure_ascii=False), work_id, branch_id,
                  *dependency_args, limit + 1)).fetchall()
            records = []
            for row in rows[:limit]:
                record = self.get_knowledge_item(row["id"])
                if record and record["version_id"] == row["version_id"] and record["status"] == "reviewed":
                    records.append(record)
        return {"records": records, "has_more": len(rows) > limit}

    def delete_knowledge_item(self, item_id: str, *, expected_version: str) -> bool:
        with self.database.transaction() as connection:
            item = connection.execute(
                "SELECT id, space, work_id FROM knowledge_item WHERE id = ?",
                (item_id,),
            ).fetchone()
            if item is None:
                return False
            latest_version = connection.execute(
                "SELECT id FROM knowledge_version WHERE item_id = ? ORDER BY version DESC LIMIT 1",
                (item_id,),
            ).fetchone()
            current_version_id = str(latest_version["id"]) if latest_version is not None else None
            if current_version_id != expected_version:
                raise KnowledgeItemVersionConflictError(
                    f"knowledge item version changed (expected {expected_version}, current {current_version_id})"
                )
            version_ids = {
                str(row["id"])
                for row in connection.execute(
                    "SELECT id FROM knowledge_version WHERE item_id = ?",
                    (item_id,),
                )
            }
            identifiers = {item_id, *version_ids}
            active_assistant = connection.execute(
                """
                SELECT 1
                FROM assistant_run AS run
                JOIN assistant_conversation AS conversation ON conversation.id = run.conversation_id
                WHERE conversation.space = ? AND conversation.work_id IS ?
                  AND run.status IN ('accepted', 'running')
                LIMIT 1
                """,
                (str(item["space"]), item["work_id"]),
            ).fetchone()
            if active_assistant is not None:
                raise ValueError("当前作品或学习范围有 Agent 正在运行，请完成后再永久删除。")
            related_runs = _workflow_run_ids_to_purge(
                connection,
                source_identifiers=identifiers,
            )
            if any(str(row["status"]) in {"queued", "running"} for row in related_runs):
                raise ValueError("仍有工作流正在使用该知识项，请完成后再永久删除。")
            reference_count, references = self._collect_hard_delete_references(
                connection,
                space=str(item["space"]),
                work_id=item["work_id"],
                item_id=item_id,
                version_ids=version_ids,
            )
            if reference_count:
                raise KnowledgeItemReferencedError(
                    reference_count=reference_count,
                    references=references,
                )
            with _allow_authorized_purge(connection):
                self._purge_knowledge_items_in_connection(connection, [item_id])
                run_ids = [str(row["run_id"]) for row in related_runs]
                _delete_by_values(connection, "workflow_node_event", "run_id", run_ids)
                _delete_by_values(connection, "workflow_run", "run_id", run_ids)
            return True


