"""Bounded, revision-bound projections for verified knowledge queries."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any


def read_query_reuse_revision(connection) -> str:
    row = connection.execute("""
        SELECT revision, knowledge_item_revision, query_reuse_revision
        FROM embedding_read_cache_revision WHERE singleton = 1
    """).fetchone()
    if row is None or any(type(value) is not int or value < 0 for value in row):
        raise ValueError("query reuse revision is unavailable")
    return ":".join(str(value) for value in row)


class KnowledgeQueryReuseRepositoryMixin:
    def clear_query_result_cache(self) -> int:
        """Discard derived query results when retiring their output contract."""
        with self.database.transaction() as connection:
            return connection.execute("DELETE FROM knowledge_query_result_cache").rowcount

    def get_query_reuse_revision(self) -> str:
        with self.database.connection() as connection:
            return read_query_reuse_revision(connection)

    def read_query_result_cache(self, cache_key: str, kind: str) -> dict[str, Any] | None:
        with self.database.transaction(immediate=False) as connection:
            # One read transaction prevents a revision from being paired with a
            # cache row read after a concurrent data change.
            revision = read_query_reuse_revision(connection)
            row = connection.execute("""
                SELECT payload_json FROM knowledge_query_result_cache
                WHERE cache_key = ? AND kind = ? AND
                  (dataset_revision = ? OR (kind = 'plan' AND dataset_revision = 'plan'))
            """, (cache_key, kind, revision)).fetchone()
            if row is None:
                return None
            try:
                value = json.loads(row["payload_json"])
            except (ValueError, TypeError):
                return None
            return {"revision": revision, "payload": value}

    def save_query_result_cache(
        self, cache_key: str, kind: str, revision: str, payload: dict[str, Any],
    ) -> bool:
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(encoded) > 4_000_000:
            return False
        with self.database.transaction() as connection:
            if not (kind == "plan" and revision == "plan") and read_query_reuse_revision(connection) != revision:
                return False
            now = datetime.now(timezone.utc).isoformat()
            connection.execute("""
                INSERT INTO knowledge_query_result_cache
                    (cache_key, kind, dataset_revision, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    kind=excluded.kind, dataset_revision=excluded.dataset_revision,
                    payload_json=excluded.payload_json, created_at=excluded.created_at
            """, (cache_key, kind, revision, encoded, now))
            connection.execute("""
                DELETE FROM knowledge_query_result_cache WHERE cache_key IN (
                    SELECT cache_key FROM knowledge_query_result_cache
                    ORDER BY created_at DESC, cache_key LIMIT -1 OFFSET 2000
                )
            """)
            return True

    def read_query_navigation(
        self, scope_hash: str, subjects: list[str], *, limit: int = 16,
    ) -> list[dict[str, Any]]:
        subjects = list(dict.fromkeys(subjects))[:40]
        if not subjects:
            return []
        marks = ",".join("?" for _ in subjects)
        with self.database.connection() as connection:
            rows = connection.execute(f"""
                SELECT subject, attribute, evidence_json FROM knowledge_query_navigation
                WHERE scope_hash = ? AND subject IN ({marks})
                ORDER BY created_at DESC, id LIMIT ?
            """, (scope_hash, *subjects, min(max(limit, 1), 32))).fetchall()
        return [{"subject": row["subject"], "attribute": row["attribute"],
                 "evidence": json.loads(row["evidence_json"])} for row in rows]

    def save_query_navigation(self, entries: list[dict[str, Any]], revision: str) -> bool:
        with self.database.transaction() as connection:
            if read_query_reuse_revision(connection) != revision:
                return False
            now = datetime.now(timezone.utc).isoformat()
            for entry in entries[:256]:
                connection.execute("""
                    INSERT INTO knowledge_query_navigation
                        (id, scope_hash, subject, attribute, evidence_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET created_at=excluded.created_at
                """, (entry["id"], entry["scope_hash"], entry["subject"], entry["attribute"],
                      json.dumps(entry["evidence"], ensure_ascii=False, allow_nan=False), now))
            connection.execute("""
                DELETE FROM knowledge_query_navigation WHERE id IN (
                    SELECT id FROM knowledge_query_navigation
                    ORDER BY created_at DESC, id LIMIT -1 OFFSET 10000
                )
            """)
            return True
