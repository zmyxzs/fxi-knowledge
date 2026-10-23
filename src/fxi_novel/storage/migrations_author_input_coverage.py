"""Track consumed author inputs separately from authoritative creative notes."""
from .migration_types import Migration


MIGRATIONS_AUTHOR_INPUT_COVERAGE = (Migration(version=101, sql="""
    CREATE TABLE author_input_coverage (
        id TEXT PRIMARY KEY,
        work_id TEXT NOT NULL,
        source_json TEXT NOT NULL,
        original_hash TEXT NOT NULL,
        remaining_text TEXT NOT NULL,
        disposition TEXT NOT NULL,
        record_ids_json TEXT NOT NULL,
        resolution_id TEXT NOT NULL
    );
    CREATE INDEX author_input_coverage_work ON author_input_coverage(work_id);
    CREATE TABLE author_input_resolution (
        id TEXT PRIMARY KEY,
        work_id TEXT NOT NULL,
        input_json TEXT NOT NULL,
        response_json TEXT NOT NULL
    );
"""),)
