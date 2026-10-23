"""Exact candidate composition and authorized public index projection."""

import json

from .candidate_query_projection import candidate_publication_projection
from .repository_claim_components import (
    claim_component_tables_exist, persist_graph_candidate_components,
)
from .repository_parse_publication import graph_candidate_publication_basis
from .candidate_publication_sql import reviewed_claim_table_exists


def candidate_content_for_index(connection, *, item_id, version_id, content):
    if not claim_component_tables_exist(connection):
        return content
    version = connection.execute(
        "SELECT content_json FROM knowledge_version WHERE id = ? AND item_id = ?",
        (version_id, item_id),
    ).fetchone()
    if version is None:
        raise ValueError("candidate index requires its exact persisted version")
    # Some adapters pass a Pydantic object with added defaults. The stored
    # immutable version, including absent fields and legacy values, is the owner.
    content = json.loads(str(version["content_json"]))
    composed = persist_graph_candidate_components(
        connection, item_id=item_id, version_id=version_id, content=content,
    )["content"]
    if isinstance(composed, dict):
        composed = {"record_type": "knowledge_graph_candidate", **composed}
    if not reviewed_claim_table_exists(connection):
        return composed
    publication = graph_candidate_publication_basis(connection, item_id, version_id)
    if not publication["published"]:
        return composed
    return candidate_publication_projection(composed, publication)[0]
