"""Batch composition of exact, capability-gated candidate vector inputs."""

from .candidate_query_projection import candidate_publication_projection
from .repository_claim_components import compose_candidate_version_batch
from .repository_parse_publication import graph_candidate_publication_capabilities


def load_candidate_component_projections(connection, rows):
    versions = {str(row["item_id"]): str(row["version_id"]) for row in rows}
    components = compose_candidate_version_batch(connection, version_pins=versions)
    bases = {str(row["item_id"]): row["publication_basis"] for row in rows}
    for item_id, component in components.items():
        if component.get("error"):
            continue
        publication = graph_candidate_publication_capabilities(bases[item_id])
        try:
            component["content"], component["publication_gaps"] = candidate_publication_projection(
                component["content"], publication,
            )
            component["publication_basis"] = publication["basis"]
        except (TypeError, ValueError) as exc:
            components[item_id] = {"error": str(exc)}
    return components
