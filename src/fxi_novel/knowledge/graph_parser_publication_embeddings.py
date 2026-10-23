"""Queue reconstructible vectors after the knowledge owner's atomic commit."""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any

from .graph_parser_support import logger


async def enqueue_published_component_embeddings(
    knowledge: Any, batch: dict[str, Any], component: list[dict[str, Any]],
) -> dict[str, Any]:
    pins = sorted((str(item["source_id"]), str(item["snapshot_id"])) for item in component)
    identity = json.dumps([str(batch["batch_id"]), pins], separators=(",", ":"))
    publication_id = "parse-component:" + sha256(identity.encode("utf-8")).hexdigest()
    try:
        return await knowledge.enqueue_published_knowledge_candidate_jobs(
            publication_id=publication_id,
            source_ids=[source_id for source_id, _ in pins], limit=512,
        )
    except Exception:
        # The queue owner persists ordinary enqueue failures. An unavailable
        # receipt store must not retroactively fail the committed publication.
        logger.exception("published component %s vector enqueue receipt failed", publication_id)
        return {"publication_id": publication_id, "status": "receipt_failed"}
