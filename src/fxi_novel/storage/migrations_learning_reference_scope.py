"""Keep learning-reference guards aligned with registered folder views."""

from .migration_types import Migration


def _trigger(name: str, event: str) -> str:
    return f"""
    CREATE TRIGGER {name}
    BEFORE {event} ON writing_learning_scope_reference
    WHEN NOT EXISTS (
        SELECT 1 FROM knowledge_project p
        WHERE p.id = NEW.work_id AND p.space = 'writing' AND p.status = 'active'
    ) OR (
        NEW.learning_work_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM knowledge_project p
            WHERE p.id = NEW.learning_work_id AND p.space = 'learning' AND p.status = 'active'
        )
    ) OR (
        NEW.folder_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM knowledge_folder f
            WHERE f.id = NEW.folder_id AND f.space = 'learning' AND (
                f.work_id IS NEW.learning_work_id OR (
                    NEW.learning_work_id IS NOT NULL AND EXISTS (
                        SELECT 1 FROM knowledge_learning_work_folder assigned
                        JOIN knowledge_folder root ON root.id = assigned.folder_id
                        WHERE assigned.work_id = NEW.learning_work_id
                          AND root.work_id IS f.work_id AND (
                              f.path = root.path COLLATE NOCASE
                              OR substr(f.path, 1, length(root.path) + 1) = root.path || '/' COLLATE NOCASE
                              OR substr(root.path, 1, length(f.path) + 1) = f.path || '/' COLLATE NOCASE
                          )
                    )
                )
            )
        )
    )
    BEGIN
        SELECT RAISE(ABORT, 'learning scope must belong to an active learning library');
    END;
    """


MIGRATIONS_LEARNING_REFERENCE_SCOPE = (Migration(version=102, sql=(
    "DROP TRIGGER writing_learning_scope_reference_insert_scope;"
    "DROP TRIGGER writing_learning_scope_reference_update_scope;"
    + _trigger("writing_learning_scope_reference_insert_scope", "INSERT")
    + _trigger("writing_learning_scope_reference_update_scope", "UPDATE OF work_space, work_id, learning_work_id, folder_id")
)),)
