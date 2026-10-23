"""Cover conversation summaries without reading full message histories."""

from .migration_types import Migration


MIGRATIONS_CONVERSATION_SUMMARIES = (
    Migration(
        version=105,
        sql="""
        CREATE INDEX assistant_conversation_summary_idx
        ON assistant_conversation(
            space, work_id, updated_at DESC, id ASC, title, revision,
            json_array_length(messages_json),
            substr(COALESCE(json_extract(messages_json, '$[#-1].content'), ''), 1, 100)
        );
        """,
    ),
)
