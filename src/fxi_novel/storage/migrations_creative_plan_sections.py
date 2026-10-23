"""Allow existing creative-plan owners to place freely named content sections."""

from .migration_types import Migration


MIGRATIONS_CREATIVE_PLAN_SECTIONS = (
    Migration(version=100, sql="""
        CREATE TABLE creative_plan_section_state_free (
            work_id TEXT NOT NULL CHECK (length(trim(work_id)) BETWEEN 1 AND 200),
            section_id TEXT NOT NULL CHECK (
                length(trim(section_id)) BETWEEN 1 AND 200 AND section_id = trim(section_id)
            ),
            folder_id TEXT REFERENCES knowledge_folder(id) ON DELETE SET NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (work_id, section_id)
        );
        INSERT INTO creative_plan_section_state_free(work_id, section_id, folder_id, updated_at)
            SELECT work_id, section_id, folder_id, updated_at FROM creative_plan_section_state;
        DROP TABLE creative_plan_section_state;
        ALTER TABLE creative_plan_section_state_free RENAME TO creative_plan_section_state;
        CREATE INDEX creative_plan_section_state_location_idx
            ON creative_plan_section_state(work_id, folder_id, section_id);
        CREATE INDEX creative_plan_section_state_folder_reference_idx
            ON creative_plan_section_state(folder_id);
    """),
)
