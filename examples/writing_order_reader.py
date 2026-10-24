"""Opt-in narrative-order reader for standalone writing-space parsing.

In the full product the writing system owns the narrative manifest, and
``KnowledgeService.set_writing_narrative_order_reader`` is wired to it. A
standalone checkout has no writer, so parsing the ``writing`` space needs an
explicit reader. This module is that explicit, deliberately small reader: it
builds a verified manifest from the sources already stored for one work,
ordered by import time.

Usage::

    from examples.writing_order_reader import SourceOrderManifest

    knowledge.set_writing_narrative_order_reader(
        SourceOrderManifest(work_id=work_id, branch_id="main")
    )

Assumptions and limits:

* chapters are numbered in the order their sources were imported (pass
  ``source_ids`` to pin an explicit order instead);
* ``artifact_id`` and ``event_id`` are synthesized identity pins for this
  checkout, not prose artifacts from the writing system;
* the manifest hash changes when the source set changes, which invalidates
  older parse receipts the same way a writer-side manifest edit would.

Keep this reader opt-in: importing a module must never silently change how
narrative positions are resolved.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Mapping, Sequence


_SOURCE_QUERY = """
    SELECT s.id, s.title, s.created_at, current.snapshot_id, snapshot.content_hash
    FROM source AS s
    JOIN source_current_snapshot AS current ON current.source_id = s.id
    JOIN source_snapshot AS snapshot ON snapshot.id = current.snapshot_id
    WHERE s.work_id = ? AND s.space = 'writing' AND s.deleted_at IS NULL
    ORDER BY s.created_at, s.rowid
"""


class SourceOrderManifest:
    """A verified manifest whose narrative order is the source import order."""

    def __init__(
        self,
        *,
        work_id: str,
        branch_id: str = "main",
        source_ids: Sequence[str] | None = None,
    ) -> None:
        if not work_id.strip():
            raise ValueError("work_id must not be blank")
        if not branch_id.strip():
            raise ValueError("branch_id must not be blank")
        self.work_id = work_id
        self.branch_id = branch_id
        self.source_ids = list(source_ids) if source_ids is not None else None

    def get_order_in_connection(
        self,
        connection: sqlite3.Connection,
        work_id: str,
        branch_id: str,
        revision: int | None = None,
        purpose: str | None = None,
    ) -> Mapping[str, Any]:
        if work_id != self.work_id or branch_id != self.branch_id:
            raise ValueError("reader was configured for a different work or branch")
        rows = list(connection.execute(_SOURCE_QUERY, (work_id,)))
        if self.source_ids is not None:
            by_id = {str(row[0]): row for row in rows}
            missing = [source_id for source_id in self.source_ids if source_id not in by_id]
            if missing:
                raise ValueError(f"pinned sources are missing from the work: {missing}")
            rows = [by_id[source_id] for source_id in self.source_ids]
        if not rows:
            raise ValueError("no current writing sources exist for this work")

        entries: list[dict[str, Any]] = []
        for ordinal, (source_id, title, created_at, snapshot_id, content_hash) in enumerate(
            rows, start=1
        ):
            artifact_id = f"source-order-{source_id}"
            pin = {
                "work_id": work_id,
                "branch_id": branch_id,
                "source_id": str(source_id),
                "snapshot_id": str(snapshot_id),
                "content_hash": str(content_hash),
                "artifact_id": artifact_id,
                "artifact_revision": 1,
                "artifact_hash": str(content_hash),
                "event_id": ordinal,
                "chapter_title": str(title),
            }
            entries.append({
                "ordinal": ordinal,
                "chapter_id": str(source_id),
                "chapter_order": ordinal * 2,
                "adopted_head": {
                    "adopted_artifact_id": artifact_id,
                    "adopted_revision": 1,
                    "adopted_hash": str(content_hash),
                    "lifecycle_event_id": ordinal,
                },
                "source_pin": pin,
            })

        manifest_hash = hashlib.sha256(
            json.dumps(
                entries,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return {
            "work_id": work_id,
            "branch_id": branch_id,
            "manifest_status": "verified",
            "complete": True,
            "revision": 1,
            "manifest_hash": manifest_hash,
            "entries": entries,
        }
