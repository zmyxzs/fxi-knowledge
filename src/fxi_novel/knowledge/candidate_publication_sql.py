"""SQL adapters for the same publication capabilities used by Python readers."""

from .claim_components import IDENTITY_FIELDS, PROPOSITION_FIELDS, TYPED_FIELDS
from .repository_parse_publication import graph_candidate_publication_basis_sql


def published_candidate_content_sql(
    item_alias: str,
    content_sql: str,
    *,
    include_query_reviewed_claims: bool = True,
) -> str:
    withheld = sorted(IDENTITY_FIELDS | PROPOSITION_FIELDS | set(TYPED_FIELDS))
    paths = ", ".join(f"'$.{field}'" for field in withheld)
    basis = graph_candidate_publication_basis_sql(
        item_alias,
        include_query_reviewed_claims=include_query_reviewed_claims,
    )
    return f"""CASE
        WHEN ({basis}) = 'query_reviewed_claim'
          THEN json_remove(
              {content_sql}, {paths}, '$.semantic_review', '$.review_note',
              '$.proposed_assessment'
          )
        WHEN ({basis}) = 'reviewed_claim'
          THEN json_remove({content_sql}, {paths}, '$.query_learning_claim')
        ELSE json_remove({content_sql}, '$.query_learning_claim')
    END"""


def published_interpretation_filter_sql(item_alias: str) -> str:
    return f"({graph_candidate_publication_basis_sql(item_alias)}) IN ('authored', 'parse_pipeline')"


def reviewed_claim_table_exists(connection) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'knowledge_reviewed_claim_publication'",
    ).fetchone() is not None
