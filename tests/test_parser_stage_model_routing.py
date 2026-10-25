"""The parser can extract with one model and review with another."""

from __future__ import annotations

from pathlib import Path

from fxi_novel.knowledge.graph_parser import KnowledgeGraphParseService
from fxi_novel.models.gateway import GatewayConfig, ModelGateway, ModelRoute


ROOT = Path(__file__).resolve().parents[1]


def _gateway(model: str) -> ModelGateway:
    route = ModelRoute(
        enabled=True, base_url="https://example.invalid/v1", model=model,
        token_env="", timeout_seconds=90,
    )
    return ModelGateway(GatewayConfig(chat=route, embedding=route))


def _service(gateway: ModelGateway, review_gateway: ModelGateway | None = None) -> KnowledgeGraphParseService:
    prompts = ROOT / "prompts" / "learning"
    return KnowledgeGraphParseService(
        object(), gateway,
        prompts / "knowledge-graph-parse.md",
        prompts / "knowledge-graph-coverage-audit.md",
        prompts / "knowledge-graph-identity-reconcile.md",
        prompts / "knowledge-graph-semantic-review.md",
        review_gateway=review_gateway,
    )


def test_review_gateway_defaults_to_the_extraction_gateway() -> None:
    gateway = _gateway("extract-model")
    service = _service(gateway)
    assert service.review_gateway is gateway


def test_review_gateway_participates_in_the_route_fingerprint() -> None:
    extraction = _gateway("extract-model")
    review = _gateway("review-model")
    default = _service(extraction).route_fingerprint
    split = _service(extraction, review).route_fingerprint
    assert default != split
    assert _service(extraction, review).route_fingerprint == split
