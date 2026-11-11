"""Small client for the kg knowledge base API.

Usage:
    from examples.kb_client import KnowledgeClient

    client = KnowledgeClient(api_key="...")
    client.search("杨凌 等级")
    client.create_work("新作品")
"""

from __future__ import annotations

from typing import Any

import httpx


class KnowledgeClient:
    def __init__(
        self,
        base_url: str = "https://kg.zmteams.site",
        *,
        api_key: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def _headers(self, write: bool) -> dict[str, str]:
        if not write:
            return {}
        if not self.api_key:
            raise ValueError("api_key is required for write operations")
        return {"X-API-Key": self.api_key}

    def _request(self, method: str, path: str, *, write: bool = False, **kwargs: Any) -> Any:
        response = self._client.request(
            method, path, headers=self._headers(write), **kwargs,
        )
        response.raise_for_status()
        return response.json()

    def health(self) -> dict:
        return self._request("GET", "/health")

    def works(self) -> dict:
        return self._request("GET", "/api/works")

    def sources(self, work: str) -> dict:
        return self._request("GET", "/api/sources", params={"work": work})

    def search(self, query: str, *, work: str | None = None, top_k: int = 5) -> dict:
        params: dict[str, Any] = {"q": query, "top_k": top_k}
        if work:
            params["work"] = work
        return self._request("GET", "/api/search", params=params)

    def story(
        self, work: str, *, category: str | None = None,
        page_size: int = 20, offset: int = 0,
    ) -> dict:
        params: dict[str, Any] = {
            "work": work, "page_size": page_size, "offset": offset,
        }
        if category:
            params["category"] = category
        return self._request("GET", "/api/story", params=params)

    def entity(self, name: str, *, work: str | None = None) -> dict:
        params: dict[str, Any] = {"name": name}
        if work:
            params["work"] = work
        return self._request("GET", "/api/entity", params=params)

    def audit(self, work: str, *, llm: bool = False) -> dict:
        return self._request(
            "GET", "/api/audit", params={"work": work, "llm": 1 if llm else 0},
        )

    def create_work(self, title: str, description: str = "") -> dict:
        return self._request(
            "POST", "/api/works", write=True,
            json={"title": title, "description": description},
        )

    def update_work(self, work_id: str, title: str, description: str = "") -> dict:
        return self._request(
            "PUT", f"/api/works/{work_id}", write=True,
            json={"title": title, "description": description},
        )

    def delete_work(self, work_id: str) -> dict:
        return self._request("DELETE", f"/api/works/{work_id}", write=True)

    def upload_source(self, work: str, title: str, content: str) -> dict:
        return self._request(
            "POST", "/api/sources", write=True,
            json={"work": work, "title": title, "content": content},
        )

    def update_source(self, source_id: str, title: str, content: str) -> dict:
        return self._request(
            "PUT", f"/api/sources/{source_id}", write=True,
            json={"title": title, "content": content},
        )

    def delete_source(self, source_id: str) -> dict:
        return self._request("DELETE", f"/api/sources/{source_id}", write=True)

    def parse(self, work: str, *, source_ids: list[str] | None = None) -> dict:
        payload: dict[str, Any] = {"work": work}
        if source_ids:
            payload["source_ids"] = source_ids
        return self._request("POST", "/api/parse", write=True, json=payload)

    def job(self, job_id: str) -> dict:
        return self._request("GET", f"/api/parse/{job_id}")

    def work_types(self) -> dict:
        return self._request("GET", "/api/work_types")

    def work_profile(self, work: str) -> dict:
        return self._request("GET", "/api/work_profile", params={"work": work})

    def set_work_profile(self, work: str, **fields: Any) -> dict:
        return self._request(
            "POST", "/api/work_profile", write=True,
            json={"work": work, **fields},
        )

    def story_records(
        self, work: str, *, category: str | None = None,
        at_chapter: int | None = None, limit: int = 500, offset: int = 0,
    ) -> dict:
        params: dict[str, Any] = {
            "work": work, "limit": limit, "offset": offset,
        }
        if category:
            params["category"] = category
        if at_chapter is not None:
            params["at_chapter"] = at_chapter
        return self._request("GET", "/api/story_record", params=params)

    def compile_archive(
        self, work: str, chapter_index: int, outline: str = "",
        *, allow_future: bool = False,
    ) -> dict:
        return self._request(
            "POST", "/api/compile_archive", write=True,
            json={
                "work": work, "chapter_index": chapter_index,
                "outline": outline, "allow_future": allow_future,
            },
        )

    def documents(
        self, work: str, *, kind: str | None = None,
        scope_key: str | None = None, version: int | None = None,
        status: str | None = None,
    ) -> dict:
        params: dict[str, Any] = {"work": work}
        for key, value in (
            ("kind", kind), ("scope_key", scope_key),
            ("version", version), ("status", status),
        ):
            if value is not None:
                params[key] = value
        return self._request("GET", "/api/documents", params=params)

    def save_document(self, work: str, payload: dict) -> dict:
        return self._request(
            "POST", "/api/documents", write=True,
            json={"work": work, **payload},
        )

    def review_document(
        self, work: str, *, kind: str, scope_key: str, version: int,
        action: str, base_version: int | None = None,
    ) -> dict:
        payload: dict[str, Any] = {
            "work": work, "kind": kind, "scope_key": scope_key,
            "version": version, "action": action,
        }
        if base_version is not None:
            payload["base_version"] = base_version
        return self._request(
            "POST", "/api/documents/review", write=True, json=payload,
        )

    def reproject_document(
        self, work: str, *, kind: str, scope_key: str, version: int,
    ) -> dict:
        return self._request(
            "POST", "/api/documents/reproject", write=True,
            json={
                "work": work, "kind": kind,
                "scope_key": scope_key, "version": version,
            },
        )

    def search_documents(
        self, work: str, query: str, *, kind: str | None = None,
        limit: int = 10,
    ) -> dict:
        params: dict[str, Any] = {"work": work, "q": query, "limit": limit}
        if kind:
            params["kind"] = kind
        return self._request("GET", "/api/documents/search", params=params)

    def context(
        self, work: str, *, chapter: int | None = None,
        max_chars: int = 60_000,
    ) -> dict:
        params: dict[str, Any] = {"work": work, "max_chars": max_chars}
        if chapter is not None:
            params["chapter"] = chapter
        return self._request("GET", "/api/context", params=params)

    def audit_draft(
        self, work: str, text: str, *, chapter: int | None = None,
        llm: bool = False,
    ) -> dict:
        payload: dict[str, Any] = {
            "work": work, "text": text, "llm": llm,
        }
        if chapter is not None:
            payload["chapter_index"] = chapter
        return self._request(
            "POST", "/api/audit", write=True, json=payload,
        )
