"""Persistence for explicit learning-work scope and pinned chapter-order manifests."""

import re

from .repository_support import *
from .repository_conversation_effects import (
    assert_conversation_write_allowed,
    learning_work_effect_state,
    record_conversation_effect,
)


def _normalize_story_coordinate_id(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > 200:
        raise ValueError(f"{field_name} must be a non-blank identifier of at most 200 characters")
    return value


def _normalize_story_coordinate_ids(
    values: Iterable[str], field_name: str, *, max_count: int,
) -> list[str]:
    result = list(values)
    if len(result) > max_count:
        raise ValueError(f"{field_name} cannot contain more than {max_count} IDs")
    normalized = [_normalize_story_coordinate_id(value, field_name) for value in result]
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{field_name} must not contain duplicate IDs")
    return normalized


class KnowledgeStoryCoordinatesRepositoryMixin:
    """Stable library identity and narrative-order operations for one SQLite owner."""

    def register_learning_work(
        self,
        *,
        work_id: str,
        title: str,
        description: str = "",
        folder_ids: Iterable[str] = (),
    ) -> dict[str, Any]:
        """Register a caller-chosen stable ID and bind existing learning folders atomically."""
        normalized_id = _normalize_story_coordinate_id(work_id, "work_id")
        normalized_title = _normalize_story_coordinate_id(title, "title")
        if len(normalized_title) > 300:
            raise ValueError("title cannot exceed 300 characters")
        if not isinstance(description, str):
            raise ValueError("description must be text")
        folders = _normalize_story_coordinate_ids(folder_ids, "folder_ids", max_count=500)
        now = _now()
        with self.database.transaction() as connection:
            assert_conversation_write_allowed(connection)
            effect_before = learning_work_effect_state(connection, normalized_id)
            project = connection.execute(
                "SELECT space, title, description, status FROM knowledge_project WHERE id = ?",
                (normalized_id,),
            ).fetchone()
            if project is None:
                connection.execute(
                    """
                    INSERT INTO knowledge_project(
                        id, space, title, description, status, created_at, updated_at
                    ) VALUES (?, 'learning', ?, ?, 'active', ?, ?)
                    """,
                    (normalized_id, normalized_title, description.strip(), now, now),
                )
            elif (
                project["space"] != "learning"
                or project["status"] != "active"
                or project["title"] != normalized_title
            ):
                raise ValueError("work_id is already used by a different or inactive project")
            self._validate_learning_folder_ids(connection, normalized_id, folders)
            connection.executemany(
                """
                INSERT OR IGNORE INTO knowledge_learning_work_folder(
                    work_id, folder_id, created_at
                ) VALUES (?, ?, ?)
                """,
                [(normalized_id, folder_id, now) for folder_id in folders],
            )
            effect_after = learning_work_effect_state(connection, normalized_id)
            if effect_after != effect_before:
                record_conversation_effect(
                    connection, domain="learning_work_registration", target_id=normalized_id,
                    before=effect_before, after=effect_after,
                )
        return self.get_learning_work(normalized_id)

    def assign_learning_work_folders(
        self,
        work_id: str,
        folder_ids: Iterable[str],
        *,
        replace: bool = False,
    ) -> dict[str, Any]:
        """Add or replace folder-ID scope without changing source or snapshot identities."""
        normalized_id = _normalize_story_coordinate_id(work_id, "work_id")
        folders = _normalize_story_coordinate_ids(folder_ids, "folder_ids", max_count=500)
        if not isinstance(replace, bool):
            raise ValueError("replace must be a boolean")
        now = _now()
        with self.database.transaction() as connection:
            assert_conversation_write_allowed(connection)
            effect_before = learning_work_effect_state(connection, normalized_id)
            self._ensure_project_exists(connection, "learning", normalized_id)
            self._validate_learning_folder_ids(connection, normalized_id, folders)
            if replace:
                connection.execute(
                    "DELETE FROM knowledge_learning_work_folder WHERE work_id = ?",
                    (normalized_id,),
                )
            connection.executemany(
                """
                INSERT OR IGNORE INTO knowledge_learning_work_folder(
                    work_id, folder_id, created_at
                ) VALUES (?, ?, ?)
                """,
                [(normalized_id, folder_id, now) for folder_id in folders],
            )
            effect_after = learning_work_effect_state(connection, normalized_id)
            if effect_after != effect_before:
                record_conversation_effect(
                    connection, domain="learning_work_registration", target_id=normalized_id,
                    before=effect_before, after=effect_after,
                )
        return self.get_learning_work(normalized_id)

    @staticmethod
    def _validate_learning_folder_ids(
        connection: sqlite3.Connection, work_id: str, folder_ids: list[str],
    ) -> None:
        if not folder_ids:
            return
        placeholders = ", ".join("?" for _ in folder_ids)
        rows = connection.execute(
            f"""
            SELECT id FROM knowledge_folder
            WHERE id IN ({placeholders}) AND space = 'learning'
              AND (work_id IS NULL OR work_id = ?)
            """,
            [*folder_ids, work_id],
        ).fetchall()
        if {str(row["id"]) for row in rows} != set(folder_ids):
            raise ValueError("all assigned folders must be learning folders in this work scope")

    def get_learning_work(self, work_id: str) -> dict[str, Any]:
        normalized_id = _normalize_story_coordinate_id(work_id, "work_id")
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT id, title, description, status, created_at, updated_at
                FROM knowledge_project
                WHERE id = ? AND space = 'learning'
                """,
                (normalized_id,),
            ).fetchone()
            if row is None:
                raise ValueError("learning work does not exist")
            folders = connection.execute(
                """
                SELECT assigned.folder_id, folder.path
                FROM knowledge_learning_work_folder AS assigned
                JOIN knowledge_folder AS folder ON folder.id = assigned.folder_id
                WHERE assigned.work_id = ?
                ORDER BY folder.path COLLATE NOCASE, assigned.folder_id
                """,
                (normalized_id,),
            ).fetchall()
        result = dict(row)
        result["folder_assignments"] = [dict(folder) for folder in folders]
        return result

    def list_learning_work_sources(
        self, work_id: str, *, include_snapshot_length: bool = True,
    ) -> list[dict[str, Any]]:
        """Enumerate explicit work sources and sources under assigned folder IDs."""
        normalized_id = _normalize_story_coordinate_id(work_id, "work_id")
        with self.database.connection() as connection:
            self._ensure_project_exists(connection, "learning", normalized_id)
            return [dict(row) for row in self._learning_work_source_rows(
                connection, normalized_id, include_snapshot_length=include_snapshot_length,
            )]

    def resolve_learning_work_scope(
        self,
        work_id: str,
        *,
        folder_ids: Iterable[str] | None = None,
        source_ids: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """Resolve one stable work into exact existing learning sources and folders.

        The resolver follows folder IDs assigned to the stable work and current folder
        ancestry, while preserving every physical source/work/snapshot identity. A
        supplied empty filter remains empty; invalid IDs are reported and never widen
        the result to the entire learning library.
        """
        normalized_id = _normalize_story_coordinate_id(work_id, "work_id")
        selected_folders = (
            None if folder_ids is None
            else _normalize_story_coordinate_ids(folder_ids, "folder_ids", max_count=500)
        )
        selected_sources = (
            None if source_ids is None
            else _normalize_story_coordinate_ids(source_ids, "source_ids", max_count=10_000)
        )
        with self.database.connection() as connection:
            return self.resolve_learning_work_scope_in_connection(
                connection,
                normalized_id,
                folder_ids=selected_folders,
                source_ids=selected_sources,
            )

    def resolve_learning_work_scope_in_connection(
        self,
        connection: sqlite3.Connection,
        work_id: str,
        *,
        folder_ids: Iterable[str] | None = None,
        source_ids: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """Resolve stable work scope using the caller's current SQLite transaction."""
        normalized_id = _normalize_story_coordinate_id(work_id, "work_id")
        selected_folders = (
            None if folder_ids is None
            else _normalize_story_coordinate_ids(folder_ids, "folder_ids", max_count=500)
        )
        selected_sources = (
            None if source_ids is None
            else _normalize_story_coordinate_ids(source_ids, "source_ids", max_count=10_000)
        )
        with nullcontext(connection):
            self._ensure_project_exists(connection, "learning", normalized_id)
            all_sources = [
                dict(row)
                for row in self._learning_work_source_rows(connection, normalized_id)
            ]
            all_source_ids = {str(source["source_id"]) for source in all_sources}
            if selected_sources is None:
                allowed_source_ids = all_source_ids
                invalid_source_ids: list[str] = []
            else:
                allowed_source_ids = all_source_ids.intersection(selected_sources)
                invalid_source_ids = sorted(set(selected_sources) - all_source_ids)

            folder_rows = connection.execute(
                """
                SELECT folder.id, folder.space, folder.work_id, folder.path,
                       folder.created_at, folder.updated_at
                FROM knowledge_folder AS folder
                WHERE folder.space = 'learning'
                  AND (
                      folder.work_id = ?
                      OR EXISTS (
                          SELECT 1
                          FROM knowledge_learning_work_folder AS assigned
                          JOIN knowledge_folder AS root ON root.id = assigned.folder_id
                          WHERE assigned.work_id = ?
                            AND root.work_id IS folder.work_id
                            AND (
                                folder.path = root.path COLLATE NOCASE
                                OR substr(folder.path, 1, length(root.path) + 1)
                                   = root.path || '/' COLLATE NOCASE
                                OR substr(root.path, 1, length(folder.path) + 1)
                                   = folder.path || '/' COLLATE NOCASE
                            )
                      )
                  )
                ORDER BY folder.work_id, folder.path COLLATE NOCASE, folder.path
                """,
                (normalized_id, normalized_id),
            ).fetchall()
        all_folder_rows = [dict(row) for row in folder_rows]
        all_folder_ids = {str(folder["id"]) for folder in all_folder_rows}
        if selected_folders is None:
            visible_folder_ids = all_folder_ids
            invalid_folder_ids: list[str] = []
            folder_scoped_source_ids = all_source_ids
        else:
            valid_selected_folder_ids = all_folder_ids.intersection(selected_folders)
            invalid_folder_ids = sorted(set(selected_folders) - all_folder_ids)
            visible_folder_ids = set(valid_selected_folder_ids)
            folder_scoped_source_ids: set[str] = set()
            by_owner_path = {
                (folder["work_id"], str(folder["path"]).casefold()): str(folder["id"])
                for folder in all_folder_rows
            }
            selected_rows = [
                folder for folder in all_folder_rows
                if str(folder["id"]) in valid_selected_folder_ids
            ]
            source_folders_by_id = {
                str(folder["id"]): folder for folder in all_folder_rows
            }
            for source in all_sources:
                source_folder_id = source.get("folder_id")
                if source_folder_id is None:
                    continue
                source_folder = source_folders_by_id.get(str(source_folder_id))
                if source_folder is None:
                    continue
                source_path = str(source_folder["path"]).casefold()
                source_owner = source_folder["work_id"]
                matched = False
                for selected_folder in selected_rows:
                    if selected_folder["work_id"] != source_owner:
                        continue
                    selected_path = str(selected_folder["path"]).casefold()
                    if source_path == selected_path or source_path.startswith(selected_path + "/"):
                        matched = True
                        break
                if matched:
                    folder_scoped_source_ids.add(str(source["source_id"]))
            # Keep only selected subtrees and their ancestors as tree scaffolding.
            for selected_folder in selected_rows:
                owner = selected_folder["work_id"]
                path = str(selected_folder["path"]).casefold()
                visible_folder_ids.update(
                    folder_id
                    for (folder_owner, folder_path), folder_id in by_owner_path.items()
                    if folder_owner == owner
                    and (path == folder_path or path.startswith(folder_path + "/"))
                )
                visible_folder_ids.update(
                    folder_id
                    for (folder_owner, folder_path), folder_id in by_owner_path.items()
                    if folder_owner == owner
                    and (folder_path == path or path.startswith(folder_path + "/"))
                )

        if selected_sources is not None and selected_folders is not None:
            # Existing study selection means "these sources OR these folders".
            # Keep that UI contract while still intersecting each side with the
            # explicitly registered stable-work scope.
            scoped_source_ids = allowed_source_ids.union(folder_scoped_source_ids)
        else:
            scoped_source_ids = allowed_source_ids.intersection(folder_scoped_source_ids)
        sources = [
            source for source in all_sources
            if str(source["source_id"]) in scoped_source_ids
        ]
        folders = [
            folder for folder in all_folder_rows
            if str(folder["id"]) in visible_folder_ids
        ]
        folder_id_to_row = {str(folder["id"]): folder for folder in folders}
        source_counts = {folder_id: 0 for folder_id in folder_id_to_row}
        direct_source_counts = {folder_id: 0 for folder_id in folder_id_to_row}
        owner_paths: dict[tuple[str | None, str], str] = {
            (folder["work_id"], str(folder["path"]).casefold()): str(folder["id"])
            for folder in folders
        }
        for source in sources:
            source_folder_id = source.get("folder_id")
            if source_folder_id is None:
                continue
            source_folder = folder_id_to_row.get(str(source_folder_id))
            if source_folder is None:
                continue
            direct_source_counts[str(source_folder_id)] += 1
            source_owner = source_folder["work_id"]
            source_path = str(source_folder["path"])
            for index in range(1, len(source_path.split("/")) + 1):
                ancestor_path = "/".join(source_path.split("/")[:index]).casefold()
                ancestor_id = owner_paths.get((source_owner, ancestor_path))
                if ancestor_id is not None:
                    source_counts[ancestor_id] += 1

        child_counts = {folder_id: 0 for folder_id in folder_id_to_row}
        folder_summaries: list[dict[str, Any]] = []
        for folder in folders:
            folder_id = str(folder["id"])
            path = str(folder["path"])
            parent_path = path.rpartition("/")[0].casefold()
            parent_id = owner_paths.get((folder["work_id"], parent_path)) if parent_path else None
            if parent_id is not None:
                child_counts[parent_id] += 1
            folder_summaries.append(
                {
                    **folder,
                    "name": path.rsplit("/", 1)[-1],
                    "parent_id": parent_id,
                    "source_count": source_counts[folder_id],
                    "child_count": child_counts.get(folder_id, 0),
                    "direct_source_count": direct_source_counts[folder_id],
                }
            )
        # child_count is computed after the first pass so each parent has its final value.
        for folder in folder_summaries:
            folder["child_count"] = child_counts[str(folder["id"])]

        owner_ids = sorted(
            {source.get("work_id") for source in sources},
            key=lambda value: (value is not None, str(value or "")),
        )
        return {
            "work_id": normalized_id,
            "sources": sources,
            "source_ids": [str(source["source_id"]) for source in sources],
            "physical_work_ids": owner_ids,
            "folders": folder_summaries,
            "folder_ids": [str(folder["id"]) for folder in folders],
            "invalid_source_ids": invalid_source_ids,
            "invalid_folder_ids": invalid_folder_ids,
        }

    @staticmethod
    def _learning_work_source_rows(
        connection: sqlite3.Connection, work_id: str,
        *, include_snapshot_length: bool = True,
    ) -> list[sqlite3.Row]:
        # Manifest coverage compares identities and hashes, so that caller need
        # not scan every chapter's text to compute a Unicode character length.
        snapshot_length = "length(snapshot.content)" if include_snapshot_length else "NULL"
        return connection.execute(
            f"""
            SELECT source.id AS source_id, source.title, source.folder_id, source.work_id,
                   source.created_at,
                   source_folder.path AS folder_path,
                   current.snapshot_id, snapshot.content_hash,
                   {snapshot_length} AS snapshot_length,
                   CASE WHEN snapshot.id IS NULL THEN 0 ELSE 1 END AS has_snapshot
            FROM source
            LEFT JOIN knowledge_folder AS source_folder
              ON source_folder.id = source.folder_id
             AND source_folder.space = source.space
             AND source_folder.work_id IS source.work_id
            LEFT JOIN source_current_snapshot AS current ON current.source_id = source.id
            LEFT JOIN source_snapshot AS snapshot
              ON snapshot.id = current.snapshot_id AND snapshot.source_id = source.id
            WHERE source.space = 'learning' AND source.deleted_at IS NULL
              AND (
                  source.work_id = ?
                  OR EXISTS (
                      SELECT 1
                      FROM knowledge_learning_work_folder AS assigned
                      JOIN knowledge_folder AS root ON root.id = assigned.folder_id
                      JOIN knowledge_folder AS source_folder
                        ON source_folder.id = source.folder_id
                       AND source_folder.space = 'learning'
                       AND source_folder.work_id IS source.work_id
                      WHERE assigned.work_id = ?
                        AND root.work_id IS source_folder.work_id
                        AND (
                            source_folder.path = root.path COLLATE NOCASE
                            OR substr(source_folder.path, 1, length(root.path) + 1)
                               = root.path || '/' COLLATE NOCASE
                        )
                  )
              )
            ORDER BY source.id
            """,
            (work_id, work_id),
        ).fetchall()

    def resolve_learning_source_manifest_pin(
        self,
        *,
        source_ids: Iterable[str],
        scope_work_id: str | None,
        folder_id: str | None,
    ) -> dict[str, Any]:
        """Resolve a unique registered work and its verified current source pins.

        Physical source ownership remains unchanged. A source may be covered by
        a stable learning work through its assigned folder tree, but overlapping
        work assignments are deliberately ambiguous unless the caller names the
        stable work explicitly.
        """
        selected_ids = _normalize_story_coordinate_ids(
            source_ids, "source_ids", max_count=10_000,
        )
        if not selected_ids:
            raise ValueError("source_ids must not be empty")
        normalized_work_id = (
            _normalize_story_coordinate_id(scope_work_id, "scope_work_id")
            if scope_work_id is not None else None
        )
        normalized_folder_id = (
            _normalize_story_coordinate_id(folder_id, "folder_id")
            if folder_id is not None else None
        )
        with self.database.connection() as connection:
            if normalized_work_id is not None:
                project = connection.execute(
                    """
                    SELECT id FROM knowledge_project
                    WHERE id = ? AND space = 'learning' AND status = 'active'
                    """,
                    (normalized_work_id,),
                ).fetchone()
                candidate_work_ids = (
                    [normalized_work_id] if project is not None else []
                )
            else:
                candidate_work_ids = [
                    str(row["id"])
                    for row in connection.execute(
                        """
                        SELECT id FROM knowledge_project
                        WHERE space = 'learning' AND status = 'active'
                        ORDER BY id
                        """
                    ).fetchall()
                ]

            selected_set = set(selected_ids)
            matching: list[tuple[str, dict[str, sqlite3.Row]]] = []
            for candidate_work_id in candidate_work_ids:
                source_rows = self._learning_work_source_rows(
                    connection, candidate_work_id,
                )
                rows_by_id = {
                    str(row["source_id"]): row for row in source_rows
                }
                if not selected_set.issubset(rows_by_id):
                    continue
                if normalized_folder_id is not None:
                    folder_scope = self.resolve_learning_work_scope_in_connection(
                        connection,
                        candidate_work_id,
                        folder_ids=[normalized_folder_id],
                        source_ids=[],
                    )
                    if (
                        folder_scope["invalid_folder_ids"]
                        or not selected_set.issubset(set(folder_scope["source_ids"]))
                    ):
                        continue
                matching.append((candidate_work_id, rows_by_id))

            if len(matching) != 1:
                status = (
                    "ambiguous" if len(matching) > 1
                    else "scope_mismatch" if normalized_work_id is not None and candidate_work_ids
                    else "unavailable"
                )
                return {
                    "status": status,
                    "reason": (
                        "multiple_registered_works_cover_sources"
                        if status == "ambiguous"
                        else "sources_outside_registered_work_scope"
                        if status == "scope_mismatch"
                        else "no_unique_registered_work"
                    ),
                    "work_id": None,
                    "candidate_work_ids": [work_id for work_id, _ in matching],
                    "manifest_revision": None,
                    "manifest_hash": None,
                    "manifest_coordinate_status": "unverified",
                    "scope_source_ids": [],
                    "positions": {},
                }

            stable_work_id, source_rows_by_id = matching[0]
            current = connection.execute(
                "SELECT revision FROM knowledge_story_order_current WHERE work_id = ?",
                (stable_work_id,),
            ).fetchone()
            if current is None:
                return {
                    "status": "unavailable",
                    "reason": "chapter_order_manifest_missing",
                    "work_id": stable_work_id,
                    "candidate_work_ids": [stable_work_id],
                    "manifest_revision": None,
                    "manifest_hash": None,
                    "manifest_coordinate_status": "unverified",
                    "scope_source_ids": sorted(selected_set),
                    "positions": {},
                }
            revision = int(current["revision"])
            order = self._read_chapter_order(connection, stable_work_id, revision)
            base = {
                "work_id": stable_work_id,
                "candidate_work_ids": [stable_work_id],
                "manifest_revision": revision,
                "manifest_hash": str(order["manifest_hash"]),
                "manifest_coordinate_status": str(order["coordinate_status"]),
                "scope_source_ids": sorted(selected_set),
                "positions": {},
            }
            if order["coordinate_status"] != "verified":
                return {
                    **base,
                    "status": "unavailable",
                    "reason": "chapter_order_manifest_not_current_and_complete",
                }

            entries_by_id = {
                str(entry["source_id"]): entry for entry in order["entries"]
            }
            positions: dict[str, dict[str, Any]] = {}
            for source_id in selected_ids:
                source_row = source_rows_by_id[source_id]
                entry = entries_by_id.get(source_id)
                if (
                    entry is None
                    or not entry["snapshot_is_current"]
                    or source_row["snapshot_id"] is None
                    or str(source_row["snapshot_id"]) != str(entry["snapshot_id"])
                    or str(source_row["content_hash"]) != str(entry["content_hash"])
                ):
                    return {
                        **base,
                        "status": "unavailable",
                        "reason": "source_snapshot_not_pinned_by_current_manifest",
                    }
                positions[source_id] = {
                    "domain": "narrative_order",
                    "space": "learning",
                    "work_id": stable_work_id,
                    "branch_id": None,
                    "manifest_revision": revision,
                    "manifest_hash": str(order["manifest_hash"]),
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
            return {
                **base,
                "status": "verified",
                "reason": "unique_registered_work_and_current_complete_manifest",
                "positions": positions,
            }

    def is_learning_source_manifest_pin_current(
        self,
        *,
        work_id: str,
        manifest_revision: int,
        manifest_hash: str,
        source_id: str,
        snapshot_id: str,
        content_hash: str,
        narrative_ordinal: int,
    ) -> bool:
        """Check the parser's complete manifest pin immediately before publication."""
        normalized_work_id = _normalize_story_coordinate_id(work_id, "work_id")
        normalized_source_id = _normalize_story_coordinate_id(source_id, "source_id")
        normalized_snapshot_id = _normalize_story_coordinate_id(snapshot_id, "snapshot_id")
        if (
            isinstance(manifest_revision, bool)
            or not isinstance(manifest_revision, int)
            or manifest_revision < 1
            or not isinstance(manifest_hash, str)
            or not re.fullmatch(r"[0-9a-f]{64}", manifest_hash)
            or not isinstance(content_hash, str)
            or not re.fullmatch(r"[0-9a-f]{64}", content_hash)
            or isinstance(narrative_ordinal, bool)
            or not isinstance(narrative_ordinal, int)
            or narrative_ordinal < 0
        ):
            return False
        with self.database.connection() as connection:
            return self.is_learning_source_manifest_pin_current_in_connection(
                connection,
                work_id=normalized_work_id,
                manifest_revision=manifest_revision,
                manifest_hash=manifest_hash,
                source_id=normalized_source_id,
                snapshot_id=normalized_snapshot_id,
                content_hash=content_hash,
                narrative_ordinal=narrative_ordinal,
            )

    def is_learning_source_manifest_pin_current_in_connection(
        self,
        connection: sqlite3.Connection,
        *,
        work_id: str,
        manifest_revision: int,
        manifest_hash: str,
        source_id: str,
        snapshot_id: str,
        content_hash: str,
        narrative_ordinal: int,
    ) -> bool:
        """Verify a pin in the caller's write transaction for publication CAS."""
        normalized_work_id = _normalize_story_coordinate_id(work_id, "work_id")
        normalized_source_id = _normalize_story_coordinate_id(source_id, "source_id")
        normalized_snapshot_id = _normalize_story_coordinate_id(snapshot_id, "snapshot_id")
        if (
            isinstance(manifest_revision, bool)
            or not isinstance(manifest_revision, int)
            or manifest_revision < 1
            or not isinstance(manifest_hash, str)
            or not re.fullmatch(r"[0-9a-f]{64}", manifest_hash)
            or not isinstance(content_hash, str)
            or not re.fullmatch(r"[0-9a-f]{64}", content_hash)
            or isinstance(narrative_ordinal, bool)
            or not isinstance(narrative_ordinal, int)
            or narrative_ordinal < 0
        ):
            return False
        current = connection.execute(
            "SELECT revision FROM knowledge_story_order_current WHERE work_id = ?",
            (normalized_work_id,),
        ).fetchone()
        if current is None or int(current["revision"]) != manifest_revision:
            return False
        try:
            order = self._read_chapter_order(
                connection, normalized_work_id, manifest_revision,
            )
        except ValueError:
            return False
        if (
            order["coordinate_status"] != "verified"
            or str(order["manifest_hash"]) != manifest_hash
        ):
            return False
        entry = next(
            (
                row for row in order["entries"]
                if row["source_id"] == normalized_source_id
            ),
            None,
        )
        return bool(
            entry is not None
            and entry["snapshot_is_current"]
            and str(entry["snapshot_id"]) == normalized_snapshot_id
            and str(entry["content_hash"]) == content_hash
            and int(entry["narrative_ordinal"]) == narrative_ordinal
        )

    def set_learning_chapter_order(
        self,
        work_id: str,
        source_ids: Iterable[str],
        *,
        complete: bool = True,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Append a chapter-order revision pinned to the exact current source snapshots."""
        normalized_id = _normalize_story_coordinate_id(work_id, "work_id")
        ordered_ids = _normalize_story_coordinate_ids(source_ids, "source_ids", max_count=100_000)
        if not ordered_ids:
            raise ValueError("chapter order must contain at least one source")
        if not isinstance(complete, bool):
            raise ValueError("complete must be a boolean")
        if expected_revision is not None and (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            raise ValueError("expected_revision must be a non-negative integer")

        with self.database.transaction() as connection:
            self._ensure_project_exists(connection, "learning", normalized_id)
            current = connection.execute(
                """
                SELECT current.revision, revision.manifest_hash
                FROM knowledge_story_order_current AS current
                JOIN knowledge_story_order_revision AS revision
                  ON revision.work_id = current.work_id AND revision.revision = current.revision
                WHERE current.work_id = ?
                """,
                (normalized_id,),
            ).fetchone()
            current_revision = int(current["revision"]) if current is not None else 0
            if expected_revision is not None and expected_revision != current_revision:
                raise ValueError(
                    f"chapter-order revision changed (expected {expected_revision}, current {current_revision})"
                )

            scope_rows = self._learning_work_source_rows(connection, normalized_id)
            scope_by_id = {str(row["source_id"]): row for row in scope_rows}
            requested_set = set(ordered_ids)
            scope_set = set(scope_by_id)
            if not requested_set.issubset(scope_set):
                raise ValueError("chapter order contains sources outside the registered learning work")
            if complete and requested_set != scope_set:
                missing = sorted(scope_set - requested_set)
                extra = sorted(requested_set - scope_set)
                raise ValueError(
                    f"complete chapter order must include every scoped source exactly once; "
                    f"missing={missing[:10]}, extra={extra[:10]}"
                )
            missing_snapshots = [
                source_id for source_id in ordered_ids
                if not int(scope_by_id[source_id]["has_snapshot"])
            ]
            if missing_snapshots:
                raise ValueError("every ordered source must have a current pinned snapshot")

            entries = [
                {
                    "source_id": source_id,
                    "snapshot_id": str(scope_by_id[source_id]["snapshot_id"]),
                    "content_hash": str(scope_by_id[source_id]["content_hash"]),
                    "chapter_title": str(scope_by_id[source_id]["title"]),
                    "chapter_label": str(scope_by_id[source_id]["title"]),
                }
                for source_id in ordered_ids
            ]
            material = {
                "work_id": normalized_id,
                "coverage_status": "complete" if complete else "partial",
                "expected_source_ids": sorted(scope_set),
                "entries": entries,
            }
            manifest_hash = hashlib.sha256(
                json.dumps(
                    material, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest()
            if current is not None and str(current["manifest_hash"]) == manifest_hash:
                return self._read_chapter_order(connection, normalized_id, current_revision)

            revision_number = current_revision + 1
            connection.execute(
                """
                INSERT INTO knowledge_story_order_revision(
                    work_id, revision, manifest_hash, coverage_status,
                    entry_count, expected_source_count, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_id, revision_number, manifest_hash,
                    "complete" if complete else "partial", len(entries), len(scope_set), _now(),
                ),
            )
            connection.executemany(
                """
                INSERT INTO knowledge_story_order_entry(
                    work_id, revision, narrative_ordinal, source_id, snapshot_id,
                    content_hash, chapter_title, chapter_label
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        normalized_id, revision_number, ordinal, entry["source_id"],
                        entry["snapshot_id"], entry["content_hash"],
                        entry["chapter_title"], entry["chapter_label"],
                    )
                    for ordinal, entry in enumerate(entries)
                ],
            )
            connection.execute(
                """
                INSERT INTO knowledge_story_order_current(work_id, revision, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(work_id) DO UPDATE SET
                    revision = excluded.revision, updated_at = excluded.updated_at
                """,
                (normalized_id, revision_number, _now()),
            )
            return self._read_chapter_order(connection, normalized_id, revision_number)

    def get_learning_chapter_order(
        self, work_id: str, *, revision: int | None = None,
    ) -> dict[str, Any] | None:
        normalized_id = _normalize_story_coordinate_id(work_id, "work_id")
        if revision is not None and (
            isinstance(revision, bool) or not isinstance(revision, int) or revision < 1
        ):
            raise ValueError("revision must be a positive integer")
        with self.database.connection() as connection:
            self._ensure_project_exists(connection, "learning", normalized_id)
            selected = revision
            if selected is None:
                row = connection.execute(
                    "SELECT revision FROM knowledge_story_order_current WHERE work_id = ?",
                    (normalized_id,),
                ).fetchone()
                if row is None:
                    return None
                selected = int(row["revision"])
            return self._read_chapter_order(connection, normalized_id, selected)

    @classmethod
    def _read_chapter_order(
        cls, connection: sqlite3.Connection, work_id: str, revision: int,
    ) -> dict[str, Any]:
        header = connection.execute(
            """
            SELECT revision.work_id, revision.revision, revision.manifest_hash,
                   revision.coverage_status, revision.entry_count,
                   revision.expected_source_count, revision.created_at,
                   current.revision AS current_revision
            FROM knowledge_story_order_revision AS revision
            LEFT JOIN knowledge_story_order_current AS current
              ON current.work_id = revision.work_id
            WHERE revision.work_id = ? AND revision.revision = ?
            """,
            (work_id, revision),
        ).fetchone()
        if header is None:
            raise ValueError("chapter-order revision does not exist")
        rows = connection.execute(
            """
            SELECT entry.narrative_ordinal, entry.source_id, entry.snapshot_id,
                   entry.content_hash, entry.chapter_title, entry.chapter_label,
                   current.snapshot_id AS current_snapshot_id
            FROM knowledge_story_order_entry AS entry
            LEFT JOIN source_current_snapshot AS current ON current.source_id = entry.source_id
            WHERE entry.work_id = ? AND entry.revision = ?
            ORDER BY entry.narrative_ordinal
            """,
            (work_id, revision),
        ).fetchall()
        current_scope_rows = cls._learning_work_source_rows(
            connection, work_id, include_snapshot_length=False,
        )
        current_scope = {str(row["source_id"]): row for row in current_scope_rows}
        entry_ids = {str(row["source_id"]) for row in rows}
        snapshot_stale_ids = sorted(
            str(row["source_id"])
            for row in rows
            if row["current_snapshot_id"] != row["snapshot_id"]
        )
        missing_ids = sorted(set(current_scope) - entry_ids)
        out_of_scope_ids = sorted(entry_ids - set(current_scope))
        current_complete = (
            str(header["coverage_status"]) == "complete"
            and not missing_ids and not out_of_scope_ids and not snapshot_stale_ids
        )
        if str(header["coverage_status"]) == "partial":
            coordinate_status = "partial_manifest"
        elif missing_ids or out_of_scope_ids or snapshot_stale_ids:
            coordinate_status = "stale_manifest"
        else:
            coordinate_status = "verified"
        return {
            "work_id": str(header["work_id"]),
            "revision": int(header["revision"]),
            "manifest_hash": str(header["manifest_hash"]),
            "coverage_status": str(header["coverage_status"]),
            "entry_count": int(header["entry_count"]),
            "expected_source_count": int(header["expected_source_count"]),
            "created_at": str(header["created_at"]),
            "is_current_revision": int(header["current_revision"] or 0) == revision,
            "is_complete_for_current_scope": current_complete,
            "coordinate_status": coordinate_status,
            "missing_source_ids": missing_ids,
            "out_of_scope_source_ids": out_of_scope_ids,
            "stale_snapshot_source_ids": snapshot_stale_ids,
            "entries": [
                {
                    "narrative_ordinal": int(row["narrative_ordinal"]),
                    "source_id": str(row["source_id"]),
                    "snapshot_id": str(row["snapshot_id"]),
                    "content_hash": str(row["content_hash"]),
                    "chapter_title": str(row["chapter_title"]),
                    "chapter_label": row["chapter_label"],
                    "snapshot_is_current": row["current_snapshot_id"] == row["snapshot_id"],
                }
                for row in rows
            ],
        }

    def resolve_narrative_position(
        self,
        *,
        work_id: str,
        source_id: str,
        snapshot_id: str,
        offset: int,
        revision: int | None = None,
    ) -> dict[str, Any]:
        """Resolve a codepoint offset only against one exact source/snapshot manifest entry."""
        normalized_work_id = _normalize_story_coordinate_id(work_id, "work_id")
        normalized_source_id = _normalize_story_coordinate_id(source_id, "source_id")
        normalized_snapshot_id = _normalize_story_coordinate_id(snapshot_id, "snapshot_id")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative unicode-codepoint position")
        order = self.get_learning_chapter_order(normalized_work_id, revision=revision)
        if order is None:
            raise ValueError("learning work has no chapter-order manifest")
        entry = next(
            (
                row for row in order["entries"]
                if row["source_id"] == normalized_source_id
                and row["snapshot_id"] == normalized_snapshot_id
            ),
            None,
        )
        if entry is None:
            raise ValueError("source and snapshot are not pinned in this chapter-order revision")
        with self.database.connection() as connection:
            snapshot = connection.execute(
                """
                SELECT length(content) AS content_length, content_hash
                FROM source_snapshot WHERE id = ? AND source_id = ?
                """,
                (normalized_snapshot_id, normalized_source_id),
            ).fetchone()
        if snapshot is None or int(snapshot["content_length"]) < offset:
            raise ValueError("narrative offset is outside the pinned source snapshot")
        if str(snapshot["content_hash"]) != entry["content_hash"]:
            raise ValueError("pinned source snapshot hash differs from the manifest entry")
        return {
            "domain": "narrative_order",
            "work_id": normalized_work_id,
            "manifest_revision": order["revision"],
            "manifest_hash": order["manifest_hash"],
            "manifest_coordinate_status": order["coordinate_status"],
            "source_id": normalized_source_id,
            "snapshot_id": normalized_snapshot_id,
            "content_hash": entry["content_hash"],
            "chapter_title": entry["chapter_title"],
            "narrative_ordinal": entry["narrative_ordinal"],
            "offset": offset,
            "offset_unit": "unicode_codepoint",
            "coordinate_basis": "verified" if order["coordinate_status"] == "verified" else "unverified",
        }
