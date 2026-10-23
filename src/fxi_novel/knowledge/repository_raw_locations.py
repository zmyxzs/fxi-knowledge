"""Read exact pinned source snapshots for narrative-order lookup."""

from __future__ import annotations

from typing import Any, Iterable


class KnowledgeRawLocationRepositoryMixin:
    """Read source text only after validating explicit membership and snapshot pins."""

    def read_raw_pinned_sources(
        self,
        *,
        work_id: str,
        source_pins: Iterable[dict[str, str]],
    ) -> list[dict[str, Any]]:
        if not isinstance(work_id, str) or not work_id.strip() or work_id != work_id.strip():
            raise ValueError("work_id must be a non-blank identifier")
        pins: list[tuple[str, str, str | None]] = []
        for raw in source_pins:
            if not isinstance(raw, dict):
                raise ValueError("source pins must be objects")
            source_id = raw.get("source_id")
            snapshot_id = raw.get("snapshot_id")
            content_hash = raw.get("content_hash")
            if not isinstance(source_id, str) or not source_id.strip():
                raise ValueError("each source pin requires source_id")
            if not isinstance(snapshot_id, str) or not snapshot_id.strip():
                raise ValueError("each source pin requires snapshot_id")
            if content_hash is not None and (
                not isinstance(content_hash, str) or len(content_hash) != 64
            ):
                raise ValueError("content_hash must be a 64-character digest")
            pins.append((source_id, snapshot_id, content_hash))
        if not pins or len(pins) > 10_000:
            raise ValueError("source_pins must contain between 1 and 10,000 pins")
        ids = [source_id for source_id, _, _ in pins]
        if len(ids) != len(set(ids)):
            raise ValueError("source_pins cannot repeat a source")

        with self.database.connection() as connection:
            self._ensure_project_exists(connection, "learning", work_id)
            scope_rows = self._learning_work_source_rows(connection, work_id)
            scope = {str(row["source_id"]): row for row in scope_rows}
            if not set(ids).issubset(scope):
                raise ValueError("source pins contain a source outside the learning work scope")

            results: list[dict[str, Any]] = []
            for start in range(0, len(pins), 400):
                batch = pins[start:start + 400]
                values = ", ".join("(?, ?, ?)" for _ in batch)
                parameters: list[Any] = []
                for source_id, snapshot_id, expected_hash in batch:
                    parameters.extend((source_id, snapshot_id, expected_hash))
                rows = connection.execute(
                    f"""
                    WITH requested(source_id, snapshot_id, expected_hash) AS (VALUES {values})
                    SELECT requested.source_id, requested.snapshot_id,
                           source.title AS chapter_title,
                           source.space, source.work_id AS direct_work_id,
                           snapshot.content_hash, snapshot.content
                    FROM requested
                    JOIN source ON source.id = requested.source_id
                    JOIN source_snapshot AS snapshot
                      ON snapshot.id = requested.snapshot_id
                     AND snapshot.source_id = requested.source_id
                    WHERE source.space = 'learning' AND source.deleted_at IS NULL
                      AND (requested.expected_hash IS NULL
                           OR requested.expected_hash = snapshot.content_hash)
                    """,
                    parameters,
                ).fetchall()
                by_id = {str(row["source_id"]): dict(row) for row in rows}
                for source_id, snapshot_id, expected_hash in batch:
                    row = by_id.get(source_id)
                    if row is None or str(row["snapshot_id"]) != snapshot_id:
                        raise ValueError("a source snapshot pin is unavailable or its hash changed")
                    if expected_hash is not None and row["content_hash"] != expected_hash:
                        raise ValueError("a source snapshot pin is unavailable or its hash changed")
                    row["work_id"] = work_id
                    results.append(row)
        return results


__all__ = ["KnowledgeRawLocationRepositoryMixin"]
