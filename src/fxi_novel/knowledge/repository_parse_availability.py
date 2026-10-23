"""Read publication availability independently of parse-stage execution status."""

from .candidate_publication_sql import reviewed_claim_table_exists
from .repository_parse_publication import (
    graph_candidate_publication_basis_sql, published_graph_candidate_filter,
)


def read_batch_claim_availability(connection, batch_id):
    if not reviewed_claim_table_exists(connection):
        return {}
    basis = graph_candidate_publication_basis_sql("item")
    published = published_graph_candidate_filter("item")
    rows = connection.execute(
        f"""WITH readable AS MATERIALIZED (
                SELECT selected.ordinal, ({basis}) AS basis
                FROM knowledge_parse_batch_item AS selected
                JOIN knowledge_parse_receipt AS receipt
                  ON receipt.id = json_extract(selected.result_json, '$.receipt_id')
                 AND receipt.parse_batch_id = selected.batch_id
                 AND receipt.source_id = selected.source_id
                 AND receipt.snapshot_id = selected.snapshot_id
                 AND receipt.generation = json_extract(selected.result_json, '$.generation')
                JOIN knowledge_item AS item ON item.origin_parse_receipt_id = receipt.id
                WHERE selected.batch_id = ? AND {published}
            )
            SELECT ordinal, COUNT(*) AS published_records,
                   SUM(basis = 'reviewed_claim') AS reviewed_only_records,
                   SUM(basis IN ('authored', 'parse_pipeline')) AS pipeline_published_records
            FROM readable GROUP BY ordinal""", (batch_id,),
    ).fetchall()
    return {int(row["ordinal"]): {
        key: int(row[key] or 0) for key in (
            "published_records", "reviewed_only_records", "pipeline_published_records",
        )
    } for row in rows}
