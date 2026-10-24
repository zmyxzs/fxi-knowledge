"""Stable learning-work scope resolution for graph candidate repository reads."""

from __future__ import annotations

import sqlite3


class KnowledgeGraphCandidateScopeMixin:
    """Resolve logical learning works to their exact physical source set."""

    def _registered_learning_graph_source_ids(
        self,
        connection: sqlite3.Connection,
        *,
        space: str,
        work_id: str | None,
        folder_id: str | None,
        source_ids: list[str] | None = None,
    ) -> list[str] | None:
        if space != "learning" or work_id is None:
            return None
        project = connection.execute(
            "SELECT 1 FROM knowledge_project WHERE id = ? AND space = 'learning' AND status = 'active'",
            (work_id,),
        ).fetchone()
        if project is None:
            return None
        if source_ids is None:
            whole_work = self.resolve_learning_work_scope_in_connection(
                connection, work_id,
            )
            selected_ids = set(whole_work["source_ids"])
        else:
            exact_work = self.resolve_learning_work_scope_in_connection(
                connection, work_id, source_ids=source_ids,
            )
            if exact_work["invalid_source_ids"]:
                raise ValueError("candidate source is outside the selected learning work")
            selected_ids = set(source_ids)
        if folder_id is not None:
            folder_scope = self.resolve_learning_work_scope_in_connection(
                connection, work_id, folder_ids=[folder_id],
            )
            if folder_scope["invalid_folder_ids"]:
                raise ValueError("folder must belong to the selected learning work")
            selected_ids.intersection_update(folder_scope["source_ids"])
        return sorted(selected_ids)
