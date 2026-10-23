"""Build the per-source authorization consumed by parse publication."""

from __future__ import annotations

from typing import Any

from .models import KNOWLEDGE_GRAPH_SEMANTIC_REVIEW_VERSION

def _checked_versions(item: dict[str, Any]) -> dict[str, str]:
    result = item.get("stages", {}).get("check", {}).get("result")
    if not isinstance(result, dict):
        raise ValueError("resolution authorization requires the completed check stage")
    versions: dict[str, str] = {}
    for unit in result.get("units", []):
        review = unit.get("review") if isinstance(unit, dict) else None
        if not isinstance(review, dict):
            raise ValueError("check stage unit has no semantic review receipt")
        for source in review.get("sources", []):
            if not isinstance(source, dict):
                raise ValueError("check stage source receipt is malformed")
            exact = source.get("candidate_version_ids")
            if exact is None and source.get("status") in {"skipped", "reused"}:
                exact = {}
            if not isinstance(exact, dict):
                raise ValueError("semantic review receipt has no exact candidate version IDs")
            for item_id, version_id in exact.items():
                if not isinstance(item_id, str) or not item_id or not isinstance(version_id, str) or not version_id:
                    raise ValueError("semantic review candidate version pin is invalid")
                previous = versions.setdefault(item_id, version_id)
                if previous != version_id:
                    raise ValueError("semantic review units disagree on a candidate version")
    return versions


def checked_versions_for_sources(items: list[dict[str, Any]]) -> dict[str, str]:
    """Collect immutable check pins for every source in one resolution batch."""
    result: dict[str, str] = {}
    for item in items:
        for item_id, version_id in _checked_versions(item).items():
            previous = result.setdefault(item_id, version_id)
            if previous != version_id:
                raise ValueError("source check stages disagree on a candidate version")
    return result


def _latest_source_candidates(
    knowledge: Any,
    *,
    batch: dict[str, Any],
    item: dict[str, Any],
    folder_id: str | None,
    checked_ids: set[str],
) -> dict[str, dict[str, Any]]:
    if not checked_ids:
        return {}
    rows = knowledge.list_current_graph_candidates_in_folder(
        str(batch["space"]),
        batch.get("work_id"),
        folder_id,
        source_ids=[str(item["source_id"])],
        include_prior_ai_exclusions=True,
        reviewer_version=KNOWLEDGE_GRAPH_SEMANTIC_REVIEW_VERSION,
        include_current_ai_exclusions=True,
    )
    current = {
        str(row["item_id"]): row
        for row in rows
        if str(row.get("item_id")) in checked_ids
        and str(row.get("source_id")) == str(item["source_id"])
        and str(row.get("snapshot_id")) == str(item["snapshot_id"])
    }
    if set(current) != checked_ids:
        missing = sorted(checked_ids - set(current))
        raise ValueError(f"checked candidate versions disappeared before resolution completion: {missing[:8]}")
    return current


def build_source_resolution_authorization(
    knowledge: Any,
    *,
    batch: dict[str, Any],
    item: dict[str, Any],
    batch_checked_versions: dict[str, str],
    folder_id: str | None,
    identity: dict[str, Any],
    event_result: dict[str, Any],
) -> dict[str, Any]:
    """Pin the complete checked source set and resolution audit evidence."""
    checked = _checked_versions(item)
    if any(batch_checked_versions.get(item_id) != version_id for item_id, version_id in checked.items()):
        raise ValueError("source checked versions disagree with the immutable batch check set")
    all_checked_ids = set(batch_checked_versions)
    final_rows = _latest_source_candidates(
        knowledge,
        batch=batch,
        item=item,
        folder_id=folder_id,
        checked_ids=set(checked),
    )
    final = {item_id: str(row["version_id"]) for item_id, row in final_rows.items()}
    # Identity reconciliation processes a folder-wide set of windows. Its
    # changed-ID list therefore spans every source in the batch, while this
    # authorization is deliberately source-local. The publisher checks each
    # complete source candidate map independently, so only intersect here.
    identity_changed_all = {
        str(item_id) for item_id in identity.get("changed_candidate_ids", [])
    }
    if not identity_changed_all <= all_checked_ids:
        raise ValueError("identity resolution changed a candidate outside the batch check set")
    # Recovery may start at an authorized intermediate version. The repository
    # verifies every transition from the original immutable check pin below.

    # Event reconciliation can likewise return updates for several selected
    # sources in one folder-level result. Keep this source's changed rows only;
    # exact event attempt refs and source pins are still validated below.
    event_changed_all = {
        str(item_id) for item_id in event_result.get("updated_candidate_ids", [])
    }
    if not event_changed_all <= all_checked_ids:
        raise ValueError("event resolution changed a candidate outside the batch check set")
    event_changed = event_changed_all & set(checked)
    event_versions = event_result.get("updated_candidate_versions") or {}
    for item_id in event_changed:
        if not isinstance(event_versions, dict) or not isinstance(event_versions.get(item_id), str):
            raise ValueError("event update has no exact pre-update candidate version")
    requested_work_id = batch.get("requested_work_id") or batch.get("work_id")
    source_id = str(item["source_id"])
    # The repository owns proof recovery from immutable history. Per-call
    # private fields are insufficient once an earlier invocation saved a prefix.
    authorization = {
        "source_id": source_id,
        "snapshot_id": str(item["snapshot_id"]),
        "receipt_id": str(item["receipt_id"]),
        "generation": int(item["generation"]),
        "requested_work_id": requested_work_id,
        "physical_work_id": batch.get("work_id"),
        "folder_id": folder_id,
        "manifest_pin": batch.get("requested_manifest_pin"),
        "checked_candidate_versions": checked,
        "final_candidate_versions": final,
        "attempt_ids": [],
        "deterministic_singleton_receipts": [],
        "canonical_event_id_map": {},
    }
    return knowledge.verify_knowledge_parse_resolution_authorization(
        batch_id=str(batch["batch_id"]), source_id=source_id, authorization=authorization,
    )
