"""Index child references so version retirement and item deletion stay bounded."""

from .migration_types import Migration


MIGRATIONS_KNOWLEDGE_REFERENCE_INDEXES = (
    Migration(version=70, sql="""
        CREATE INDEX IF NOT EXISTS knowledge_identity_review_version_reference_idx
            ON knowledge_graph_identity_review_index(candidate_version_id);
        CREATE INDEX IF NOT EXISTS knowledge_relation_reference_version_idx
            ON knowledge_graph_relation_reference(candidate_version_id);
        CREATE INDEX IF NOT EXISTS knowledge_card_folder_summary_version_reference_idx
            ON knowledge_card_folder_summary(representative_version_id);
        CREATE INDEX IF NOT EXISTS knowledge_card_folder_summary_item_reference_idx
            ON knowledge_card_folder_summary(representative_item_id);
        CREATE INDEX IF NOT EXISTS knowledge_card_summary_version_reference_idx
            ON knowledge_card_summary(representative_version_id);
        CREATE INDEX IF NOT EXISTS knowledge_card_summary_item_reference_idx
            ON knowledge_card_summary(representative_item_id);
        CREATE INDEX IF NOT EXISTS knowledge_projection_dependency_version_reference_idx
            ON knowledge_projection_dependency(knowledge_version_id);
        CREATE INDEX IF NOT EXISTS knowledge_projection_dependency_item_reference_idx
            ON knowledge_projection_dependency(knowledge_item_id);
        CREATE INDEX IF NOT EXISTS knowledge_graph_projection_version_reference_idx
            ON knowledge_graph_projection(candidate_version_id);
    """),
    Migration(version=71, sql="""
        CREATE INDEX IF NOT EXISTS knowledge_card_candidate_evidence_item_reference_idx
            ON knowledge_card_candidate_evidence(candidate_item_id);
        CREATE INDEX IF NOT EXISTS knowledge_card_candidate_evidence_span_reference_idx
            ON knowledge_card_candidate_evidence(evidence_span_id);
        CREATE INDEX IF NOT EXISTS knowledge_relation_evidence_reference_idx
            ON knowledge_relation(evidence_span_id);
        CREATE INDEX IF NOT EXISTS writing_learning_scope_folder_reference_idx
            ON writing_learning_scope_reference(folder_id);
        CREATE INDEX IF NOT EXISTS knowledge_parse_receipt_snapshot_reference_idx
            ON knowledge_parse_receipt(snapshot_id);
        CREATE INDEX IF NOT EXISTS source_import_batch_item_source_reference_idx
            ON source_import_batch_item(source_id);
        CREATE INDEX IF NOT EXISTS creative_plan_state_folder_reference_idx
            ON creative_plan_state(folder_id);
        CREATE INDEX IF NOT EXISTS prose_artifact_state_folder_reference_idx
            ON prose_artifact_state(folder_id);
        CREATE INDEX IF NOT EXISTS prose_detector_candidate_revision_reference_idx
            ON prose_detector_comparison(candidate_artifact_id, candidate_revision);
        CREATE INDEX IF NOT EXISTS prose_detector_source_revision_reference_idx
            ON prose_detector_comparison(source_artifact_id, source_revision);
        CREATE INDEX IF NOT EXISTS prose_chapter_head_revision_reference_idx
            ON prose_chapter_head(adopted_artifact_id, adopted_revision);
        CREATE INDEX IF NOT EXISTS knowledge_projection_dependency_event_reference_idx
            ON knowledge_projection_dependency(invalidated_by_event_id);
        CREATE INDEX IF NOT EXISTS knowledge_prose_event_receipt_event_reference_idx
            ON knowledge_prose_event_receipt(event_id);
        CREATE INDEX IF NOT EXISTS knowledge_prose_refresh_snapshot_reference_idx
            ON knowledge_prose_refresh(snapshot_id);
        CREATE INDEX IF NOT EXISTS knowledge_prose_refresh_source_reference_idx
            ON knowledge_prose_refresh(source_id);
        CREATE INDEX IF NOT EXISTS knowledge_prose_refresh_event_reference_idx
            ON knowledge_prose_refresh(event_id);
        CREATE INDEX IF NOT EXISTS chapter_outline_state_folder_reference_idx
            ON chapter_outline_state(folder_id);
        CREATE INDEX IF NOT EXISTS knowledge_card_folder_summary_folder_reference_idx
            ON knowledge_card_folder_summary(folder_id);
        CREATE INDEX IF NOT EXISTS knowledge_card_folder_summary_dirty_folder_reference_idx
            ON knowledge_card_folder_summary_dirty(folder_id);
        CREATE INDEX IF NOT EXISTS creative_plan_section_state_folder_reference_idx
            ON creative_plan_section_state(folder_id);
        CREATE INDEX IF NOT EXISTS knowledge_identity_review_source_reference_idx
            ON knowledge_graph_identity_review_index(source_id);
        CREATE INDEX IF NOT EXISTS knowledge_story_order_entry_snapshot_reference_idx
            ON knowledge_story_order_entry(snapshot_id);
        CREATE INDEX IF NOT EXISTS knowledge_story_order_entry_source_reference_idx
            ON knowledge_story_order_entry(source_id);
        CREATE INDEX IF NOT EXISTS knowledge_story_order_current_revision_reference_idx
            ON knowledge_story_order_current(work_id, revision);
    """),
)
