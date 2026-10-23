"""Pin the currently published graph candidate version independently of drafts."""

import json
import sqlite3

from .migration_types import Migration


def _install_published_graph_version_pointer(connection: sqlite3.Connection) -> None:
    connection.execute(
        "ALTER TABLE knowledge_item ADD COLUMN published_graph_version_id TEXT"
    )
    connection.execute("""
        CREATE TRIGGER knowledge_item_published_graph_version_owner
        BEFORE UPDATE OF published_graph_version_id ON knowledge_item
        WHEN NEW.published_graph_version_id IS NOT NULL AND (
            NEW.kind != 'knowledge_graph_candidate'
            OR NOT EXISTS (
                SELECT 1 FROM knowledge_version AS version
                WHERE version.id = NEW.published_graph_version_id
                  AND version.item_id = NEW.id
            )
        )
        BEGIN
            SELECT RAISE(ABORT, 'published graph version must belong to its candidate item');
        END
    """)
    connection.execute("""
        CREATE TRIGGER knowledge_item_published_graph_version_insert_owner
        BEFORE INSERT ON knowledge_item
        WHEN NEW.published_graph_version_id IS NOT NULL AND (
            NEW.kind != 'knowledge_graph_candidate'
            OR NOT EXISTS (
                SELECT 1 FROM knowledge_version AS version
                WHERE version.id = NEW.published_graph_version_id
                  AND version.item_id = NEW.id
            )
        )
        BEGIN
            SELECT RAISE(ABORT, 'published graph version must belong to its candidate item');
        END
    """)
    connection.execute("""
        CREATE TRIGGER knowledge_item_published_graph_version_invalidate
        AFTER UPDATE OF status ON knowledge_item
        WHEN NEW.status IN ('deleted', 'rejected', 'superseded')
          AND NEW.published_graph_version_id IS NOT NULL
        BEGIN
            UPDATE knowledge_item SET published_graph_version_id = NULL WHERE id = NEW.id;
        END
    """)

    # Capture the legacy head before any new append can make MAX(version) point
    # at an unreviewed candidate. This only migrates rows the old owner exposed.
    from fxi_novel.knowledge.repository_parse_publication import (
        published_graph_candidate_filter,
    )
    legacy_published = published_graph_candidate_filter(
        "item", require_version_pointer=False, allow_checkpoint=False,
        include_query_reviewed_claims=False,
    )
    connection.execute(f"""
        UPDATE knowledge_item AS item
        SET published_graph_version_id = (
            SELECT version.id FROM knowledge_version AS version
            WHERE version.item_id = item.id
            ORDER BY version.version DESC LIMIT 1
        )
        WHERE item.kind = 'knowledge_graph_candidate'
          AND item.status NOT IN ('deleted', 'rejected', 'superseded')
          AND EXISTS (SELECT 1 FROM knowledge_version WHERE item_id = item.id)
          AND {legacy_published}
    """)

    # Retry receipts retain the exact prior owner-selected version in their
    # immutable checkpoint. Restore that exact pin; never infer it from MAX.
    from fxi_novel.knowledge.parse_publication_checkpoint import published_checkpoint_sql
    checkpoint_gate = published_checkpoint_sql(
        "receipt", "checkpoint_item", "checkpoint_batch",
    )
    connection.execute(f"""
        UPDATE knowledge_item AS candidate
        SET published_graph_version_id = (
            SELECT checkpoint_version.id
            FROM knowledge_parse_receipt AS receipt
            JOIN knowledge_parse_batch_item AS checkpoint_item
              ON checkpoint_item.batch_id = receipt.parse_batch_id
             AND checkpoint_item.source_id = receipt.source_id
             AND checkpoint_item.snapshot_id = receipt.snapshot_id
            JOIN knowledge_parse_batch AS checkpoint_batch
              ON checkpoint_batch.id = checkpoint_item.batch_id
            JOIN json_each(
                json_extract(checkpoint_item.result_json, '$.publication_checkpoint'),
                '$.candidate_versions'
            ) AS pinned ON pinned.key = candidate.id AND pinned.type = 'text'
            JOIN knowledge_version AS checkpoint_version
              ON checkpoint_version.id = pinned.value
             AND checkpoint_version.item_id = candidate.id
            WHERE candidate.origin_parse_receipt_id = receipt.id
              AND candidate.space = receipt.space AND candidate.work_id IS receipt.work_id
              AND candidate.status NOT IN ('deleted', 'rejected', 'superseded')
              AND {checkpoint_gate}
            ORDER BY checkpoint_item.updated_at DESC
            LIMIT 1
        )
        WHERE candidate.kind = 'knowledge_graph_candidate'
          AND candidate.published_graph_version_id IS NULL
          AND candidate.status NOT IN ('deleted', 'rejected', 'superseded')
          AND EXISTS (
              SELECT 1 FROM knowledge_parse_receipt AS receipt
              JOIN knowledge_parse_batch_item AS checkpoint_item
                ON checkpoint_item.batch_id = receipt.parse_batch_id
               AND checkpoint_item.source_id = receipt.source_id
               AND checkpoint_item.snapshot_id = receipt.snapshot_id
              JOIN knowledge_parse_batch AS checkpoint_batch
                ON checkpoint_batch.id = checkpoint_item.batch_id
              JOIN json_each(
                  json_extract(checkpoint_item.result_json, '$.publication_checkpoint'),
                  '$.candidate_versions'
              ) AS pinned ON pinned.key = candidate.id AND pinned.type = 'text'
              WHERE candidate.origin_parse_receipt_id = receipt.id
                AND candidate.space = receipt.space AND candidate.work_id IS receipt.work_id
                AND {checkpoint_gate}
          )
    """)

    _restore_published_graph_indexes_for_pinned_versions(connection)


def _restore_published_graph_indexes_for_pinned_versions(
    connection: sqlite3.Connection,
) -> None:
    """Repair only candidates whose legacy public index was replaced by a draft."""
    rows = connection.execute("""
        SELECT item.id AS item_id,
               published.id AS published_version_id,
               published.content_json AS published_content_json,
               latest.id AS latest_version_id,
               latest.content_json AS latest_content_json
        FROM knowledge_item AS item
        JOIN knowledge_version AS published
          ON published.id = item.published_graph_version_id
         AND published.item_id = item.id
        JOIN knowledge_version AS latest
          ON latest.item_id = item.id
         AND latest.version = (
             SELECT MAX(newest.version) FROM knowledge_version AS newest
             WHERE newest.item_id = item.id
         )
        WHERE item.kind = 'knowledge_graph_candidate'
          AND item.status NOT IN ('deleted', 'rejected', 'superseded')
          AND published.id != latest.id
    """).fetchall()
    if not rows:
        return

    from fxi_novel.knowledge.repository_knowledge_graph_projection import (
        KnowledgeGraphProjectionRepositoryMixin,
    )
    for row in rows:
        item_id = str(row["item_id"])
        published_version_id = str(row["published_version_id"])
        KnowledgeGraphProjectionRepositoryMixin._index_knowledge_graph_candidate(
            connection, item_id, published_version_id,
            json.loads(str(row["published_content_json"])),
        )
        KnowledgeGraphProjectionRepositoryMixin._index_knowledge_graph_candidate(
            connection, item_id, str(row["latest_version_id"]),
            json.loads(str(row["latest_content_json"])),
        )


MIGRATIONS_PUBLISHED_GRAPH_VERSIONS = (
    Migration(version=83, sql="", callback=_install_published_graph_version_pointer),
)
