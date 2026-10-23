"""Knowledge persistence methods for work references."""

from .repository_support import *
from contextlib import nullcontext
from .repository_conversation_effects import (
    assert_conversation_write_allowed,
    record_conversation_effect,
    reference_effect_state,
)


class KnowledgeWorkReferenceRepositoryMixin:
    """Work references operations for the public knowledge repository."""

    def _learning_reference_selection(
        self, connection: sqlite3.Connection, work_id: str, *, include_source_ids: bool = False,
    ) -> dict[str, Any]:
        exact_rows = connection.execute(
            """
            SELECT r.source_id
            FROM writing_learning_source_reference AS r
            JOIN source AS s ON s.id = r.source_id
            LEFT JOIN knowledge_project AS p
              ON p.id = s.work_id AND p.space = 'learning' AND p.status = 'active'
            WHERE r.work_id = ? AND s.space = 'learning' AND s.deleted_at IS NULL
              AND (s.work_id IS NULL OR p.id IS NOT NULL)
            ORDER BY s.title COLLATE NOCASE, s.id
            """,
            (work_id,),
        ).fetchall()
        scope_rows = connection.execute(
            """
            SELECT r.learning_work_id, r.folder_id,
                   COALESCE(p.title, '个人学习资料') AS library_title,
                   f.path AS folder_path
            FROM writing_learning_scope_reference AS r
            LEFT JOIN knowledge_project AS p
              ON p.id = r.learning_work_id
             AND p.space = 'learning' AND p.status = 'active'
            LEFT JOIN knowledge_folder AS f ON f.id = r.folder_id
            WHERE r.work_id = ?
              AND (r.learning_work_id IS NULL OR p.id IS NOT NULL)
            ORDER BY library_title COLLATE NOCASE, folder_path COLLATE NOCASE
            """,
            (work_id,),
        ).fetchall()
        # Personal sources retain their physical scope. Registered libraries can
        # also own a logical view of folders whose physical owner is personal;
        # resolve those with the same owner used by browsing and retrieval.
        effective_ids = {str(row["source_id"]) for row in exact_rows}
        personal_rows = connection.execute(
            """
            SELECT s.id
            FROM source AS s
            WHERE s.space = 'learning' AND s.work_id IS NULL
              AND s.deleted_at IS NULL
              AND EXISTS (
                      SELECT 1
                      FROM writing_learning_scope_reference AS scope
                      WHERE scope.work_id = ?
                        AND scope.learning_work_id IS NULL
                        AND (
                            scope.folder_id IS NULL
                            OR EXISTS (
                                SELECT 1
                                FROM knowledge_folder AS selected_folder
                                JOIN knowledge_folder AS source_folder
                                  ON source_folder.space = 'learning'
                                 AND source_folder.work_id IS selected_folder.work_id
                                WHERE selected_folder.id = scope.folder_id
                                  AND s.folder_id = source_folder.id
                                  AND (
                                      source_folder.path = selected_folder.path COLLATE NOCASE
                                      OR substr(
                                          source_folder.path, 1,
                                          length(selected_folder.path) + 1
                                      ) = selected_folder.path || '/' COLLATE NOCASE
                                  )
                            )
                        )
              )
            """,
            (work_id,),
        ).fetchall()
        effective_ids.update(str(row["id"]) for row in personal_rows)
        by_library: dict[str, list[str | None]] = {}
        for row in scope_rows:
            if row["learning_work_id"] is not None:
                by_library.setdefault(str(row["learning_work_id"]), []).append(row["folder_id"])
        for library_id, folder_ids in by_library.items():
            resolved = self.resolve_learning_work_scope_in_connection(
                connection, library_id,
                folder_ids=None if None in folder_ids else folder_ids,
            )
            effective_ids.update(resolved["source_ids"])
        return {
            "source_ids": [str(row["source_id"]) for row in exact_rows],
            "scopes": [dict(row) for row in scope_rows],
            "effective_source_count": len(effective_ids),
            **({"effective_source_ids": sorted(effective_ids)} if include_source_ids else {}),
        }

    def list_work_learning_references(self, work_id: str) -> dict[str, Any]:
        """List explicit sources and dynamic library/folder scopes for a writing work."""
        validate_scope("writing", work_id)
        with self.database.connection() as connection:
            self._ensure_project_exists(connection, "writing", work_id)
            return self._learning_reference_selection(connection, work_id)

    def list_work_learning_reference_filters(self, work_id: str) -> dict[str, Any]:
        """Read only the compact filters required by search; do not expand a library."""
        validate_scope("writing", work_id)
        with self.database.connection() as connection:
            self._ensure_project_exists(connection, "writing", work_id)
            exact_rows = connection.execute(
                """
                SELECT s.id AS source_id, s.work_id AS learning_work_id
                FROM writing_learning_source_reference AS r
                JOIN source AS s ON s.id = r.source_id
                LEFT JOIN knowledge_project AS p
                  ON p.id = s.work_id AND p.space = 'learning' AND p.status = 'active'
                WHERE r.work_id = ? AND s.space = 'learning' AND s.deleted_at IS NULL
                  AND (s.work_id IS NULL OR p.id IS NOT NULL)
                ORDER BY s.work_id, s.id
                """,
                (work_id,),
            ).fetchall()
            scope_rows = connection.execute(
                """
                SELECT r.learning_work_id, r.folder_id
                FROM writing_learning_scope_reference AS r
                LEFT JOIN knowledge_project AS p
                  ON p.id = r.learning_work_id
                 AND p.space = 'learning' AND p.status = 'active'
                WHERE r.work_id = ?
                  AND (r.learning_work_id IS NULL OR p.id IS NOT NULL)
                ORDER BY r.learning_work_id, r.folder_id
                """,
                (work_id,),
            ).fetchall()
        return {
            "sources": [dict(row) for row in exact_rows],
            "scopes": [dict(row) for row in scope_rows],
        }

    def replace_work_learning_references(
        self,
        work_id: str,
        source_ids: list[str],
        scopes: list[dict[str, str | None]] | None = None,
        *, _connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        """Atomically replace explicit learning sources and dynamic scopes."""
        validate_scope("writing", work_id)
        if any(
            not isinstance(source_id, str) or not source_id.strip()
            for source_id in source_ids
        ):
            raise ValueError("learning source references must not contain blank IDs")
        unique_ids = list(dict.fromkeys(source_ids))
        if len(unique_ids) > 10_000:
            raise ValueError("a work can reference at most 10000 learning sources")
        normalized_scopes: list[tuple[str | None, str | None]] = []
        for scope in scopes or []:
            learning_work_id = scope.get("learning_work_id")
            folder_id = scope.get("folder_id")
            if learning_work_id is not None and (
                not isinstance(learning_work_id, str) or not learning_work_id.strip()
            ):
                raise ValueError("learning library IDs must not be blank")
            if folder_id is not None and (
                not isinstance(folder_id, str) or not folder_id.strip()
            ):
                raise ValueError("learning folder IDs must not be blank")
            normalized_scopes.append((learning_work_id, folder_id))
        normalized_scopes = list(dict.fromkeys(normalized_scopes))
        if len(normalized_scopes) > 500:
            raise ValueError("a work can reference at most 500 learning scopes")

        with (nullcontext(_connection) if _connection is not None else self.database.transaction()) as connection:
            assert_conversation_write_allowed(connection)
            effect_before = reference_effect_state(connection, "work_learning_references", work_id)
            self._ensure_project_exists(connection, "writing", work_id)
            if unique_ids:
                placeholders = ", ".join("?" for _ in unique_ids)
                rows = connection.execute(
                    f"""
                    SELECT s.id
                    FROM source AS s
                    WHERE s.id IN ({placeholders})
                      AND s.space = 'learning'
                      AND s.deleted_at IS NULL
                      AND (s.work_id IS NULL OR EXISTS (
                          SELECT 1 FROM knowledge_project AS p
                          WHERE p.id = s.work_id
                            AND p.space = 'learning'
                            AND p.status = 'active'
                      ))
                    """,
                    unique_ids,
                ).fetchall()
                if {str(row["id"]) for row in rows} != set(unique_ids):
                    raise ValueError("all references must point to active learning sources")

            learning_work_ids = {
                learning_work_id
                for learning_work_id, _folder_id in normalized_scopes
                if learning_work_id is not None
            }
            if learning_work_ids:
                placeholders = ", ".join("?" for _ in learning_work_ids)
                active_libraries = connection.execute(
                    f"""
                    SELECT id FROM knowledge_project
                    WHERE id IN ({placeholders}) AND space = 'learning' AND status = 'active'
                    """,
                    list(learning_work_ids),
                ).fetchall()
                if {str(row["id"]) for row in active_libraries} != learning_work_ids:
                    raise ValueError("learning scopes must point to active learning libraries")
            folders_by_library: dict[str | None, list[str]] = {}
            for library_id, folder_id in normalized_scopes:
                if folder_id is not None:
                    folders_by_library.setdefault(library_id, []).append(folder_id)
            for library_id, folder_ids in folders_by_library.items():
                if library_id is not None:
                    resolved = self.resolve_learning_work_scope_in_connection(
                        connection, library_id, folder_ids=folder_ids,
                    )
                    if resolved["invalid_folder_ids"]:
                        raise ValueError("learning folder scopes must stay inside the selected library")
                    continue
                placeholders = ", ".join("?" for _ in folder_ids)
                folders = connection.execute(
                    f"""
                    SELECT id FROM knowledge_folder
                    WHERE id IN ({placeholders}) AND space = 'learning'
                      AND work_id IS NULL
                    """,
                    folder_ids,
                ).fetchall()
                if {str(row["id"]) for row in folders} != set(folder_ids):
                    raise ValueError("learning folder scopes must stay inside the selected library")

            connection.execute(
                "DELETE FROM writing_learning_source_reference WHERE work_id = ?",
                (work_id,),
            )
            connection.execute(
                "DELETE FROM writing_learning_scope_reference WHERE work_id = ?",
                (work_id,),
            )
            now = _now()
            connection.executemany(
                """
                INSERT INTO writing_learning_source_reference(work_id, source_id, created_at)
                VALUES (?, ?, ?)
                """,
                [(work_id, source_id, now) for source_id in unique_ids],
            )
            connection.executemany(
                """
                INSERT INTO writing_learning_scope_reference(
                    work_id, learning_work_id, folder_id, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                [
                    (work_id, learning_work_id, folder_id, now)
                    for learning_work_id, folder_id in normalized_scopes
                ],
            )
            record_conversation_effect(
                connection, domain="work_learning_references", target_id=work_id,
                before=effect_before,
                after=reference_effect_state(connection, "work_learning_references", work_id),
            )
            result = self._learning_reference_selection(connection, work_id)
        return result

    def _base_reference_source_ids(
        self,
        connection: sqlite3.Connection,
        *,
        work_id: str,
        base_space: str,
        base_work_id: str,
        exact_source_ids: list[str],
        scope_folder_ids: list[str | None],
    ) -> list[dict[str, Any]]:
        if base_space == "writing":
            rows = connection.execute(
                """
                SELECT DISTINCT s.id AS source_id, s.title
                FROM source AS s
                WHERE s.space = 'writing' AND s.work_id = ? AND s.deleted_at IS NULL
                  AND (
                      s.id IN (SELECT source_id FROM writing_base_source_reference WHERE work_id = ?)
                      OR EXISTS (
                          SELECT 1 FROM writing_base_scope_reference AS scope
                          WHERE scope.work_id = ? AND (
                              scope.folder_id IS NULL OR EXISTS (
                                  SELECT 1 FROM knowledge_folder AS selected
                                  JOIN knowledge_folder AS source_folder
                                    ON source_folder.space = 'writing'
                                   AND source_folder.work_id = selected.work_id
                                  WHERE selected.id = scope.folder_id
                                    AND s.folder_id = source_folder.id
                                    AND (source_folder.path = selected.path COLLATE NOCASE
                                      OR substr(source_folder.path, 1, length(selected.path) + 1)
                                         = selected.path || '/' COLLATE NOCASE)
                              )
                          )
                      )
                  )
                ORDER BY s.title COLLATE NOCASE, s.id
                """,
                (base_work_id, work_id, work_id),
            ).fetchall()
            return [dict(row) for row in rows]

        full_scope = self.resolve_learning_work_scope_in_connection(
            connection, base_work_id
        )
        available_by_id = {
            str(source["source_id"]): source for source in full_scope["sources"]
        }
        exact = set(exact_source_ids).intersection(available_by_id)
        if None in scope_folder_ids:
            selected = set(available_by_id)
        elif scope_folder_ids:
            folder_scope = self.resolve_learning_work_scope_in_connection(
                connection,
                base_work_id,
                folder_ids=[str(folder_id) for folder_id in scope_folder_ids],
            )
            selected = {str(source["source_id"]) for source in folder_scope["sources"]}
        else:
            selected = set()
        return [
            {"source_id": source_id, "title": available_by_id[source_id]["title"]}
            for source_id in sorted(exact | selected, key=lambda item: (
                str(available_by_id[item]["title"]).casefold(), item
            ))
        ]

    def list_work_base_reference(self, work_id: str) -> dict[str, Any] | None:
        """Return persisted exact IDs separately from expanded dynamic scopes."""
        validate_scope("writing", work_id)
        with self.database.connection() as connection:
            self._ensure_project_exists(connection, "writing", work_id)
            row = connection.execute(
                """
                SELECT r.base_space, r.base_work_id, p.title AS base_work_title,
                       r.base_branch_id
                FROM writing_base_work_reference AS r
                JOIN knowledge_project AS p ON p.id = r.base_work_id
                  AND p.space = r.base_space AND p.status = 'active'
                WHERE r.work_id = ?
                """,
                (work_id,),
            ).fetchone()
            if row is None:
                return None
            scope_rows = connection.execute(
                """
                SELECT r.folder_id, f.path AS folder_path
                FROM writing_base_scope_reference AS r
                LEFT JOIN knowledge_folder AS f ON f.id = r.folder_id
                WHERE r.work_id = ?
                ORDER BY folder_path COLLATE NOCASE, r.folder_id
                """,
                (work_id,),
            ).fetchall()
            exact_source_ids = [
                str(source["source_id"])
                for source in connection.execute(
                    "SELECT source_id FROM writing_base_source_reference WHERE work_id = ? ORDER BY source_id",
                    (work_id,),
                ).fetchall()
            ]
            source_rows = self._base_reference_source_ids(
                connection,
                work_id=work_id,
                base_space=str(row["base_space"]),
                base_work_id=str(row["base_work_id"]),
                exact_source_ids=exact_source_ids,
                scope_folder_ids=[scope["folder_id"] for scope in scope_rows],
            )
        return {
            "space": str(row["base_space"]),
            "base_work_id": str(row["base_work_id"]),
            "base_work_title": str(row["base_work_title"]),
            "base_branch_id": (
                str(row["base_branch_id"]) if row["base_branch_id"] is not None else None
            ),
            "exact_source_ids": exact_source_ids,
            "source_ids": [str(source["source_id"]) for source in source_rows],
            "source_titles": [str(source["title"]) for source in source_rows],
            "scope_folder_ids": [scope["folder_id"] for scope in scope_rows],
            "scopes": [dict(scope) for scope in scope_rows],
            "effective_source_count": len(source_rows),
        }

    def list_work_base_reference_filters(self, work_id: str) -> dict[str, Any] | None:
        """Read compact base filters for search without counting/materializing sources."""
        validate_scope("writing", work_id)
        with self.database.connection() as connection:
            self._ensure_project_exists(connection, "writing", work_id)
            row = connection.execute(
                """
                SELECT r.base_space, r.base_work_id, r.base_branch_id
                FROM writing_base_work_reference AS r
                JOIN knowledge_project AS p ON p.id = r.base_work_id
                  AND p.space = r.base_space AND p.status = 'active'
                WHERE r.work_id = ?
                """,
                (work_id,),
            ).fetchone()
            if row is None:
                return None
            exact_ids = [
                str(source["source_id"])
                for source in connection.execute(
                    "SELECT source_id FROM writing_base_source_reference WHERE work_id = ? ORDER BY source_id",
                    (work_id,),
                ).fetchall()
            ]
            scope_ids = [
                scope["folder_id"]
                for scope in connection.execute(
                    "SELECT folder_id FROM writing_base_scope_reference WHERE work_id = ? ORDER BY folder_id",
                    (work_id,),
                ).fetchall()
            ]
            if row["base_space"] == "learning":
                resolved = self.resolve_learning_work_scope_in_connection(
                    connection,
                    str(row["base_work_id"]),
                    source_ids=exact_ids,
                )
                if resolved["invalid_source_ids"]:
                    exact_ids = [source_id for source_id in exact_ids
                                 if source_id not in resolved["invalid_source_ids"]]
                if scope_ids and None not in scope_ids:
                    folders = self.resolve_learning_work_scope_in_connection(
                        connection,
                        str(row["base_work_id"]),
                        folder_ids=[str(folder_id) for folder_id in scope_ids],
                    )
                    scope_ids = [
                        folder_id for folder_id in scope_ids
                        if folder_id not in folders["invalid_folder_ids"]
                    ]
        return {
            "space": str(row["base_space"]),
            "base_work_id": str(row["base_work_id"]),
            "base_branch_id": (
                str(row["base_branch_id"]) if row["base_branch_id"] is not None else None
            ),
            "source_ids": exact_ids,
            "scope_folder_ids": scope_ids,
        }

    def replace_work_base_reference(
        self,
        work_id: str,
        *,
        base_work_id: str | None,
        base_branch_id: str | None,
        source_ids: list[str],
        scope_folder_ids: list[str | None] | None = None,
        space: str = "writing",
    ) -> dict[str, Any] | None:
        """Replace one work's exact and dynamic, read-only base references atomically."""
        validate_scope("writing", work_id)
        if space not in {"writing", "learning"}:
            raise ValueError("base space must be writing or learning")
        normalized_source_ids = list(dict.fromkeys(source_ids))
        if any(not isinstance(source_id, str) or not source_id.strip() for source_id in source_ids):
            raise ValueError("base source references must not contain blank IDs")
        if len(normalized_source_ids) > 500:
            raise ValueError("a work can reference at most 500 base sources")
        normalized_scope_folder_ids = list(dict.fromkeys(scope_folder_ids or []))
        if any(
            folder_id is not None and (not isinstance(folder_id, str) or not folder_id.strip())
            for folder_id in normalized_scope_folder_ids
        ):
            raise ValueError("base folder scope IDs must not be blank")
        if len(normalized_scope_folder_ids) > 500:
            raise ValueError("a work can reference at most 500 base scopes")
        branch = base_branch_id.strip() if base_branch_id is not None else None
        if base_work_id is None:
            if normalized_source_ids or normalized_scope_folder_ids:
                raise ValueError("base source and scope IDs require a selected base work")
        elif not base_work_id.strip():
            raise ValueError("base_work_id must not be blank")
        elif space == "writing":
            if branch is None:
                raise ValueError("writing base references require a branch")
            validate_temporal_branch_id(branch)
        elif branch is not None:
            raise ValueError("learning base references do not use a branch")

        with self.database.transaction() as connection:
            assert_conversation_write_allowed(connection)
            effect_before = reference_effect_state(connection, "work_base_reference", work_id)
            self._ensure_project_exists(connection, "writing", work_id)
            if base_work_id is None:
                connection.execute(
                    "DELETE FROM writing_base_work_reference WHERE work_id = ?",
                    (work_id,),
                )
                record_conversation_effect(
                    connection, domain="work_base_reference", target_id=work_id,
                    before=effect_before,
                    after=reference_effect_state(connection, "work_base_reference", work_id),
                )
                return None
            if space == "writing" and base_work_id == work_id:
                raise ValueError("a work cannot reference itself as its base")
            base = connection.execute(
                """
                SELECT id FROM knowledge_project
                WHERE id = ? AND space = ? AND status = 'active'
                """,
                (base_work_id, space),
            ).fetchone()
            if base is None:
                raise ValueError(f"base_work_id must reference an active {space} work")

            folder_ids = [
                str(folder_id) for folder_id in normalized_scope_folder_ids
                if folder_id is not None
            ]
            if space == "writing":
                if normalized_source_ids:
                    placeholders = ", ".join("?" for _ in normalized_source_ids)
                    source_rows = connection.execute(
                        f"""SELECT id FROM source WHERE id IN ({placeholders})
                            AND space = 'writing' AND work_id = ? AND deleted_at IS NULL""",
                        [*normalized_source_ids, base_work_id],
                    ).fetchall()
                    if {str(row["id"]) for row in source_rows} != set(normalized_source_ids):
                        raise ValueError("all base sources must belong to the selected active base work")
                if folder_ids:
                    placeholders = ", ".join("?" for _ in folder_ids)
                    folder_rows = connection.execute(
                        f"""SELECT id FROM knowledge_folder WHERE id IN ({placeholders})
                            AND space = 'writing' AND work_id = ?""",
                        [*folder_ids, base_work_id],
                    ).fetchall()
                    if {str(folder["id"]) for folder in folder_rows} != set(folder_ids):
                        raise ValueError("all base folder scopes must belong to the selected base work")
            else:
                source_scope = self.resolve_learning_work_scope_in_connection(
                    connection, base_work_id, source_ids=normalized_source_ids
                )
                folder_scope = self.resolve_learning_work_scope_in_connection(
                    connection, base_work_id, folder_ids=folder_ids
                ) if folder_ids else {"invalid_folder_ids": []}
                if source_scope["invalid_source_ids"]:
                    raise ValueError("all base sources must belong to the selected learning work")
                if folder_scope["invalid_folder_ids"]:
                    raise ValueError("all base folder scopes must belong to the selected learning work")

            now = _now()
            connection.execute(
                "DELETE FROM writing_base_work_reference WHERE work_id = ?", (work_id,)
            )
            connection.execute(
                """
                INSERT INTO writing_base_work_reference(
                    work_id, base_space, base_work_id, base_branch_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (work_id, space, base_work_id, branch, now, now),
            )
            connection.executemany(
                "INSERT INTO writing_base_source_reference(work_id, source_id, created_at) VALUES (?, ?, ?)",
                [(work_id, source_id, now) for source_id in normalized_source_ids],
            )
            connection.executemany(
                "INSERT INTO writing_base_scope_reference(work_id, folder_id, created_at) VALUES (?, ?, ?)",
                [(work_id, folder_id, now) for folder_id in normalized_scope_folder_ids],
            )
            record_conversation_effect(
                connection, domain="work_base_reference", target_id=work_id,
                before=effect_before,
                after=reference_effect_state(connection, "work_base_reference", work_id),
            )
        return self.list_work_base_reference(work_id)


