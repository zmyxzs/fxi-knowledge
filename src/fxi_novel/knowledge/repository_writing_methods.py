"""Bounded cross-work search for already qualified personal writing methods."""

import json
from typing import Any

from .repository_support import fts_query, validate_scope
from .scope_selection import source_selection_filter
from .tokenizer import tokenize_chinese
from .planning_study_basis import current_planning_study_basis_filter
from .style_study_basis import style_study_bases


_PERSONAL_METHOD_SEARCH_CARD_LIMIT = 24
_PERSONAL_METHOD_SEARCH_METHOD_LIMIT = 24


class KnowledgeWritingMethodSearchRepositoryMixin:
    """Search method-card text without widening fact or source retrieval."""

    def style_study_basis_status(self, item_id: str, expected_version: int) -> str:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT i.id, i.space, i.work_id, i.status, v.version, "
                "json_object('derived_from_studies', json_extract(v.content_json, '$.derived_from_studies')) AS content_json "
                "FROM knowledge_item AS i JOIN knowledge_version AS v ON v.item_id = i.id "
                "WHERE i.id = ? ORDER BY v.version DESC LIMIT 1", (item_id,),
            ).fetchone()
            if row is None or row["version"] != expected_version or row["status"] not in {"draft", "reviewed"}:
                return "changed"
            return style_study_bases(connection, [{**dict(row), "content": json.loads(row["content_json"])}])[item_id]

    def planning_method_study_basis_is_current(self, item_id: str, expected_version: int) -> bool:
        """Check the same current interpretation contract used by method search."""
        with self.database.connection() as connection:
            row = connection.execute(
                """SELECT 1 FROM knowledge_item AS i
                   JOIN knowledge_version AS v ON v.item_id = i.id
                   WHERE i.id = ? AND v.version = ?
                     AND v.version = (SELECT MAX(latest.version) FROM knowledge_version AS latest
                                      WHERE latest.item_id = i.id)
                     AND i.status IN ('draft', 'reviewed')
                     AND json_extract(v.content_json, '$.learning_role') = 'planning_method_synthesis'
                     AND """ + current_planning_study_basis_filter("i", "v"),
                (item_id, expected_version),
            ).fetchone()
        return row is not None

    def list_current_planning_studies(
        self, space: str, work_id: str | None, *,
        source_ids: list[str] | None = None, folder_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Locate planning studies by registered, current sources, not search words."""
        validate_scope(space, work_id)
        parameters: list[Any] = [space, work_id]
        source_filter = source_selection_filter("s", source_ids, folder_ids, parameters)
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT i.id AS item_id, i.updated_at, v.version,
                       json_extract(v.content_json, '$.source_refs') AS source_refs_json
                FROM knowledge_item AS i
                JOIN knowledge_version AS v ON v.item_id = i.id
                  AND v.version = (SELECT MAX(latest.version) FROM knowledge_version AS latest
                                   WHERE latest.item_id = i.id)
                WHERE i.space = ? AND i.work_id IS ?
                  AND i.kind = 'source_study' AND i.status IN ('draft', 'reviewed')
                  AND json_extract(v.content_json, '$.analysis_type') = 'planning'
                  AND json_extract(v.content_json, '$.learning_role') = 'planning_method_synthesis'
                  AND json_array_length(v.content_json, '$.method_payload.methods') > 0
                  AND json_array_length(v.content_json, '$.source_refs') > 0
                  AND NOT EXISTS (
                    SELECT 1 FROM json_each(v.content_json, '$.source_refs') AS ref
                    WHERE NOT EXISTS (
                      SELECT 1 FROM source AS s
                      JOIN source_current_snapshot AS current ON current.source_id = s.id
                      JOIN source_snapshot AS ss ON ss.id = current.snapshot_id
                      WHERE s.id = json_extract(ref.value, '$.source_id')
                        AND ss.id = json_extract(ref.value, '$.snapshot_id')
                        AND ss.content_hash = json_extract(ref.value, '$.content_hash')
                        AND s.deleted_at IS NULL AND s.space = i.space AND s.work_id IS i.work_id
                """ + source_filter + """
                    )
                  )
                  AND """ + current_planning_study_basis_filter("i", "v") + """
                ORDER BY i.updated_at DESC, v.version DESC, i.id
                """, parameters,
            ).fetchall()
        return [{"item_id": row["item_id"], "version": row["version"],
                 "updated_at": row["updated_at"], "source_refs": json.loads(row["source_refs_json"])}
                for row in rows]

    def list_current_style_profiles(
        self, space: str, work_id: str | None, *,
        source_ids: list[str] | None = None, folder_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Locate current study identities without FTS ranking or reading examples."""
        validate_scope(space, work_id)
        parameters: list[Any] = [space, work_id]
        source_filter = source_selection_filter("s", source_ids, folder_ids, parameters)
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT i.id AS item_id, i.space, i.work_id, i.updated_at, v.version,
                       json_object(
                         'derived_from_studies', json_extract(v.content_json, '$.derived_from_studies'),
                         'source_refs', json_extract(v.content_json, '$.source_refs'),
                         'style_example_profile', json_extract(v.content_json, '$.style_example_profile')) AS content_json,
                       json_extract(v.content_json, '$.craft_method_payload.profile') AS profile_json,
                       json_extract(v.content_json, '$.source_refs') AS source_refs_json,
                       json_type(v.content_json, '$.author_style_fingerprint') = 'object' AS author_profile
                FROM knowledge_item AS i
                JOIN knowledge_version AS v ON v.item_id = i.id
                  AND v.version = (SELECT MAX(latest.version) FROM knowledge_version AS latest
                                   WHERE latest.item_id = i.id)
                WHERE i.space = ? AND i.work_id IS ?
                  AND i.kind = 'source_study' AND i.status IN ('draft', 'reviewed')
                  AND json_extract(v.content_json, '$.analysis_type') = 'style'
                  AND json_extract(v.content_json, '$.learning_role') IN ('craft_method_synthesis', 'craft_observations')
                  AND (json_array_length(v.content_json, '$.craft_method_payload.methods') > 0
                    OR json_array_length(v.content_json, '$.author_style_fingerprint.golden_voice_slices') > 0)
                  AND json_array_length(v.content_json, '$.source_refs') > 0
                  AND NOT EXISTS (
                    SELECT 1
                    FROM json_each(COALESCE(
                      NULLIF(json_extract(v.content_json, '$.craft_method_payload.profile.source_ids'), '[]'),
                      NULLIF(json_extract(v.content_json, '$.style_example_profile.source_ids'), '[]'),
                      (SELECT json_group_array(json_extract(ref.value, '$.source_id'))
                       FROM json_each(v.content_json, '$.source_refs') AS ref))) AS source_id
                    WHERE NOT EXISTS (
                      SELECT 1 FROM source AS s
                      JOIN source_current_snapshot AS current ON current.source_id = s.id
                      JOIN source_snapshot AS ss ON ss.id = current.snapshot_id
                      JOIN json_each(v.content_json, '$.source_refs') AS ref
                        ON json_extract(ref.value, '$.source_id') = s.id
                       AND json_extract(ref.value, '$.snapshot_id') = ss.id
                       AND json_extract(ref.value, '$.content_hash') = ss.content_hash
                      WHERE s.id = source_id.value AND s.deleted_at IS NULL
                        AND s.space = i.space AND s.work_id IS i.work_id
                """ + source_filter + """
                    )
                  )
                ORDER BY i.updated_at DESC, v.version DESC, i.id
                """, parameters,
            ).fetchall()
            items = [{"id": row["item_id"], "space": row["space"], "work_id": row["work_id"],
                      "content": json.loads(row["content_json"])} for row in rows]
            bases = style_study_bases(connection, items)
        from .style_examples import example_profile
        return [{
            "item_id": row["item_id"], "version": row["version"],
            "updated_at": row["updated_at"],
            "profile": json.loads(row["profile_json"]) if row["profile_json"] else example_profile({
                "id": row["item_id"], "version": row["version"], "content": item["content"]}),
            "profile_kind": "methods" if row["profile_json"] and bases[row["item_id"]] != "changed" else "examples",
            "study_basis_status": bases[row["item_id"]],
            "source_refs": json.loads(row["source_refs_json"]),
            "author_profile": bool(row["author_profile"]),
        } for row, item in zip(rows, items, strict=True)]

    def search_personal_writing_methods(
        self, query: str, *, candidate_limit: int = _PERSONAL_METHOD_SEARCH_METHOD_LIMIT,
    ) -> list[dict[str, Any]]:
        normalized = query.strip()
        if not normalized or len(normalized) > 500:
            raise ValueError("personal writing-method query must contain 1–500 characters")
        if not 1 <= candidate_limit <= _PERSONAL_METHOD_SEARCH_METHOD_LIMIT:
            raise ValueError("personal writing-method search pool is limited to 24 methods")
        expression = fts_query(normalized)
        if not expression:
            return []
        self.ensure_knowledge_item_index()
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT i.id AS item_id, i.space, i.work_id, i.status,
                       v.id AS version_id, v.version,
                       v.content_json,
                       i.updated_at,
                       bm25(knowledge_item_fts) AS lexical_rank
                FROM knowledge_item_fts
                JOIN knowledge_item AS i ON i.id = knowledge_item_fts.item_id
                JOIN knowledge_version AS v ON v.id = knowledge_item_fts.version_id
                  AND v.item_id = i.id
                  AND v.version = (
                      SELECT MAX(latest.version)
                      FROM knowledge_version AS latest
                      WHERE latest.item_id = i.id
                  )
                WHERE knowledge_item_fts MATCH ?
                  AND i.kind = 'source_study'
                  AND i.status IN ('draft', 'reviewed')
                  AND ((i.space = 'learning' AND i.work_id IS NULL)
                    OR (i.space = 'writing' AND i.work_id IS NOT NULL))
                  AND json_extract(v.content_json, '$.analysis_type') = 'style'
                  AND json_extract(v.content_json, '$.learning_role') = 'craft_method_synthesis'
                  AND EXISTS (
                    SELECT 1 FROM json_each(v.content_json, '$.craft_method_payload.methods') AS method
                    WHERE json_extract(method.value, '$.reuse_qualification.status') = 'qualified'
                      AND json_extract(method.value, '$.reuse_qualification.reuse_scope') = 'personal'
                      AND json_extract(method.value, '$.reuse_qualification.induction.decision') = 'pass'
                      AND json_extract(method.value, '$.reuse_qualification.transfer.decision') = 'pass'
                      AND json_extract(method.value, '$.scope_assessment') != 'source_bound'
                  )
                ORDER BY lexical_rank ASC, i.updated_at DESC, i.id
                LIMIT ?
                """,
                (expression, _PERSONAL_METHOD_SEARCH_CARD_LIMIT),
            ).fetchall()
        query_terms = set(tokenize_chinese(normalized))
        method_results: list[dict[str, Any]] = []
        for row in rows:
            content = json.loads(str(row["content_json"]))
            method_payload = content.get("craft_method_payload")
            methods = method_payload.get("methods", []) if isinstance(method_payload, dict) else []
            for method in methods:
                if not isinstance(method, dict):
                    continue
                qualification = method.get("reuse_qualification")
                if not isinstance(qualification, dict) or (
                    qualification.get("status") != "qualified"
                    or qualification.get("reuse_scope") != "personal"
                    or method.get("scope_assessment") == "source_bound"
                ):
                    continue
                review = qualification.get("induction")
                transfer = qualification.get("transfer")
                if (
                    not isinstance(review, dict) or review.get("decision") != "pass"
                    or not isinstance(transfer, dict) or transfer.get("decision") != "pass"
                ):
                    continue
                method_text = " ".join(
                    str(method.get(field, ""))
                    for field in (
                        "title", "aspect", "observation", "technique", "use_when",
                        "avoid_when", "effect", "preserve", "observed_contexts",
                        "limitations", "source_specific_elements",
                    )
                )
                method_terms = set(tokenize_chinese(method_text))
                matched_terms = query_terms & method_terms
                # FTS card matching only narrows the bounded item set. Rank each
                # method from its own indexed-style text so omnibus cards do not
                # return every sibling method at the same card-level score.
                if not matched_terms:
                    continue
                method_results.append(
                    {
                        "item_id": str(row["item_id"]),
                        "space": str(row["space"]),
                        "work_id": row["work_id"],
                        "status": str(row["status"]),
                        "version_id": str(row["version_id"]),
                        "version": int(row["version"]),
                        "method_id": str(method.get("method_id")),
                        "method": method,
                        "score": float(len(matched_terms)),
                        "matched_query_terms": sorted(matched_terms),
                        "card_lexical_rank": float(row["lexical_rank"]),
                        "updated_at": str(row["updated_at"]),
                    }
                )
        method_results.sort(
            key=lambda row: (
                -row["score"], row["card_lexical_rank"], row["updated_at"],
                row["item_id"], row["method_id"],
            )
        )
        return method_results[:candidate_limit]

    def get_personal_writing_method_records(
        self, method_ids: list[str],
    ) -> list[dict[str, Any]]:
        """Resolve exact method IDs; FTS is not an identity lookup."""
        normalized = list(dict.fromkeys(method_ids))
        if not normalized or len(normalized) > 3:
            raise ValueError("exact personal method reads are limited to 1–3 IDs")
        if any(not isinstance(value, str) or not value.strip() for value in normalized):
            raise ValueError("method IDs must be non-empty strings")
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT i.id AS item_id, i.space, i.work_id, i.status,
                       v.id AS version_id, v.version, v.content_json,
                       method.value AS method_json
                FROM knowledge_item AS i
                JOIN knowledge_version AS v ON v.item_id = i.id
                  AND v.version = (
                      SELECT MAX(latest.version)
                      FROM knowledge_version AS latest
                      WHERE latest.item_id = i.id
                  )
                JOIN json_each(v.content_json, '$.craft_method_payload.methods') AS method
                WHERE i.kind = 'source_study'
                  AND i.status IN ('draft', 'reviewed')
                  AND ((i.space = 'learning' AND i.work_id IS NULL)
                    OR (i.space = 'writing' AND i.work_id IS NOT NULL))
                  AND json_extract(v.content_json, '$.analysis_type') = 'style'
                  AND json_extract(v.content_json, '$.learning_role') = 'craft_method_synthesis'
                  AND json_extract(method.value, '$.method_id') IN (
                      SELECT value FROM json_each(?)
                  )
                ORDER BY i.id, json_extract(method.value, '$.method_id')
                """,
                (json.dumps(normalized, ensure_ascii=False),),
            ).fetchall()
        return [
            {
                "item_id": str(row["item_id"]),
                "space": str(row["space"]),
                "work_id": row["work_id"],
                "status": str(row["status"]),
                "version_id": str(row["version_id"]),
                "version": int(row["version"]),
                "content": json.loads(str(row["content_json"])),
                "method": json.loads(str(row["method_json"])),
            }
            for row in rows
        ]

    def search_unreviewed_personal_writing_method_items(
        self, query: str, *, top_k: int = 6,
    ) -> list[dict[str, Any]]:
        """Find only personal-learning synthesis cards needing their first review."""
        normalized = query.strip()
        if not normalized or len(normalized) > 500:
            raise ValueError("personal writing-method query must contain 1–500 characters")
        if not 1 <= top_k <= 6:
            raise ValueError("unreviewed personal method items are limited to 6 results")
        expression = fts_query(normalized)
        if not expression:
            return []
        self.ensure_knowledge_item_index()
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT i.id AS item_id, i.status, v.id AS version_id, v.version,
                       v.content_json, bm25(knowledge_item_fts) AS lexical_rank
                FROM knowledge_item_fts
                JOIN knowledge_item AS i ON i.id = knowledge_item_fts.item_id
                JOIN knowledge_version AS v ON v.id = knowledge_item_fts.version_id
                  AND v.item_id = i.id
                  AND v.version = (
                      SELECT MAX(latest.version)
                      FROM knowledge_version AS latest
                      WHERE latest.item_id = i.id
                  )
                WHERE knowledge_item_fts MATCH ?
                  AND i.kind = 'source_study'
                  AND i.space = 'learning' AND i.work_id IS NULL
                  AND i.status = 'draft'
                  AND json_extract(v.content_json, '$.analysis_type') = 'style'
                  AND json_extract(v.content_json, '$.learning_role') = 'craft_method_synthesis'
                  AND EXISTS (
                      SELECT 1 FROM json_each(v.content_json, '$.craft_method_payload.methods') AS method
                      WHERE json_extract(method.value, '$.scope_assessment') != 'source_bound'
                        AND (
                            json_type(method.value, '$.reuse_qualification') IS NULL
                            OR json_type(method.value, '$.reuse_qualification') = 'null'
                        )
                  )
                ORDER BY lexical_rank ASC, i.updated_at DESC, i.id
                LIMIT ?
                """,
                (expression, top_k),
            ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            content = json.loads(str(row["content_json"]))
            payload = content.get("craft_method_payload", {})
            methods = payload.get("methods", []) if isinstance(payload, dict) else []
            method_ids = [
                str(method.get("method_id"))
                for method in methods
                if isinstance(method, dict)
                and method.get("scope_assessment") != "source_bound"
                and not method.get("reuse_qualification")
            ]
            if not method_ids:
                continue
            results.append(
                {
                    "item_id": str(row["item_id"]),
                    "version_id": str(row["version_id"]),
                    "version": int(row["version"]),
                    "title": str(content.get("title", "文风研究")),
                    "method_ids": method_ids,
                    "score": -float(row["lexical_rank"]),
                }
            )
        return results
