"""Read-only parse acceptance lookup; never submits or resumes a task."""

from __future__ import annotations

from .repository_support import validate_scope, validate_knowledge_parse_client_batch_id


class KnowledgeParseLookupRepositoryMixin:
    def lookup_knowledge_parse_batch_client(
        self, *, space: str, work_id: str | None, client_batch_id: str,
    ) -> dict | None:
        validate_scope(space, work_id)
        validate_knowledge_parse_client_batch_id(client_batch_id)
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT id, client_batch_id, space, work_id, requested_work_id, "
                "requested_manifest_pin_json, folder_id, branch_id, status, total_sources, "
                "diagnostic, created_at, updated_at, completion_goal "
                "FROM knowledge_parse_batch "
                "WHERE client_batch_id = ? AND space = ? AND requested_work_id IS ?",
                (client_batch_id, space, work_id),
            ).fetchone()
            return self._parse_batch_header(row) if row is not None else None
