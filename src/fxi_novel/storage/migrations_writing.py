"""SQLite migration definitions for prose lifecycle schema versions 55 through 63."""

import json
import sqlite3
from .migration_types import Migration
from .migrations_material_indices import MIGRATIONS_MATERIAL_INDICES


_IDENTITY_REVIEW_NULL_SORT_KEY = 9223372036854775807


def _repair_embedding_projection_vector_format(connection: sqlite3.Connection) -> None:
    columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(embedding_projection)")
    }
    if not columns:
        raise sqlite3.OperationalError("embedding_projection table is missing")
    if "vector_format" not in columns:
        connection.execute(
            "ALTER TABLE embedding_projection "
            "ADD COLUMN vector_format TEXT NOT NULL DEFAULT 'f32le-v1'"
        )


def _create_graph_identity_review_projection(connection: sqlite3.Connection) -> None:
    """Repair the known v62 drift and rebuild the derived identity-review index."""
    _repair_embedding_projection_vector_format(connection)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS knowledge_graph_identity_review_index (
            candidate_item_id TEXT NOT NULL REFERENCES knowledge_item(id) ON DELETE CASCADE,
            candidate_version_id TEXT NOT NULL REFERENCES knowledge_version(id) ON DELETE CASCADE,
            space TEXT NOT NULL CHECK (space IN ('learning', 'writing')),
            work_id TEXT,
            source_id TEXT NOT NULL REFERENCES source(id) ON DELETE CASCADE,
            role TEXT NOT NULL CHECK (role IN (
                'subject', 'target', 'asserted_by', 'observed_by', 'holder', 'audience'
            )),
            audience_index INTEGER NOT NULL DEFAULT -1 CHECK (audience_index >= -1),
            source_chapter_order INTEGER NOT NULL DEFAULT 9223372036854775807,
            evidence_quote_start INTEGER NOT NULL DEFAULT 9223372036854775807,
            mention_name TEXT NOT NULL CHECK (length(trim(mention_name)) > 0),
            entity_ref TEXT,
            identity_group_id TEXT,
            decision_json TEXT,
            PRIMARY KEY (candidate_item_id, role, audience_index)
        )
        """
    )
    expected_columns = {
        "candidate_item_id", "candidate_version_id", "space", "work_id", "source_id",
        "role", "audience_index", "source_chapter_order", "evidence_quote_start",
        "mention_name", "entity_ref", "identity_group_id", "decision_json",
    }
    actual_columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(knowledge_graph_identity_review_index)")
    }
    if not expected_columns.issubset(actual_columns):
        raise sqlite3.OperationalError(
            "knowledge_graph_identity_review_index has an incompatible schema"
        )
    connection.execute(
        """CREATE INDEX IF NOT EXISTS knowledge_graph_identity_review_order_idx
           ON knowledge_graph_identity_review_index(
               space, work_id, source_chapter_order, evidence_quote_start,
               candidate_item_id, role, audience_index
           )"""
    )
    connection.execute(
        """CREATE INDEX IF NOT EXISTS knowledge_graph_identity_review_pending_idx
           ON knowledge_graph_identity_review_index(
               space, work_id, identity_group_id, source_id, candidate_item_id, role, audience_index
           )"""
    )
    # This is a rebuildable projection. Canonical decisions remain in knowledge_version.
    connection.execute("DELETE FROM knowledge_graph_identity_review_index")
    rows = connection.execute(
        """
        SELECT item.id AS item_id, item.space, item.work_id, receipt.source_id,
               version.id AS version_id, version.content_json
        FROM knowledge_item AS item
        JOIN knowledge_version AS version ON version.item_id = item.id
          AND version.version = (
              SELECT MAX(latest.version) FROM knowledge_version AS latest
              WHERE latest.item_id = item.id
          )
        JOIN knowledge_parse_receipt AS receipt ON receipt.id = item.origin_parse_receipt_id
        WHERE item.kind = 'knowledge_graph_candidate' AND item.status != 'deleted'
        ORDER BY item.id
        """
    )
    projection_rows: list[tuple[object, ...]] = []
    insert_sql = """INSERT INTO knowledge_graph_identity_review_index(
        candidate_item_id, candidate_version_id, space, work_id, source_id,
        role, audience_index, source_chapter_order, evidence_quote_start, mention_name,
        entity_ref, identity_group_id, decision_json
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
    for row in rows:
        try:
            content = json.loads(str(row["content_json"]))
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(content, dict) or content.get("candidate_role") != "fact":
            continue
        decisions = content.get("identity_review_decisions")
        if not isinstance(decisions, list):
            decisions = []

        associations: list[tuple[str, int, object, object, object]] = [
            ("subject", -1, content.get("entity_name") or content.get("subject"),
             content.get("entity_ref"), content.get("identity_group_id")),
            ("target", -1, content.get("target_entity_name"),
             content.get("target_entity_ref"), content.get("target_identity_group_id")),
            ("asserted_by", -1, content.get("asserted_by"),
             content.get("asserted_by_entity_ref"), content.get("asserted_by_identity_group_id")),
            ("observed_by", -1, content.get("observed_by"),
             content.get("observed_by_entity_ref"), content.get("observed_by_identity_group_id")),
            ("holder", -1, content.get("holder"),
             content.get("holder_entity_ref"), content.get("holder_identity_group_id")),
        ]
        audience = content.get("audience")
        audience_refs = content.get("audience_entity_refs")
        audience_groups = content.get("audience_identity_group_ids")
        if isinstance(audience, list):
            for index, name in enumerate(audience):
                associations.append((
                    "audience", index, name,
                    audience_refs[index] if isinstance(audience_refs, list) and index < len(audience_refs) else None,
                    audience_groups[index] if isinstance(audience_groups, list) and index < len(audience_groups) else None,
                ))
        for role, audience_index, raw_name, raw_ref, raw_group in associations:
            mention_name = str(raw_name or "").strip()
            if not mention_name:
                continue
            normalized_group = str(raw_group or "").strip() or None
            decision = next((
                value for value in reversed(decisions)
                if isinstance(value, dict)
                and value.get("role") == role
                and value.get("audience_index") == (audience_index if role == "audience" else None)
            ), None)
            if normalized_group is not None and decision is None:
                continue
            projection_rows.append((
                str(row["item_id"]), str(row["version_id"]), str(row["space"]),
                row["work_id"], str(row["source_id"]), role, audience_index,
                (content.get("source_chapter_order") if content.get("source_chapter_order") is not None
                 else _IDENTITY_REVIEW_NULL_SORT_KEY),
                (content.get("evidence_quote_start") if content.get("evidence_quote_start") is not None
                 else _IDENTITY_REVIEW_NULL_SORT_KEY),
                mention_name, (str(raw_ref).strip() or None) if raw_ref is not None else None,
                normalized_group,
                json.dumps(decision, ensure_ascii=False, separators=(",", ":"))
                if decision is not None else None,
            ))
            if len(projection_rows) >= 2048:
                connection.executemany(insert_sql, projection_rows)
                projection_rows.clear()
    if projection_rows:
        connection.executemany(insert_sql, projection_rows)

MIGRATIONS_WRITING_PROSE = (
    Migration(
        version=55,
        sql="""
        CREATE TABLE knowledge_graph_relation_reference (
            candidate_item_id TEXT NOT NULL
                REFERENCES knowledge_item(id) ON DELETE CASCADE,
            candidate_version_id TEXT NOT NULL
                REFERENCES knowledge_version(id) ON DELETE CASCADE,
            space TEXT NOT NULL CHECK (space IN ('learning','writing')),
            work_id TEXT,
            reference_kind TEXT NOT NULL CHECK (reference_kind IN ('identity_group','entity_ref')),
            reference_id TEXT NOT NULL CHECK (length(trim(reference_id)) > 0),
            relation_role TEXT NOT NULL CHECK (relation_role IN (
                'subject','target','asserted_by','observed_by','holder','audience',
                'entity_member'
            )),
            PRIMARY KEY(candidate_item_id, reference_kind, reference_id, relation_role),
            CHECK ((space='learning' AND work_id IS NULL)
                OR (space='writing' AND work_id IS NOT NULL)),
            FOREIGN KEY (space, work_id) REFERENCES knowledge_project(space,id) ON DELETE RESTRICT
        ) WITHOUT ROWID;
        CREATE INDEX knowledge_graph_relation_reference_lookup_idx
            ON knowledge_graph_relation_reference(
                space, work_id, reference_kind, reference_id, candidate_item_id
            );

        WITH current_candidates AS MATERIALIZED (
            SELECT i.id AS candidate_item_id, v.id AS candidate_version_id,
                   i.space, i.work_id, v.content_json,
                   json_extract(v.content_json, '$.candidate_role') AS candidate_role
            FROM knowledge_item AS i
            JOIN knowledge_version AS v ON v.item_id = i.id
              AND v.version = (
                  SELECT MAX(latest.version)
                  FROM knowledge_version AS latest
                  WHERE latest.item_id = i.id
              )
            WHERE i.kind = 'knowledge_graph_candidate'
              AND i.status != 'deleted'
        ), current_facts AS MATERIALIZED (
            SELECT * FROM current_candidates WHERE candidate_role = 'fact'
        )
        INSERT OR IGNORE INTO knowledge_graph_relation_reference(
            candidate_item_id, candidate_version_id, space, work_id,
            reference_kind, reference_id, relation_role
        )
        SELECT candidate_item_id, candidate_version_id, space, work_id,
               'identity_group', lower(trim(CAST(json_extract(content_json, '$.identity_group_id') AS TEXT))), 'entity_member'
        FROM current_candidates
        WHERE json_extract(content_json, '$.candidate_role') = 'entity'
          AND json_extract(content_json, '$.identity_group_id') IS NOT NULL
          AND length(trim(CAST(json_extract(content_json, '$.identity_group_id') AS TEXT))) > 0
        UNION ALL
        SELECT candidate_item_id, candidate_version_id, space, work_id,
               'entity_ref', lower(trim(CAST(json_extract(content_json, '$.entity_ref') AS TEXT))), 'entity_member'
        FROM current_candidates
        WHERE json_extract(content_json, '$.candidate_role') = 'entity'
          AND json_extract(content_json, '$.entity_ref') IS NOT NULL
          AND length(trim(CAST(json_extract(content_json, '$.entity_ref') AS TEXT))) > 0
        UNION ALL
        SELECT candidate_item_id, candidate_version_id, space, work_id,
               'identity_group', lower(trim(CAST(json_extract(content_json, '$.identity_group_id') AS TEXT))), 'subject'
        FROM current_facts
        WHERE json_extract(content_json, '$.identity_group_id') IS NOT NULL
          AND length(trim(CAST(json_extract(content_json, '$.identity_group_id') AS TEXT))) > 0
        UNION ALL
        SELECT candidate_item_id, candidate_version_id, space, work_id,
               'identity_group', lower(trim(CAST(json_extract(content_json, '$.target_identity_group_id') AS TEXT))), 'target'
        FROM current_facts
        WHERE json_extract(content_json, '$.target_identity_group_id') IS NOT NULL
          AND length(trim(CAST(json_extract(content_json, '$.target_identity_group_id') AS TEXT))) > 0
        UNION ALL
        SELECT candidate_item_id, candidate_version_id, space, work_id,
               'identity_group', lower(trim(CAST(json_extract(content_json, '$.asserted_by_identity_group_id') AS TEXT))), 'asserted_by'
        FROM current_facts
        WHERE json_extract(content_json, '$.asserted_by_identity_group_id') IS NOT NULL
          AND length(trim(CAST(json_extract(content_json, '$.asserted_by_identity_group_id') AS TEXT))) > 0
        UNION ALL
        SELECT candidate_item_id, candidate_version_id, space, work_id,
               'identity_group', lower(trim(CAST(json_extract(content_json, '$.observed_by_identity_group_id') AS TEXT))), 'observed_by'
        FROM current_facts
        WHERE json_extract(content_json, '$.observed_by_identity_group_id') IS NOT NULL
          AND length(trim(CAST(json_extract(content_json, '$.observed_by_identity_group_id') AS TEXT))) > 0
        UNION ALL
        SELECT candidate_item_id, candidate_version_id, space, work_id,
               'identity_group', lower(trim(CAST(json_extract(content_json, '$.holder_identity_group_id') AS TEXT))), 'holder'
        FROM current_facts
        WHERE json_extract(content_json, '$.holder_identity_group_id') IS NOT NULL
          AND length(trim(CAST(json_extract(content_json, '$.holder_identity_group_id') AS TEXT))) > 0
        UNION ALL
        SELECT c.candidate_item_id, c.candidate_version_id, c.space, c.work_id,
               'identity_group', lower(trim(CAST(a.value AS TEXT))), 'audience'
        FROM current_facts AS c
        JOIN json_each(c.content_json, '$.audience_identity_group_ids') AS a
        WHERE a.value IS NOT NULL AND length(trim(CAST(a.value AS TEXT))) > 0
        UNION ALL
        SELECT candidate_item_id, candidate_version_id, space, work_id,
               'entity_ref', lower(trim(CAST(json_extract(content_json, '$.entity_ref') AS TEXT))), 'subject'
        FROM current_facts
        WHERE json_extract(content_json, '$.entity_ref') IS NOT NULL
          AND length(trim(CAST(json_extract(content_json, '$.entity_ref') AS TEXT))) > 0
        UNION ALL
        SELECT candidate_item_id, candidate_version_id, space, work_id,
               'entity_ref', lower(trim(CAST(json_extract(content_json, '$.target_entity_ref') AS TEXT))), 'target'
        FROM current_facts
        WHERE json_extract(content_json, '$.target_entity_ref') IS NOT NULL
          AND length(trim(CAST(json_extract(content_json, '$.target_entity_ref') AS TEXT))) > 0
        UNION ALL
        SELECT candidate_item_id, candidate_version_id, space, work_id,
               'entity_ref', lower(trim(CAST(json_extract(content_json, '$.asserted_by_entity_ref') AS TEXT))), 'asserted_by'
        FROM current_facts
        WHERE json_extract(content_json, '$.asserted_by_entity_ref') IS NOT NULL
          AND length(trim(CAST(json_extract(content_json, '$.asserted_by_entity_ref') AS TEXT))) > 0
        UNION ALL
        SELECT candidate_item_id, candidate_version_id, space, work_id,
               'entity_ref', lower(trim(CAST(json_extract(content_json, '$.observed_by_entity_ref') AS TEXT))), 'observed_by'
        FROM current_facts
        WHERE json_extract(content_json, '$.observed_by_entity_ref') IS NOT NULL
          AND length(trim(CAST(json_extract(content_json, '$.observed_by_entity_ref') AS TEXT))) > 0
        UNION ALL
        SELECT candidate_item_id, candidate_version_id, space, work_id,
               'entity_ref', lower(trim(CAST(json_extract(content_json, '$.holder_entity_ref') AS TEXT))), 'holder'
        FROM current_facts
        WHERE json_extract(content_json, '$.holder_entity_ref') IS NOT NULL
          AND length(trim(CAST(json_extract(content_json, '$.holder_entity_ref') AS TEXT))) > 0
        UNION ALL
        SELECT c.candidate_item_id, c.candidate_version_id, c.space, c.work_id,
               'entity_ref', lower(trim(CAST(a.value AS TEXT))), 'audience'
        FROM current_facts AS c
        JOIN json_each(c.content_json, '$.audience_entity_refs') AS a
        WHERE a.value IS NOT NULL AND length(trim(CAST(a.value AS TEXT))) > 0;
        """,
    ),
    Migration(
        version=56,
        sql="""
        WITH current_entities AS MATERIALIZED (
            SELECT i.id AS candidate_item_id, v.id AS candidate_version_id,
                   i.space, i.work_id, v.content_json
            FROM knowledge_item AS i
            JOIN knowledge_version AS v ON v.item_id = i.id
              AND v.version = (
                  SELECT MAX(latest.version)
                  FROM knowledge_version AS latest
                  WHERE latest.item_id = i.id
              )
            WHERE i.kind = 'knowledge_graph_candidate'
              AND i.status != 'deleted'
              AND json_extract(v.content_json, '$.candidate_role') = 'entity'
        )
        INSERT OR IGNORE INTO knowledge_graph_relation_reference(
            candidate_item_id, candidate_version_id, space, work_id,
            reference_kind, reference_id, relation_role
        )
        SELECT candidate_item_id, candidate_version_id, space, work_id,
               'identity_group', lower(trim(CAST(json_extract(content_json, '$.identity_group_id') AS TEXT))), 'entity_member'
        FROM current_entities
        WHERE json_extract(content_json, '$.identity_group_id') IS NOT NULL
          AND length(trim(CAST(json_extract(content_json, '$.identity_group_id') AS TEXT))) > 0
        UNION ALL
        SELECT candidate_item_id, candidate_version_id, space, work_id,
               'entity_ref', lower(trim(CAST(json_extract(content_json, '$.entity_ref') AS TEXT))), 'entity_member'
        FROM current_entities
        WHERE json_extract(content_json, '$.entity_ref') IS NOT NULL
          AND length(trim(CAST(json_extract(content_json, '$.entity_ref') AS TEXT))) > 0;
        """,
    ),
    Migration(
        version=57,
        sql="""
        ALTER TABLE assistant_run
            ADD COLUMN superseded_message_refs_json TEXT NOT NULL DEFAULT '[]'
            CHECK (json_valid(superseded_message_refs_json));
        """,
    ),
    Migration(
        version=58,
        sql="""
        ALTER TABLE assistant_run
            ADD COLUMN branch_retraction_status TEXT NOT NULL DEFAULT 'not_required'
            CHECK (branch_retraction_status IN ('not_required', 'pending', 'failed', 'completed'));
        """,
    ),
    Migration(
        version=59,
        sql="""
        CREATE TABLE knowledge_card_folder_summary_dirty (
            scope_key TEXT NOT NULL,
            folder_id TEXT NOT NULL REFERENCES knowledge_folder(id) ON DELETE CASCADE,
            PRIMARY KEY(scope_key, folder_id)
        ) WITHOUT ROWID;

        INSERT OR IGNORE INTO knowledge_card_folder_summary_dirty(scope_key, folder_id)
        SELECT folder.space || ':' || COALESCE(folder.work_id, ''), folder.id
        FROM knowledge_folder AS folder;
        UPDATE knowledge_card_folder_summary_state SET index_version = 1 WHERE id = 1;

        DROP TRIGGER IF EXISTS knowledge_card_folder_summary_candidate_insert;
        DROP TRIGGER IF EXISTS knowledge_card_folder_summary_candidate_update;
        DROP TRIGGER IF EXISTS knowledge_card_folder_summary_candidate_delete;
        DROP TRIGGER IF EXISTS knowledge_card_folder_summary_source_insert;
        DROP TRIGGER IF EXISTS knowledge_card_folder_summary_source_scope_update;
        DROP TRIGGER IF EXISTS knowledge_card_folder_summary_source_delete;
        DROP TRIGGER IF EXISTS knowledge_card_folder_summary_folder_insert;
        DROP TRIGGER IF EXISTS knowledge_card_folder_summary_folder_path_update;
        DROP TRIGGER IF EXISTS knowledge_card_folder_summary_folder_delete;

        CREATE TRIGGER knowledge_card_folder_summary_candidate_insert
        AFTER INSERT ON knowledge_card_candidate
        BEGIN
            INSERT OR IGNORE INTO knowledge_card_folder_summary_dirty(scope_key, folder_id)
            SELECT NEW.scope_key, ancestor.id
            FROM knowledge_card_candidate_evidence AS link
            JOIN source AS live_source ON live_source.id = link.source_id
            JOIN knowledge_folder AS source_folder
              ON source_folder.id = live_source.folder_id
             AND source_folder.space = live_source.space
             AND source_folder.work_id IS live_source.work_id
            JOIN knowledge_folder AS ancestor
              ON ancestor.space = source_folder.space
             AND ancestor.work_id IS source_folder.work_id
             AND (
                 source_folder.path = ancestor.path COLLATE NOCASE
                 OR substr(source_folder.path, 1, length(ancestor.path) + 1)
                        = ancestor.path || '/' COLLATE NOCASE
             )
            WHERE link.candidate_item_id = NEW.candidate_item_id
              AND link.candidate_version_id = NEW.candidate_version_id
              AND live_source.deleted_at IS NULL
              AND NEW.scope_key = live_source.space || ':' || COALESCE(live_source.work_id, '');
        END;

        CREATE TRIGGER knowledge_card_folder_summary_candidate_update
        AFTER UPDATE ON knowledge_card_candidate
        BEGIN
            INSERT OR IGNORE INTO knowledge_card_folder_summary_dirty(scope_key, folder_id)
            SELECT OLD.scope_key, ancestor.id
            FROM knowledge_card_candidate_evidence AS link
            JOIN source AS live_source ON live_source.id = link.source_id
            JOIN knowledge_folder AS source_folder
              ON source_folder.id = live_source.folder_id
             AND source_folder.space = live_source.space
             AND source_folder.work_id IS live_source.work_id
            JOIN knowledge_folder AS ancestor
              ON ancestor.space = source_folder.space
             AND ancestor.work_id IS source_folder.work_id
             AND (
                 source_folder.path = ancestor.path COLLATE NOCASE
                 OR substr(source_folder.path, 1, length(ancestor.path) + 1)
                        = ancestor.path || '/' COLLATE NOCASE
             )
            WHERE link.candidate_item_id = OLD.candidate_item_id
              AND link.candidate_version_id = OLD.candidate_version_id
              AND live_source.deleted_at IS NULL
              AND OLD.scope_key = live_source.space || ':' || COALESCE(live_source.work_id, '');

            INSERT OR IGNORE INTO knowledge_card_folder_summary_dirty(scope_key, folder_id)
            SELECT NEW.scope_key, ancestor.id
            FROM knowledge_card_candidate_evidence AS link
            JOIN source AS live_source ON live_source.id = link.source_id
            JOIN knowledge_folder AS source_folder
              ON source_folder.id = live_source.folder_id
             AND source_folder.space = live_source.space
             AND source_folder.work_id IS live_source.work_id
            JOIN knowledge_folder AS ancestor
              ON ancestor.space = source_folder.space
             AND ancestor.work_id IS source_folder.work_id
             AND (
                 source_folder.path = ancestor.path COLLATE NOCASE
                 OR substr(source_folder.path, 1, length(ancestor.path) + 1)
                        = ancestor.path || '/' COLLATE NOCASE
             )
            WHERE link.candidate_item_id = NEW.candidate_item_id
              AND link.candidate_version_id = NEW.candidate_version_id
              AND live_source.deleted_at IS NULL
              AND NEW.scope_key = live_source.space || ':' || COALESCE(live_source.work_id, '');
        END;

        CREATE TRIGGER knowledge_card_folder_summary_candidate_delete
        BEFORE DELETE ON knowledge_card_candidate
        BEGIN
            INSERT OR IGNORE INTO knowledge_card_folder_summary_dirty(scope_key, folder_id)
            SELECT OLD.scope_key, ancestor.id
            FROM knowledge_card_candidate_evidence AS link
            JOIN source AS live_source ON live_source.id = link.source_id
            JOIN knowledge_folder AS source_folder
              ON source_folder.id = live_source.folder_id
             AND source_folder.space = live_source.space
             AND source_folder.work_id IS live_source.work_id
            JOIN knowledge_folder AS ancestor
              ON ancestor.space = source_folder.space
             AND ancestor.work_id IS source_folder.work_id
             AND (
                 source_folder.path = ancestor.path COLLATE NOCASE
                 OR substr(source_folder.path, 1, length(ancestor.path) + 1)
                        = ancestor.path || '/' COLLATE NOCASE
             )
            WHERE link.candidate_item_id = OLD.candidate_item_id
              AND link.candidate_version_id = OLD.candidate_version_id
              AND live_source.deleted_at IS NULL
              AND OLD.scope_key = live_source.space || ':' || COALESCE(live_source.work_id, '');
        END;

        CREATE TRIGGER knowledge_card_folder_summary_candidate_evidence_insert
        AFTER INSERT ON knowledge_card_candidate_evidence
        BEGIN
            INSERT OR IGNORE INTO knowledge_card_folder_summary_dirty(scope_key, folder_id)
            SELECT candidate.scope_key, ancestor.id
            FROM knowledge_card_candidate AS candidate
            JOIN source AS live_source ON live_source.id = NEW.source_id
            JOIN knowledge_folder AS source_folder
              ON source_folder.id = live_source.folder_id
             AND source_folder.space = live_source.space
             AND source_folder.work_id IS live_source.work_id
            JOIN knowledge_folder AS ancestor
              ON ancestor.space = source_folder.space
             AND ancestor.work_id IS source_folder.work_id
             AND (
                 source_folder.path = ancestor.path COLLATE NOCASE
                 OR substr(source_folder.path, 1, length(ancestor.path) + 1)
                        = ancestor.path || '/' COLLATE NOCASE
             )
            WHERE candidate.candidate_item_id = NEW.candidate_item_id
              AND candidate.candidate_version_id = NEW.candidate_version_id
              AND live_source.deleted_at IS NULL
              AND candidate.scope_key = live_source.space || ':' || COALESCE(live_source.work_id, '');
        END;

        CREATE TRIGGER knowledge_card_folder_summary_candidate_evidence_update
        AFTER UPDATE ON knowledge_card_candidate_evidence
        BEGIN
            INSERT OR IGNORE INTO knowledge_card_folder_summary_dirty(scope_key, folder_id)
            SELECT candidate.scope_key, ancestor.id
            FROM knowledge_card_candidate AS candidate
            JOIN source AS live_source ON live_source.id = OLD.source_id
            JOIN knowledge_folder AS source_folder
              ON source_folder.id = live_source.folder_id
             AND source_folder.space = live_source.space
             AND source_folder.work_id IS live_source.work_id
            JOIN knowledge_folder AS ancestor
              ON ancestor.space = source_folder.space
             AND ancestor.work_id IS source_folder.work_id
             AND (
                 source_folder.path = ancestor.path COLLATE NOCASE
                 OR substr(source_folder.path, 1, length(ancestor.path) + 1)
                        = ancestor.path || '/' COLLATE NOCASE
             )
            WHERE candidate.candidate_item_id = OLD.candidate_item_id
              AND live_source.deleted_at IS NULL
              AND candidate.scope_key = live_source.space || ':' || COALESCE(live_source.work_id, '');

            INSERT OR IGNORE INTO knowledge_card_folder_summary_dirty(scope_key, folder_id)
            SELECT candidate.scope_key, ancestor.id
            FROM knowledge_card_candidate AS candidate
            JOIN source AS live_source ON live_source.id = NEW.source_id
            JOIN knowledge_folder AS source_folder
              ON source_folder.id = live_source.folder_id
             AND source_folder.space = live_source.space
             AND source_folder.work_id IS live_source.work_id
            JOIN knowledge_folder AS ancestor
              ON ancestor.space = source_folder.space
             AND ancestor.work_id IS source_folder.work_id
             AND (
                 source_folder.path = ancestor.path COLLATE NOCASE
                 OR substr(source_folder.path, 1, length(ancestor.path) + 1)
                        = ancestor.path || '/' COLLATE NOCASE
             )
            WHERE candidate.candidate_item_id = NEW.candidate_item_id
              AND candidate.candidate_version_id = NEW.candidate_version_id
              AND live_source.deleted_at IS NULL
              AND candidate.scope_key = live_source.space || ':' || COALESCE(live_source.work_id, '');
        END;

        CREATE TRIGGER knowledge_card_folder_summary_candidate_evidence_delete
        BEFORE DELETE ON knowledge_card_candidate_evidence
        BEGIN
            INSERT OR IGNORE INTO knowledge_card_folder_summary_dirty(scope_key, folder_id)
            SELECT candidate.scope_key, ancestor.id
            FROM knowledge_card_candidate AS candidate
            JOIN source AS live_source ON live_source.id = OLD.source_id
            JOIN knowledge_folder AS source_folder
              ON source_folder.id = live_source.folder_id
             AND source_folder.space = live_source.space
             AND source_folder.work_id IS live_source.work_id
            JOIN knowledge_folder AS ancestor
              ON ancestor.space = source_folder.space
             AND ancestor.work_id IS source_folder.work_id
             AND (
                 source_folder.path = ancestor.path COLLATE NOCASE
                 OR substr(source_folder.path, 1, length(ancestor.path) + 1)
                        = ancestor.path || '/' COLLATE NOCASE
             )
            WHERE candidate.candidate_item_id = OLD.candidate_item_id
              AND live_source.deleted_at IS NULL
              AND candidate.scope_key = live_source.space || ':' || COALESCE(live_source.work_id, '');
        END;

        CREATE TRIGGER knowledge_card_folder_summary_source_insert
        AFTER INSERT ON source
        WHEN NEW.folder_id IS NOT NULL AND NEW.deleted_at IS NULL
        BEGIN
            INSERT OR IGNORE INTO knowledge_card_folder_summary_dirty(scope_key, folder_id)
            SELECT NEW.space || ':' || COALESCE(NEW.work_id, ''), ancestor.id
            FROM knowledge_folder AS source_folder
            JOIN knowledge_folder AS ancestor
              ON ancestor.space = source_folder.space
             AND ancestor.work_id IS source_folder.work_id
             AND (
                 source_folder.path = ancestor.path COLLATE NOCASE
                 OR substr(source_folder.path, 1, length(ancestor.path) + 1)
                        = ancestor.path || '/' COLLATE NOCASE
             )
            WHERE source_folder.id = NEW.folder_id
              AND source_folder.space = NEW.space
              AND source_folder.work_id IS NEW.work_id;
        END;

        CREATE TRIGGER knowledge_card_folder_summary_source_scope_update
        AFTER UPDATE OF space, work_id, folder_id, deleted_at ON source
        WHEN OLD.space IS NOT NEW.space OR OLD.work_id IS NOT NEW.work_id
          OR OLD.folder_id IS NOT NEW.folder_id OR OLD.deleted_at IS NOT NEW.deleted_at
        BEGIN
            INSERT OR IGNORE INTO knowledge_card_folder_summary_dirty(scope_key, folder_id)
            SELECT OLD.space || ':' || COALESCE(OLD.work_id, ''), ancestor.id
            FROM knowledge_folder AS source_folder
            JOIN knowledge_folder AS ancestor
              ON ancestor.space = source_folder.space
             AND ancestor.work_id IS source_folder.work_id
             AND (
                 source_folder.path = ancestor.path COLLATE NOCASE
                 OR substr(source_folder.path, 1, length(ancestor.path) + 1)
                        = ancestor.path || '/' COLLATE NOCASE
             )
            WHERE OLD.folder_id IS NOT NULL
              AND source_folder.id = OLD.folder_id
              AND source_folder.space = OLD.space
              AND source_folder.work_id IS OLD.work_id;

            INSERT OR IGNORE INTO knowledge_card_folder_summary_dirty(scope_key, folder_id)
            SELECT NEW.space || ':' || COALESCE(NEW.work_id, ''), ancestor.id
            FROM knowledge_folder AS source_folder
            JOIN knowledge_folder AS ancestor
              ON ancestor.space = source_folder.space
             AND ancestor.work_id IS source_folder.work_id
             AND (
                 source_folder.path = ancestor.path COLLATE NOCASE
                 OR substr(source_folder.path, 1, length(ancestor.path) + 1)
                        = ancestor.path || '/' COLLATE NOCASE
             )
            WHERE NEW.folder_id IS NOT NULL
              AND NEW.deleted_at IS NULL
              AND source_folder.id = NEW.folder_id
              AND source_folder.space = NEW.space
              AND source_folder.work_id IS NEW.work_id;
        END;

        CREATE TRIGGER knowledge_card_folder_summary_source_delete
        BEFORE DELETE ON source
        WHEN OLD.folder_id IS NOT NULL
        BEGIN
            INSERT OR IGNORE INTO knowledge_card_folder_summary_dirty(scope_key, folder_id)
            SELECT OLD.space || ':' || COALESCE(OLD.work_id, ''), ancestor.id
            FROM knowledge_folder AS source_folder
            JOIN knowledge_folder AS ancestor
              ON ancestor.space = source_folder.space
             AND ancestor.work_id IS source_folder.work_id
             AND (
                 source_folder.path = ancestor.path COLLATE NOCASE
                 OR substr(source_folder.path, 1, length(ancestor.path) + 1)
                        = ancestor.path || '/' COLLATE NOCASE
             )
            WHERE source_folder.id = OLD.folder_id
              AND source_folder.space = OLD.space
              AND source_folder.work_id IS OLD.work_id;
        END;

        CREATE TRIGGER knowledge_card_folder_summary_folder_insert
        AFTER INSERT ON knowledge_folder
        BEGIN
            INSERT OR IGNORE INTO knowledge_card_folder_summary_dirty(scope_key, folder_id)
            SELECT NEW.space || ':' || COALESCE(NEW.work_id, ''), ancestor.id
            FROM knowledge_folder AS ancestor
            WHERE ancestor.space = NEW.space
              AND ancestor.work_id IS NEW.work_id
              AND (
                  NEW.path = ancestor.path COLLATE NOCASE
                  OR substr(NEW.path, 1, length(ancestor.path) + 1)
                         = ancestor.path || '/' COLLATE NOCASE
              );
        END;

        CREATE TRIGGER knowledge_card_folder_summary_folder_path_update
        AFTER UPDATE OF space, work_id, path ON knowledge_folder
        WHEN OLD.space IS NOT NEW.space OR OLD.work_id IS NOT NEW.work_id
          OR OLD.path IS NOT NEW.path
        BEGIN
            INSERT OR IGNORE INTO knowledge_card_folder_summary_dirty(scope_key, folder_id)
            SELECT OLD.space || ':' || COALESCE(OLD.work_id, ''), ancestor.id
            FROM knowledge_folder AS ancestor
            WHERE ancestor.space = OLD.space
              AND ancestor.work_id IS OLD.work_id
              AND ancestor.id != OLD.id
              AND (
                  OLD.path = ancestor.path COLLATE NOCASE
                  OR substr(OLD.path, 1, length(ancestor.path) + 1)
                         = ancestor.path || '/' COLLATE NOCASE
              );

            DELETE FROM knowledge_card_folder_summary
            WHERE folder_id = OLD.id
              AND scope_key = OLD.space || ':' || COALESCE(OLD.work_id, '')
              AND (OLD.space IS NOT NEW.space OR OLD.work_id IS NOT NEW.work_id);

            INSERT OR IGNORE INTO knowledge_card_folder_summary_dirty(scope_key, folder_id)
            SELECT NEW.space || ':' || COALESCE(NEW.work_id, ''), ancestor.id
            FROM knowledge_folder AS ancestor
            WHERE ancestor.space = NEW.space
              AND ancestor.work_id IS NEW.work_id
              AND (
                  NEW.path = ancestor.path COLLATE NOCASE
                  OR substr(NEW.path, 1, length(ancestor.path) + 1)
                         = ancestor.path || '/' COLLATE NOCASE
              );
        END;

        CREATE TRIGGER knowledge_card_folder_summary_folder_delete
        BEFORE DELETE ON knowledge_folder
        BEGIN
            INSERT OR IGNORE INTO knowledge_card_folder_summary_dirty(scope_key, folder_id)
            SELECT OLD.space || ':' || COALESCE(OLD.work_id, ''), ancestor.id
            FROM knowledge_folder AS ancestor
            WHERE ancestor.space = OLD.space
              AND ancestor.work_id IS OLD.work_id
              AND ancestor.id != OLD.id
              AND (
                  OLD.path = ancestor.path COLLATE NOCASE
                  OR substr(OLD.path, 1, length(ancestor.path) + 1)
                         = ancestor.path || '/' COLLATE NOCASE
              );
        END;
        """,
    ),
    Migration(
        version=60,
        sql="""
        ALTER TABLE chapter_outline_state
            ADD COLUMN lifecycle_status TEXT NOT NULL DEFAULT 'active'
            CHECK (lifecycle_status IN ('active', 'branch_retracted'));

        CREATE TABLE creative_plan_section_state (
            work_id TEXT NOT NULL CHECK (length(trim(work_id)) BETWEEN 1 AND 200),
            section_id TEXT NOT NULL CHECK (section_id IN (
                'premise', 'protagonist_goal', 'central_conflict', 'stakes',
                'opening', 'story_arc', 'turning_points', 'open_questions'
            )),
            folder_id TEXT REFERENCES knowledge_folder(id) ON DELETE SET NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (work_id, section_id)
        );
        CREATE INDEX creative_plan_section_state_location_idx
            ON creative_plan_section_state(work_id, folder_id, section_id);

        WITH section_ids(section_id) AS (VALUES
            ('premise'),
            ('protagonist_goal'),
            ('central_conflict'),
            ('stakes'),
            ('opening'),
            ('story_arc'),
            ('turning_points'),
            ('open_questions')
        )
        INSERT OR IGNORE INTO creative_plan_section_state(
            work_id, section_id, folder_id, updated_at
        )
        SELECT
            project.id,
            section_ids.section_id,
            COALESCE(
                (
                    SELECT folder.id
                    FROM creative_plan_state AS legacy_plan
                    JOIN knowledge_folder AS folder
                      ON folder.id = legacy_plan.folder_id
                     AND folder.space = 'writing'
                     AND folder.work_id = project.id
                    WHERE legacy_plan.work_id = project.id
                ),
                (
                    SELECT folder.id
                    FROM knowledge_folder AS folder
                    WHERE folder.space = 'writing'
                      AND folder.work_id = project.id
                      AND folder.path = '规划' COLLATE NOCASE
                    LIMIT 1
                )
            ),
            strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        FROM knowledge_project AS project
        CROSS JOIN section_ids
        WHERE project.space = 'writing' AND project.status = 'active';

        ALTER TABLE assistant_run_event RENAME TO assistant_run_event_v59;
        DROP INDEX IF EXISTS assistant_run_event_parent_idx;

        CREATE TABLE assistant_run_event (
            run_id TEXT NOT NULL REFERENCES assistant_run(id) ON DELETE CASCADE,
            seq INTEGER NOT NULL CHECK (seq > 0),
            event_key TEXT NOT NULL CHECK (length(trim(event_key)) > 0),
            event_hash TEXT NOT NULL,
            event_type TEXT NOT NULL CHECK (event_type IN (
                'accepted', 'activity.started', 'activity.completed',
                'context.compacted', 'answer.delta', 'answer.committed',
                'usage.updated', 'model.receipt', 'run.completed', 'run.failed',
                'run.incomplete', 'run.cancelled', 'run.interrupted',
                'prose.draft_saved'
            )),
            occurred_at TEXT NOT NULL,
            parent_run_id TEXT REFERENCES assistant_run(id) ON DELETE SET NULL,
            data_json TEXT NOT NULL,
            PRIMARY KEY (run_id, seq),
            UNIQUE (run_id, event_key)
        );

        INSERT INTO assistant_run_event(
            run_id, seq, event_key, event_hash, event_type, occurred_at,
            parent_run_id, data_json
        )
        SELECT
            run_id, seq, event_key, event_hash, event_type, occurred_at,
            parent_run_id, data_json
        FROM assistant_run_event_v59;

        DROP TABLE assistant_run_event_v59;
        CREATE INDEX assistant_run_event_parent_idx
            ON assistant_run_event(parent_run_id, run_id, seq);
        """,
    ),
    Migration(
        version=61,
        sql="""
        CREATE TABLE IF NOT EXISTS embedding_projection_backfill (
            route_fingerprint TEXT PRIMARY KEY,
            cursor_target_id TEXT NOT NULL DEFAULT '',
            complete INTEGER NOT NULL DEFAULT 0 CHECK (complete IN (0, 1)),
            updated_at TEXT NOT NULL
        );
        """,
    ),
    Migration(
        version=62,
        sql="",
        callback=_repair_embedding_projection_vector_format,
    ),
    Migration(
        version=63,
        sql="",
        callback=_create_graph_identity_review_projection,
    ),
)

MIGRATIONS_WRITING = (*MIGRATIONS_MATERIAL_INDICES, *MIGRATIONS_WRITING_PROSE)

