"""Exact states captured by the source owner at operation boundaries."""

import sqlite3


def record_source_state(connection: sqlite3.Connection, source_id: str) -> None:
    # Capture only states actually observed here. Never infer old attributes
    # from a text snapshot or record intermediate steps of a multi-statement save.
    connection.execute(
        """INSERT INTO source_record_history (
            source_id, edit_revision, snapshot_id, title, source_uri, space,
            work_id, folder_id, deleted_at, document_metadata_json, recorded_at
        ) SELECT s.id, s.edit_revision, c.snapshot_id, s.title, s.source_uri,
                 s.space, s.work_id, s.folder_id, s.deleted_at,
                 s.document_metadata_json, strftime('%Y-%m-%dT%H:%M:%fZ','now')
          FROM source AS s
          LEFT JOIN source_current_snapshot AS c ON c.source_id = s.id
          WHERE s.id = ?
          ON CONFLICT(source_id, edit_revision) DO NOTHING""",
        (source_id,),
    )
