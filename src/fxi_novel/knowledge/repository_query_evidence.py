"""Bounded current-snapshot reads for execution-time evidence validation."""

import json
from collections.abc import Sequence


class KnowledgeQueryEvidenceRepositoryMixin:
    def get_current_query_source_snapshots(self, source_ids: Sequence[str]) -> dict[str, dict]:
        selected = list(dict.fromkeys(source_ids))
        if len(selected) > 2500:
            raise ValueError("current query evidence exceeds 2500 sources")
        if not selected:
            return {}
        # One read transaction pins all selected source snapshots. No history,
        # chunks, cards, parsing state or index maintenance is loaded here.
        with self.database.transaction(immediate=False) as connection:
            rows = connection.execute(
                """SELECT source.id AS source_id, source.space, source.work_id,
                          snapshot.id AS snapshot_id, snapshot.content_hash, snapshot.content
                   FROM json_each(?) AS selected
                   JOIN source ON source.id=selected.value
                   JOIN source_current_snapshot AS current ON current.source_id=source.id
                   JOIN source_snapshot AS snapshot ON snapshot.id=current.snapshot_id
                   WHERE source.deleted_at IS NULL
                     AND (source.work_id IS NULL OR EXISTS (
                         SELECT 1 FROM knowledge_project AS project
                         WHERE project.id=source.work_id AND project.space=source.space
                           AND project.status='active'
                     ))""",
                (json.dumps(selected),),
            ).fetchall()
        return {row["source_id"]: dict(row) for row in rows}
