"""Exact source examples are usable without manufacturing a writing method."""

from copy import deepcopy
import hashlib
from typing import Any

from .repository import KnowledgeItemVersionConflictError


def example_profile(item: dict[str, Any]) -> dict[str, Any]:
    content = item["content"]
    stored = content.get("style_example_profile") or {}
    method_profile = (content.get("craft_method_payload") or {}).get("profile") or {}
    return {
        "profile_id": stored.get("profile_id") or method_profile.get("profile_id")
            or f"style-examples:{item['id']}:{item['version']}",
        "source_ids": stored.get("source_ids") or method_profile.get("source_ids")
            or list(dict.fromkeys(row["source_id"] for row in content.get("source_refs", []))),
    }


def example_bindings(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Legacy quotes resolve only against a unique exact pin on this item."""
    if not item:
        return []
    content = item["content"]
    sources = set(example_profile(item)["source_ids"])
    refs = {(row.get("source_id"), row.get("snapshot_id")) for row in content.get("source_refs", [])}
    evidence = {}
    for row in item.get("evidence", []):
        start, end, quote = row.get("start_offset"), row.get("end_offset"), row.get("quote")
        if (row.get("source_id") in sources and (row.get("source_id"), row.get("snapshot_id")) in refs
                and type(start) is int and type(end) is int and start >= 0 and end > start
                and isinstance(quote, str) and len(quote) == end - start
                and hashlib.sha256(quote.encode("utf-8")).hexdigest() == row.get("quote_hash")):
            evidence[row["id"]] = row
    bindings = []
    if "style_example_profile" in content:
        bindings = [{**deepcopy(row), "is_golden": row.get("is_golden", True)}
                    for row in content["style_example_profile"].get("examples") or []
                    if row.get("evidence_id") in evidence]
    for golden in ([] if "style_example_profile" in content else
                   (content.get("author_style_fingerprint") or {}).get("golden_voice_slices", [])):
        quote = str(golden.get("quote") or "").strip()
        matches = [row for row in evidence.values() if quote and row["quote"] == quote]
        if len(matches) == 1:
            bindings.append({"evidence_id": matches[0]["id"], "title": golden.get("title", ""),
                             "cadence_notes": golden.get("cadence_notes", ""), "is_golden": True})
    # Method interpretations can expire while their pinned source passages remain valid.
    # Recover only passage identities, not the old support/counterexample judgment.
    seen = {row["evidence_id"] for row in bindings}
    for method in (content.get("craft_method_payload") or {}).get("methods", []):
        identities = [row.get("evidence_id") for row in method.get("examples", [])]
        identities.extend(method.get("evidence_ids", []))
        identities.extend(method.get("counterevidence_ids", []))
        for identity in identities:
            if identity in evidence and identity not in seen:
                bindings.append({"evidence_id": identity, "is_golden": False})
                seen.add(identity)
    return bindings


def read_style_examples(service: Any, item: dict[str, Any], *, profile_id: str,
                        work_id: str, example_ids: list[str] | None = None) -> dict[str, Any]:
    profile = example_profile(item)
    if profile["profile_id"] != profile_id:
        raise ValueError("the selected example profile does not belong to this study")
    sources = set(profile["source_ids"])
    if not sources or not sources.issubset(service._work_method_source_ids(work_id, sources)):
        raise ValueError("the selected style sources are outside this work's registered references")
    bindings = {row["evidence_id"]: row for row in example_bindings(item)}
    ids = list(bindings) if example_ids is None else example_ids
    if not ids or len(set(ids)) != len(ids) or set(ids) - bindings.keys():
        raise ValueError("selected source examples are unavailable or outside the pinned profile")
    refs = {row["source_id"]: row for row in item["content"].get("source_refs", [])}
    # Validate every profile source, even when this read selects only one sample.
    for source_id in sources:
        source = service.get_source(source_id, content_offset=0, content_limit=1, include_evidence=False)
        ref, snapshot = refs.get(source_id, {}), (source or {}).get("snapshot") or {}
        if (source is None or (source.get("space"), source.get("work_id")) != (item["space"], item.get("work_id"))
                or snapshot.get("id") != ref.get("snapshot_id")
                or snapshot.get("content_hash") != ref.get("content_hash")):
            raise KnowledgeItemVersionConflictError("a selected style source changed or was withdrawn")
    basis = service.style_study_basis_status(item["id"], expected_version=item["version"])
    evidence = {row["id"]: row for row in item.get("evidence", [])}
    examples = []
    for identity in ids:
        row = evidence.get(identity) or {}
        start, end, quote = row.get("start_offset"), row.get("end_offset"), row.get("quote")
        ref = refs.get(row.get("source_id"), {})
        if (row.get("source_id") not in sources or row.get("snapshot_id") != ref.get("snapshot_id")
                or type(start) is not int or type(end) is not int or start < 0 or end <= start
                or not isinstance(quote, str) or not quote or end - start != len(quote)
                or hashlib.sha256(quote.encode("utf-8")).hexdigest() != row.get("quote_hash")):
            raise ValueError("the selected style example has an invalid exact evidence pin")
        exact = service.get_source(row["source_id"], row["snapshot_id"], content_offset=start,
                                   content_limit=end - start, include_evidence=False)
        if (exact is None or exact["snapshot"]["content"] != quote
                or exact["snapshot"]["content_hash"] != ref.get("content_hash")):
            raise ValueError("the selected style example differs from its source snapshot")
        examples.append({"evidence_id": identity, "profile_id": profile_id, "role": "context",
                         "is_golden": bindings[identity]["is_golden"], "text": exact["snapshot"]["content"],
                         "source_id": row["source_id"], "source_title": exact["title"],
                         "snapshot_id": row["snapshot_id"], "content_hash": ref["content_hash"],
                         "start_offset": start, "end_offset": end, "offset_unit": "unicode_codepoint",
                         "quote_hash": row["quote_hash"], "title": bindings[identity].get("title", ""),
                         "cadence_notes": bindings[identity].get("cadence_notes", "") if basis == "current" else ""})
    return {"record_type": "writing_style_examples", "profile_id": profile_id,
            "learning_item_id": item["id"], "learning_item_version": item["version"],
            "learning_item_version_id": item["version_id"], "reuse_scope": "source_bound",
            "style_profile_kind": "author_fingerprint", "study_basis_status": basis,
            "style_profile_summary": (item["content"].get("author_style_fingerprint") or {}).get("summary", "")
                if basis == "current" else "",
            "examples": examples, "scoped_style_example_read": {
                "work_id": work_id, "learning_item_id": item["id"], "learning_item_version": item["version"],
                "profile_id": profile_id, "source_ids": profile["source_ids"],
                "example_ids": ids,
            }}
