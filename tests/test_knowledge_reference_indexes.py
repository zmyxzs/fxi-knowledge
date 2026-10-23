"""SQLite retirement plans must use child-key indexes with FK checks enabled."""

from fxi_novel.storage.database import Database


def test_version_and_item_deletes_do_not_scan_reference_tables():
    database = Database(":memory:")
    database.initialize()
    references = {
        "knowledge_graph_identity_review_index", "knowledge_graph_relation_reference",
        "knowledge_card_folder_summary", "knowledge_card_summary",
        "knowledge_projection_dependency", "knowledge_graph_projection",
        "knowledge_card_candidate_evidence",
    }
    with database.connection() as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        for parent in ("knowledge_item", "knowledge_version", "knowledge_card_candidate"):
            plan = [str(row[3]) for row in connection.execute(
                f"EXPLAIN QUERY PLAN DELETE FROM {parent} WHERE "
                + ("candidate_item_id" if parent == "knowledge_card_candidate" else "id") + " = ?", ("missing",),
            )]
            assert all(not any(f"SCAN {table}" in detail for table in references) for detail in plan), plan
