"""Permit immutable graph-claim rows to be removed only by a scoped purge owner."""

import sqlite3
import re

from .migration_types import Migration


def _rebuild_reviewed_publication_scope(connection: sqlite3.Connection) -> None:
    """Widen the proof table while preserving its exact historical receipts."""
    table = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' "
        "AND name = 'knowledge_reviewed_claim_publication'"
    ).fetchone()
    if table is None or not isinstance(table["sql"], str):
        raise sqlite3.OperationalError("reviewed claim publication table is missing")
    table_sql = str(table["sql"])
    scope_check = re.compile(
        r"CHECK\s*\(\s*\(\s*space\s*=\s*'learning'\s+AND\s+work_id\s+IS\s+NULL\s*\)"
        r"\s+OR\s+\(\s*space\s*=\s*'writing'\s+AND\s+work_id\s+IS\s+NOT\s+NULL\s*\)\s*\)",
        re.IGNORECASE,
    )
    widened_sql, replacements = scope_check.subn(
        "CHECK ((space = 'learning') OR (space = 'writing' AND work_id IS NOT NULL))",
        table_sql,
        count=1,
    )
    if not replacements:
        compact = " ".join(table_sql.casefold().split())
        if "check ((space = 'learning') or (space = 'writing' and work_id is not null))" in compact:
            return
        raise sqlite3.OperationalError(
            "reviewed claim publication scope check is not a known schema"
        )

    objects = connection.execute(
        """SELECT type, name, sql FROM sqlite_master
           WHERE tbl_name = 'knowledge_reviewed_claim_publication'
             AND type IN ('index', 'trigger') AND sql IS NOT NULL
           ORDER BY type, name"""
    ).fetchall()
    indexes = [str(row["sql"]) for row in objects if row["type"] == "index"]
    triggers = [str(row["sql"]) for row in objects if row["type"] == "trigger"]
    columns = [
        str(row["name"])
        for row in connection.execute(
            "PRAGMA table_info(knowledge_reviewed_claim_publication)"
        )
    ]
    if not columns:
        raise sqlite3.OperationalError("reviewed claim publication columns are missing")

    for row in objects:
        if row["type"] == "trigger":
            connection.execute(f'DROP TRIGGER "{str(row["name"]).replace(chr(34), chr(34) * 2)}"')
    connection.execute(
        "ALTER TABLE knowledge_reviewed_claim_publication "
        "RENAME TO knowledge_reviewed_claim_publication_retirement_old"
    )
    connection.execute(widened_sql)
    quoted_columns = ", ".join('"' + name.replace('"', '""') + '"' for name in columns)
    connection.execute(
        f"INSERT INTO knowledge_reviewed_claim_publication ({quoted_columns}) "
        f"SELECT {quoted_columns} FROM knowledge_reviewed_claim_publication_retirement_old"
    )
    connection.execute("DROP TABLE knowledge_reviewed_claim_publication_retirement_old")
    for statement in indexes:
        connection.execute(statement)
    for statement in triggers:
        if "knowledge_reviewed_claim_publication_no_delete" not in statement.casefold():
            connection.execute(statement)


