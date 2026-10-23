"""Revision-bound continuation cursors shared by bounded knowledge reads."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import sqlite3
from collections.abc import Mapping
from typing import Any


def query_scope_hash(value: Any) -> str:
    """Return a stable digest for the exact structured query and resolved scope."""
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def read_query_dataset_revision(connection: sqlite3.Connection) -> str:
    """Read source and graph revisions that bind one structured query snapshot."""
    try:
        row = connection.execute(
            "SELECT revision, knowledge_item_revision FROM embedding_read_cache_revision WHERE singleton = 1"
        ).fetchone()
    except sqlite3.Error as exc:
        raise ValueError("knowledge query dataset revision is unavailable") from exc
    if row is None:
        raise ValueError("knowledge query dataset revision is unavailable")
    revisions = tuple(row)
    if (
        len(revisions) != 2
        or any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in revisions)
    ):
        raise ValueError("knowledge query dataset revision is invalid")
    return f"{revisions[0]}:{revisions[1]}"


def encode_query_cursor(
    *, route: str, scope_hash: str, revision: str, position: Mapping[str, Any],
) -> str:
    """Encode a compact, validated cursor for one exact scope and dataset revision."""
    if not route or len(route) > 40 or len(scope_hash) != 64:
        raise ValueError("query cursor identity is invalid")
    if not _valid_revision(revision):
        raise ValueError("query cursor revision is invalid")
    normalized: dict[str, int | str] = {}
    for key, value in position.items():
        if not isinstance(key, str) or not key or len(key) > 24:
            raise ValueError("query cursor position key is invalid")
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise ValueError("query cursor position is invalid")
        if isinstance(value, int) and value < 0:
            raise ValueError("query cursor position is invalid")
        if isinstance(value, str) and (not value or len(value) > 200):
            raise ValueError("query cursor position is invalid")
        normalized[key] = value
    payload = json.dumps(
        {"v": 1, "r": route, "s": scope_hash, "d": revision, "p": normalized},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    token = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    if len(token) > 500:
        raise ValueError("query cursor exceeds its contract length")
    return token


def inspect_query_cursor(token: str) -> dict[str, Any]:
    """Decode basic cursor claims so the service can select its continuation stage."""
    if not isinstance(token, str) or not token or len(token) > 500:
        raise ValueError("query cursor is invalid")
    try:
        padded = token + "=" * (-len(token) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, TypeError, UnicodeError, binascii.Error, json.JSONDecodeError) as exc:
        raise ValueError("query cursor is invalid") from exc
    if not isinstance(value, dict):
        raise ValueError("query cursor is invalid")
    route, scope_hash, revision, position = (
        value.get("r"), value.get("s"), value.get("d"), value.get("p"),
    )
    if (
        value.get("v") != 1
        or not isinstance(route, str) or not route or len(route) > 40
        or not isinstance(scope_hash, str) or len(scope_hash) != 64
        or not _valid_revision(revision)
        or not isinstance(position, dict) or len(position) > 8
    ):
        raise ValueError("query cursor is invalid")
    for key, item in position.items():
        if not isinstance(key, str) or not key or len(key) > 24:
            raise ValueError("query cursor is invalid")
        if isinstance(item, bool) or not isinstance(item, (int, str)):
            raise ValueError("query cursor is invalid")
        if isinstance(item, int) and item < 0:
            raise ValueError("query cursor is invalid")
        if isinstance(item, str) and (not item or len(item) > 200):
            raise ValueError("query cursor is invalid")
    return {"route": route, "scope_hash": scope_hash, "revision": revision, "position": position}


def decode_query_cursor(
    token: str, *, route: str, scope_hash: str, revision: str,
) -> dict[str, int | str]:
    """Validate all continuation bindings and reject stale dataset snapshots."""
    claims = inspect_query_cursor(token)
    if claims["route"] != route or claims["scope_hash"] != scope_hash:
        raise ValueError("query cursor belongs to a different operation or scope")
    if claims["revision"] != revision:
        raise ValueError("query cursor dataset revision is stale; restart the structured query")
    return claims["position"]


def _valid_revision(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > 64:
        return False
    parts = value.split(":")
    return len(parts) == 2 and all(part.isdigit() for part in parts)


__all__ = [
    "decode_query_cursor", "encode_query_cursor", "inspect_query_cursor",
    "query_scope_hash", "read_query_dataset_revision",
]
