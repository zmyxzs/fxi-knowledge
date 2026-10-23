from __future__ import annotations

import hashlib

from fxi_novel.knowledge.event_canonicalization import derive_event_canonicalization


_HASH = hashlib.sha256(b"manifest").hexdigest()
_CONTENT = hashlib.sha256(b"content").hexdigest()


def _position(source_id: str, ordinal: int, offset: int, *, manifest_hash: str = _HASH) -> dict:
    return {
        "domain": "narrative_order",
        "space": "learning",
        "work_id": "work-1",
        "branch_id": None,
        "manifest_revision": 4,
        "manifest_hash": manifest_hash,
        "manifest_coordinate_status": "verified",
        "source_id": source_id,
        "snapshot_id": f"snapshot-{source_id}",
        "content_hash": _CONTENT,
        "chapter_title": source_id,
        "narrative_ordinal": ordinal,
        "offset": offset,
        "offset_unit": "unicode_codepoint",
        "coordinate_basis": "verified",
    }


def test_canonical_representative_uses_verified_manifest_order_and_offset():
    result = derive_event_canonicalization(
        canonical_ids_by_item={"late": "canonical-late", "early": "canonical-early", "other": "canonical-other"},
        mention_ids_by_item={"late": ["late"], "early": ["early", "alias"], "other": ["other"]},
        accepted_edges=[("late", "early")],
        positions_by_item={
            "late": _position("chapter-9", 9, 10),
            "early": _position("chapter-2", 2, 90),
            "other": _position("chapter-2", 2, 12),
        },
    )

    assert result["available"] is True
    assert result["canonical_event_id_map"] == {
        "canonical-late": "canonical-early",
        "canonical-early": "canonical-early",
        "canonical-other": "canonical-other",
    }
    assert result["components"] == [["other"], ["early", "late"]]
    assert result["mention_ids_by_item"]["late"] == ["early", "alias", "late"]


def test_canonicalization_is_unavailable_when_positions_mix_manifests():
    result = derive_event_canonicalization(
        canonical_ids_by_item={"a": "event-a", "b": "event-b"},
        mention_ids_by_item={"a": ["a"], "b": ["b"]},
        accepted_edges=[("a", "b")],
        positions_by_item={
            "a": _position("chapter-a", 0, 10),
            "b": _position("chapter-b", 1, 20, manifest_hash=hashlib.sha256(b"other").hexdigest()),
        },
    )

    assert result == {
        "available": False,
        "reason": "different_verified_manifest_identity",
        "canonical_event_id_map": {},
        "canonical_event_id_by_item": {},
        "components": [],
        "mention_ids_by_item": {},
    }


def test_canonicalization_is_unavailable_when_a_component_position_is_missing():
    result = derive_event_canonicalization(
        canonical_ids_by_item={"a": "event-a", "b": "event-b"},
        mention_ids_by_item={"a": ["a"], "b": ["b"]},
        accepted_edges=[("a", "b")],
        positions_by_item={"a": _position("chapter-a", 0, 10)},
    )

    assert result["available"] is False
    assert result["reason"] == "verified_manifest_position_missing"
    assert result["canonical_event_id_map"] == {}