def _rebuild_claim_component_tables(connection: sqlite3.Connection) -> None:
    """Preserve every component while allowing learning-work scoped candidates."""
    connection.execute("""CREATE TABLE knowledge_claim_base_revision_retirement_new (
        id TEXT PRIMARY KEY,
        item_id TEXT NOT NULL REFERENCES knowledge_item(id) ON DELETE RESTRICT,
        revision INTEGER NOT NULL CHECK (revision > 0),
        space TEXT NOT NULL CHECK (space IN ('learning', 'writing')),
        work_id TEXT,
        content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
        content_json TEXT NOT NULL CHECK (json_valid(content_json)),
        created_at TEXT NOT NULL,
        UNIQUE(item_id, revision),
        UNIQUE(item_id, content_hash),
        UNIQUE(id, item_id),
        CHECK (space != 'writing' OR work_id IS NOT NULL)
    )""")
    connection.execute("""CREATE TABLE knowledge_claim_interpretation_retirement_new (
        id TEXT PRIMARY KEY,
        item_id TEXT NOT NULL REFERENCES knowledge_item(id) ON DELETE RESTRICT,
        component_kind TEXT NOT NULL CHECK (component_kind IN (
            'identity', 'review', 'typed_event', 'typed_quantity', 'typed_rule',
            'typed_instance', 'proposition_resolution'
        )),
        revision INTEGER NOT NULL CHECK (revision > 0),
        space TEXT NOT NULL CHECK (space IN ('learning', 'writing')),
        work_id TEXT,
        content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
        content_json TEXT NOT NULL CHECK (json_valid(content_json)),
        created_at TEXT NOT NULL,
        UNIQUE(item_id, component_kind, revision),
        UNIQUE(item_id, component_kind, content_hash),
        UNIQUE(id, item_id, component_kind),
        CHECK (space != 'writing' OR work_id IS NOT NULL)
    )""")
    connection.execute("""CREATE TABLE knowledge_claim_version_component_retirement_new (
        knowledge_version_id TEXT PRIMARY KEY
            REFERENCES knowledge_version(id) ON DELETE RESTRICT,
        item_id TEXT NOT NULL REFERENCES knowledge_item(id) ON DELETE RESTRICT,
        space TEXT NOT NULL CHECK (space IN ('learning', 'writing')),
        work_id TEXT,
        base_revision_id TEXT NOT NULL,
        interpretation_refs_json TEXT NOT NULL CHECK (json_valid(interpretation_refs_json)),
        evidence_refs_json TEXT NOT NULL CHECK (json_valid(evidence_refs_json)),
        evidence_hash TEXT NOT NULL CHECK (length(evidence_hash) = 64),
        composed_content_hash TEXT NOT NULL CHECK (length(composed_content_hash) = 64),
        scope_verified INTEGER NOT NULL CHECK (scope_verified IN (0, 1)),
        scope_diagnostics_json TEXT NOT NULL CHECK (json_valid(scope_diagnostics_json)),
        created_at TEXT NOT NULL,
        UNIQUE(knowledge_version_id, item_id),
        FOREIGN KEY (base_revision_id, item_id)
            REFERENCES knowledge_claim_base_revision_retirement_new(id, item_id) ON DELETE RESTRICT,
        CHECK (space != 'writing' OR work_id IS NOT NULL)
    )""")

    connection.execute("""INSERT INTO knowledge_claim_base_revision_retirement_new(
        id, item_id, revision, space, work_id, content_hash, content_json, created_at
    ) SELECT id, item_id, revision, space, work_id, content_hash, content_json, created_at
      FROM knowledge_claim_base_revision""")
    connection.execute("""INSERT INTO knowledge_claim_interpretation_retirement_new(
        id, item_id, component_kind, revision, space, work_id,
        content_hash, content_json, created_at
    ) SELECT id, item_id, component_kind, revision, space, work_id,
             content_hash, content_json, created_at
      FROM knowledge_claim_interpretation""")
    connection.execute("""INSERT INTO knowledge_claim_version_component_retirement_new(
        knowledge_version_id, item_id, space, work_id, base_revision_id,
        interpretation_refs_json, evidence_refs_json, evidence_hash,
        composed_content_hash, scope_verified, scope_diagnostics_json, created_at
    ) SELECT knowledge_version_id, item_id, space, work_id, base_revision_id,
             interpretation_refs_json, evidence_refs_json, evidence_hash,
             composed_content_hash, scope_verified, scope_diagnostics_json, created_at
      FROM knowledge_claim_version_component""")

    connection.execute("DROP TABLE knowledge_claim_version_component")
    connection.execute("DROP TABLE knowledge_claim_interpretation")
    connection.execute("DROP TABLE knowledge_claim_base_revision")
    connection.execute(
        "ALTER TABLE knowledge_claim_base_revision_retirement_new "
        "RENAME TO knowledge_claim_base_revision"
    )
    connection.execute(
        "ALTER TABLE knowledge_claim_interpretation_retirement_new "
        "RENAME TO knowledge_claim_interpretation"
    )
    connection.execute(
        "ALTER TABLE knowledge_claim_version_component_retirement_new "
        "RENAME TO knowledge_claim_version_component"
    )
    connection.execute(
        "CREATE INDEX knowledge_claim_base_item_idx "
        "ON knowledge_claim_base_revision(item_id, revision)"
    )
    connection.execute(
        "CREATE INDEX knowledge_claim_interpretation_item_idx "
        "ON knowledge_claim_interpretation(item_id, component_kind, revision)"
    )
    connection.execute(
        "CREATE INDEX knowledge_claim_version_item_idx "
        "ON knowledge_claim_version_component(item_id, knowledge_version_id)"
    )


