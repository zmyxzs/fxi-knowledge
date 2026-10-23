"""Publish-time queueing of scoped semantic candidate vectors."""

from __future__ import annotations

import asyncio


class EmbeddingCandidateProjectionMixin:
    """One bounded publish hook using the manager's stable automatic scope."""

    async def enqueue_published_knowledge_candidate_jobs(
        self,
        *,
        publication_id: str,
        source_ids: list[str],
        limit: int = 512,
    ) -> dict[str, object]:
        """Queue a published scope and persist its success, failure, or unavailable receipt."""
        receipt = await asyncio.to_thread(
            self.repository.begin_candidate_embedding_enqueue,
            publication_id=publication_id,
            source_ids=source_ids,
        )
        if receipt.get("already_queued"):
            return {
                "publication_id": publication_id,
                "status": "queued",
                "already_queued": True,
                **(receipt.get("result") if isinstance(receipt.get("result"), dict) else {}),
            }
        pins: list[dict[str, str]] = []
        route_fingerprint: str | None = None
        try:
            result, pins, route_fingerprint = await self._enqueue_published_candidate_scope(
                source_ids=source_ids, limit=limit,
            )
        except Exception as exc:
            result = {
                "status": "failed",
                "enqueued": 0,
                "has_more": False,
                "error": f"{type(exc).__name__}: {exc}"[:4000],
            }
            await asyncio.to_thread(
                self.repository.finish_candidate_embedding_enqueue,
                publication_id=publication_id,
                request_hash=str(receipt["request_hash"]),
                status="failed",
                result=result,
                source_snapshot_pins=pins,
                route_fingerprint=route_fingerprint,
                error=str(result["error"]),
            )
            return {"publication_id": publication_id, **result}
        await asyncio.to_thread(
            self.repository.finish_candidate_embedding_enqueue,
            publication_id=publication_id,
            request_hash=str(receipt["request_hash"]),
            status=str(result["status"]),
            result=result,
            source_snapshot_pins=pins,
            route_fingerprint=route_fingerprint,
        )
        return {"publication_id": publication_id, **result}

    async def _enqueue_published_candidate_scope(
        self, *, source_ids: list[str], limit: int, selected_scope=None,
    ) -> tuple[dict[str, object], list[dict[str, str]], str | None]:
        """Queue every durable cursor page and return the exact pins used."""
        scope = selected_scope or self._automatic_index_scope
        if scope is None:
            if self._automatic_index_folder_configured:
                return {"status": "unavailable", "enqueued": 0, "has_more": False}, [], None
            scopes = await asyncio.to_thread(self.repository.list_embedding_scopes, source_ids)
            totals = {"status": "queued", "scanned": 0, "enqueued": 0, "invalid_candidate_count": 0, "has_more": False}
            pins = []
            fingerprint = None
            for current in scopes:
                selected = await asyncio.to_thread(self.repository.list_current_embedding_snapshot_refs, **current)
                result, part, fingerprint = await self._enqueue_published_candidate_scope(
                    source_ids=[row["source_id"] for row in selected], limit=limit, selected_scope=current)
                for key in ("scanned", "enqueued", "invalid_candidate_count"):
                    totals[key] += int(result.get(key, 0))
                if result.get("status") != "queued":
                    totals["status"] = result.get("status", "unavailable")
                pins.extend(part)
            if not scopes:
                totals["status"] = "unavailable"
            self._work_available.set()
            return totals, pins, fingerprint
        if not isinstance(source_ids, list) or not source_ids or len(source_ids) > 10_000:
            raise ValueError("source_ids must contain one to 10,000 IDs")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 512:
            raise ValueError("candidate embedding page size must be between 1 and 512")
        if any(not isinstance(value, str) or not value.strip() for value in source_ids):
            raise ValueError("source_ids must contain non-empty IDs")
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("source_ids must be unique")
        allowed = await asyncio.to_thread(
            self.repository.list_current_embedding_snapshot_refs,
            space=str(scope["space"]),
            work_id=scope["work_id"],
            folder_ids=scope.get("folder_ids"),
            source_ids=scope.get("source_ids"),
        )
        allowed_by_source = {row["source_id"]: row for row in allowed}
        outside = sorted(set(source_ids) - set(allowed_by_source))
        if outside:
            return ({
                "status": "outside_automatic_scope",
                "enqueued": 0,
                "has_more": False,
                "rejected_source_ids": outside,
            }, [], None)
        identity = self.route_identity
        if identity is None or not self.indexing_enabled:
            return {"status": "unavailable", "enqueued": 0, "has_more": False}, [], None
        selected_refs = [allowed_by_source[source_id] for source_id in source_ids]
        totals = {"scanned": 0, "enqueued": 0, "invalid_candidate_count": 0}
        while True:
            if self.route_identity != identity:
                raise RuntimeError("embedding route changed while queuing published candidates")
            progress = await asyncio.to_thread(
                self.repository.enqueue_knowledge_item_embedding_scope_page,
                space=str(scope["space"]),
                work_id=scope["work_id"],
                allowed_source_snapshots=selected_refs,
                route_fingerprint=identity.fingerprint,
                provider=identity.provider,
                model_id=identity.model_id,
                limit=limit,
            )
            for key in totals:
                totals[key] += int(progress.get(key, 0) or 0)
            if not bool(progress.get("has_more")):
                break
            if int(progress.get("scanned", 0) or 0) < 1:
                raise RuntimeError("candidate embedding cursor made no progress")
            await asyncio.sleep(0)
        self._wake_started_worker(dict(scope))
        return (
            {**progress, **totals, "status": "queued", "has_more": False},
            selected_refs,
            identity.fingerprint,
        )


__all__ = ["EmbeddingCandidateProjectionMixin"]
