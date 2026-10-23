"""Stable identities and vector validation helpers for embedding projection."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import struct
from urllib.parse import urlsplit

from fxi_novel.models.gateway import ModelRoute


@dataclass(frozen=True)
class EmbeddingRouteIdentity:
    fingerprint: str
    provider: str
    model_id: str


@dataclass(frozen=True)
class DenseQueryEmbedding:
    identity: EmbeddingRouteIdentity
    dimensions: int
    vector: tuple[float, ...]


def embedding_route_identity(route: ModelRoute) -> EmbeddingRouteIdentity | None:
    """Return a stable, secret-free identity for compatible document vectors."""
    if not route.enabled or not route.base_url.strip() or not route.model.strip():
        return None
    configured_endpoint = urlsplit(route.base_url.strip().rstrip("/"))
    host = configured_endpoint.hostname or "custom-endpoint"
    if configured_endpoint.port is not None:
        host = f"{host}:{configured_endpoint.port}"
    normalized_path = configured_endpoint.path.rstrip("/")
    base_url = f"{configured_endpoint.scheme.casefold()}://{host.casefold()}{normalized_path}"
    material = {
        "format": "fxi-embedding-f32le-v1",
        "base_url": base_url,
        "model": route.model.strip(),
        "dimensions": route.dimensions,
        "token_env": route.token_env.strip(),
    }
    if route.query_prefix:
        material["query_prefix"] = route.query_prefix
    fingerprint = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    provider = host.casefold()
    return EmbeddingRouteIdentity(
        fingerprint=fingerprint,
        provider=provider,
        model_id=route.model.strip(),
    )


def encode_embedding_vector(vector: list[float], dimensions: int) -> bytes:
    if len(vector) != dimensions or dimensions < 1:
        raise ValueError("embedding vector length does not match its reported dimensions")
    if any(not isinstance(value, (int, float)) or not math.isfinite(float(value)) for value in vector):
        raise ValueError("embedding vector must contain only finite numbers")
    return struct.pack(f"<{dimensions}f", *vector)


__all__ = [
    "DenseQueryEmbedding",
    "EmbeddingRouteIdentity",
    "encode_embedding_vector",
    "embedding_route_identity",
]