def _install_selective_claim_retirement(connection: sqlite3.Connection) -> None:
    _rebuild_claim_component_tables(connection)
    _rebuild_reviewed_publication_scope(connection)
    statements = (
        """CREATE TABLE knowledge_item_retirement_authorization (
            item_id TEXT PRIMARY KEY REFERENCES knowledge_item(id) ON DELETE CASCADE,
            owner_kind TEXT NOT NULL CHECK (owner_kind IN (
                'parse_folder_clear', 'source_purge', 'project_delete'
            )),
            space TEXT NOT NULL CHECK (space IN ('learning', 'writing')),
            work_id TEXT,
            retired_at TEXT NOT NULL,
            CHECK (space != 'writing' OR work_id IS NOT NULL)
        )""",
        """CREATE TABLE knowledge_reviewed_claim_source_retirement_authorization (
            publication_id TEXT PRIMARY KEY,
            owner_kind TEXT NOT NULL CHECK (owner_kind = 'source_purge'),
            candidate_item_id TEXT NOT NULL REFERENCES knowledge_item(id) ON DELETE RESTRICT,
            source_id TEXT NOT NULL REFERENCES source(id) ON DELETE RESTRICT,
            snapshot_id TEXT NOT NULL REFERENCES source_snapshot(id) ON DELETE RESTRICT,
            space TEXT NOT NULL CHECK (space IN ('learning', 'writing')),
            work_id TEXT,
            authorized_at TEXT NOT NULL,
            CHECK (space != 'writing' OR work_id IS NOT NULL)
        )""",
        """CREATE TRIGGER knowledge_item_retirement_authorization_scope_insert
        BEFORE INSERT ON knowledge_item_retirement_authorization
        WHEN NOT EXISTS (
            SELECT 1 FROM knowledge_item AS item
            WHERE item.id = NEW.item_id AND item.kind = 'knowledge_graph_candidate'
              AND item.status = 'deleted'
              AND item.space = NEW.space AND item.work_id IS NEW.work_id
        )
        BEGIN SELECT RAISE(ABORT, 'retirement authorization requires a deleted candidate in the same scope'); END""",
        """CREATE TRIGGER knowledge_reviewed_claim_source_retirement_authorization_scope_insert
        BEFORE INSERT ON knowledge_reviewed_claim_source_retirement_authorization
        WHEN NOT EXISTS (
            SELECT 1
            FROM knowledge_reviewed_claim_publication AS publication
            JOIN knowledge_item AS item ON item.id = publication.candidate_item_id
            JOIN source ON source.id = publication.source_id
            JOIN source_snapshot AS snapshot
              ON snapshot.id = publication.snapshot_id
             AND snapshot.source_id = publication.source_id
            WHERE publication.id = NEW.publication_id
              AND publication.candidate_item_id = NEW.candidate_item_id
              AND publication.source_id = NEW.source_id
              AND publication.snapshot_id = NEW.snapshot_id
              AND publication.space = NEW.space
              AND publication.work_id IS NEW.work_id
              AND source.id = NEW.source_id
              AND source.space = NEW.space
              AND source.work_id IS NEW.work_id
              AND snapshot.id = NEW.snapshot_id
              AND item.kind = 'knowledge_graph_candidate'
              AND item.status NOT IN ('deleted', 'rejected', 'superseded')
              AND item.space = NEW.space AND item.work_id IS NEW.work_id
        )
        BEGIN SELECT RAISE(ABORT, 'source proof retirement must match a retained candidate and exact source snapshot'); END""",
        """CREATE TRIGGER knowledge_reviewed_claim_source_retirement_authorization_no_update
        BEFORE UPDATE ON knowledge_reviewed_claim_source_retirement_authorization
        BEGIN SELECT RAISE(ABORT, 'source proof retirement authorization is immutable'); END""",
        """CREATE TRIGGER knowledge_reviewed_claim_source_retirement_authorization_no_delete_while_proof_exists
        BEFORE DELETE ON knowledge_reviewed_claim_source_retirement_authorization
        WHEN EXISTS (
            SELECT 1 FROM knowledge_reviewed_claim_publication AS publication
            WHERE publication.id = OLD.publication_id
        )
        BEGIN SELECT RAISE(ABORT, 'source proof retirement authorization cannot be removed before its exact proof'); END""",
        """CREATE TRIGGER knowledge_claim_base_scope_insert
        BEFORE INSERT ON knowledge_claim_base_revision
        WHEN NOT EXISTS (
            SELECT 1 FROM knowledge_item AS item
            WHERE item.id = NEW.item_id AND item.kind = 'knowledge_graph_candidate'
              AND item.space = NEW.space AND item.work_id IS NEW.work_id
        )
        BEGIN SELECT RAISE(ABORT, 'base claim scope must match graph candidate'); END""",
        """CREATE TRIGGER knowledge_claim_interpretation_scope_insert
        BEFORE INSERT ON knowledge_claim_interpretation
        WHEN NOT EXISTS (
            SELECT 1 FROM knowledge_item AS item
            WHERE item.id = NEW.item_id AND item.kind = 'knowledge_graph_candidate'
              AND item.space = NEW.space AND item.work_id IS NEW.work_id
        )
        BEGIN SELECT RAISE(ABORT, 'claim interpretation scope must match graph candidate'); END""",
        """CREATE TRIGGER knowledge_claim_version_scope_insert
        BEFORE INSERT ON knowledge_claim_version_component
        WHEN NOT EXISTS (
            SELECT 1 FROM knowledge_item AS item
            JOIN knowledge_version AS version ON version.item_id = item.id
            WHERE item.id = NEW.item_id AND item.kind = 'knowledge_graph_candidate'
              AND item.space = NEW.space AND item.work_id IS NEW.work_id
              AND version.id = NEW.knowledge_version_id
        )
        BEGIN SELECT RAISE(ABORT, 'claim version scope must match graph candidate version'); END""",
        """CREATE TRIGGER knowledge_claim_base_no_update
        BEFORE UPDATE ON knowledge_claim_base_revision
        BEGIN SELECT RAISE(ABORT, 'claim base revisions are append-only'); END""",
        """CREATE TRIGGER knowledge_claim_interpretation_no_update
        BEFORE UPDATE ON knowledge_claim_interpretation
        BEGIN SELECT RAISE(ABORT, 'claim interpretations are append-only'); END""",
        """CREATE TRIGGER knowledge_claim_version_component_no_update
        BEFORE UPDATE ON knowledge_claim_version_component
        BEGIN SELECT RAISE(ABORT, 'claim version composition is immutable'); END""",
        """CREATE TRIGGER knowledge_claim_base_no_delete
        BEFORE DELETE ON knowledge_claim_base_revision
        WHEN NOT EXISTS (
            SELECT 1 FROM knowledge_item_retirement_authorization AS authorization
            JOIN knowledge_item AS item ON item.id = authorization.item_id
            WHERE authorization.item_id = OLD.item_id AND item.status = 'deleted'
        )
        BEGIN SELECT RAISE(ABORT, 'claim base deletion requires the selected-item purge owner'); END""",
        """CREATE TRIGGER knowledge_claim_interpretation_no_delete
        BEFORE DELETE ON knowledge_claim_interpretation
        WHEN NOT EXISTS (
            SELECT 1 FROM knowledge_item_retirement_authorization AS authorization
            JOIN knowledge_item AS item ON item.id = authorization.item_id
            WHERE authorization.item_id = OLD.item_id AND item.status = 'deleted'
        )
        BEGIN SELECT RAISE(ABORT, 'claim interpretation deletion requires the selected-item purge owner'); END""",
        """CREATE TRIGGER knowledge_claim_version_component_no_delete
        BEFORE DELETE ON knowledge_claim_version_component
        WHEN NOT EXISTS (
            SELECT 1 FROM knowledge_item_retirement_authorization AS authorization
            JOIN knowledge_item AS item ON item.id = authorization.item_id
            WHERE authorization.item_id = OLD.item_id AND item.status = 'deleted'
        )
        BEGIN SELECT RAISE(ABORT, 'claim composition deletion requires the selected-item purge owner'); END""",
        "DROP TRIGGER IF EXISTS knowledge_reviewed_claim_publication_no_delete",
        """CREATE TRIGGER knowledge_reviewed_claim_publication_no_delete
        BEFORE DELETE ON knowledge_reviewed_claim_publication
        WHEN NOT EXISTS (
            SELECT 1 FROM knowledge_item_retirement_authorization AS authorization
            JOIN knowledge_item AS item ON item.id = authorization.item_id
            WHERE authorization.item_id = OLD.candidate_item_id AND item.status = 'deleted'
        ) AND NOT EXISTS (
            SELECT 1 FROM knowledge_reviewed_claim_source_retirement_authorization AS authorization
            WHERE authorization.publication_id = OLD.id
              AND authorization.owner_kind = 'source_purge'
              AND authorization.candidate_item_id = OLD.candidate_item_id
              AND authorization.source_id = OLD.source_id
              AND authorization.snapshot_id = OLD.snapshot_id
              AND authorization.space = OLD.space
              AND authorization.work_id IS OLD.work_id
        )
        BEGIN SELECT RAISE(ABORT, 'reviewed claim publication deletion requires an exact item or source purge owner'); END""",
    )
    for statement in statements:
        connection.execute(statement)


MIGRATIONS_CLAIM_COMPONENT_RETIREMENT = (
    Migration(
        version=90,
        sql="",
        callback=_install_selective_claim_retirement,
        rebuilds_referenced_table=True,
    ),
)
