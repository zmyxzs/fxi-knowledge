"""Immutable event candidate and approved-pair audit serialization."""

from __future__ import annotations

from typing import Any

from fxi_novel.knowledge.event_state import EventIdentityReviewRecord, TypedEvent

def _audit_candidate_refs(
    pairs: dict[str, dict[str, Any]],
    mentions: dict[str, dict[str, Any]],
    scope: dict[str, Any],
    *,
    positions_by_item: dict[str, dict[str, Any]],
    canonicalization_status: str,
    event_repartition_requests: list[dict[str, Any]] | None = None,
    event_rows: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    candidate_refs: list[dict[str, Any]] = []
    paired_items: set[str] = set()
    involved_canonical_ids: set[str] = set()

    def serialize(
        item_id: str,
        *,
        pair_ref: str | None,
        side: str,
        candidate_retrieval_method: str | None = None,
        embedding_identity: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        mention = mentions[item_id]
        event: TypedEvent = mention["event"]
        position = positions_by_item.get(item_id)
        return {
            "role": "target" if mention["is_batch_candidate"] else "prior_published",
            "pair_ref": pair_ref,
            "side": side,
            "candidate_retrieval_method": candidate_retrieval_method,
            "embedding_identity": embedding_identity,
            "item_id": str(mention["item_id"]),
            "version_id": str(mention["version_id"]),
            "source_id": str(mention["source_id"]),
            "snapshot_id": str(mention["snapshot_id"]),
            "content_hash": str(mention["content_hash"]),
            "receipt_id": mention.get("receipt_id"),
            "generation": mention.get("generation"),
            "branch_id": scope["branch_id"],
            "canonicalization_status": canonicalization_status,
            "narrative_position": position,
            "evidence_refs": [
                ref.model_dump(mode="json") for ref in event.evidence_refs
            ],
            "entity_identity_pins": [
                mention["entity_identity_pins"][entity_id]
                for entity_id in sorted(mention.get("entity_identity_pins", {}))
            ],
            **({"event_repartition_requests": event_repartition_requests}
               if event_repartition_requests else {}),
        }

    for pair_ref, pair in pairs.items():
        for side, item_id in (
            ("target", pair["target_item_id"]),
            ("candidate", pair["candidate_item_id"]),
        ):
            item_id = str(item_id)
            paired_items.add(item_id)
            involved_canonical_ids.add(mentions[item_id]["event"].canonical_event_id)
            candidate_refs.append(serialize(
                item_id,
                pair_ref=pair_ref,
                side=side,
                candidate_retrieval_method=pair.get("candidate_retrieval_method"),
                embedding_identity=pair.get("embedding_identity"),
            ))
    context_ids = sorted(
        item_id for item_id, mention in mentions.items()
        if (
            item_id not in paired_items
            and mention["event"].canonical_event_id in involved_canonical_ids
        )
    )
    for item_id in context_ids:
        candidate_refs.append({
            **serialize(item_id, pair_ref=None, side="component_context"),
            "component_context": True,
        })
    plans = event_repartition_requests or []
    dependency_item_ids = {
        str(remap["dependent_item_id"])
        for plan in plans
        for remap in plan.get("dependency_remaps", [])
    }
    referenced_items = {str(ref.get("item_id")) for ref in candidate_refs}
    for item_id in sorted(dependency_item_ids - referenced_items):
        row = (event_rows or {}).get(item_id)
        if row is None or not row.get("is_batch_candidate"):
            raise ValueError("event dependency candidate is not in the exact active batch scope")
        content = row.get("content")
        if not isinstance(content, dict):
            raise ValueError("event dependency candidate content is unavailable")
        evidence_refs_by_id = {
            str(reference["id"]): reference
            for payload_name in ("typed_event", "typed_quantity")
            for payload in [content.get(payload_name)]
            if isinstance(payload, dict)
            for reference in payload.get("evidence_refs", [])
            if isinstance(reference, dict) and reference.get("id")
        }
        expected_evidence_ids = {
            evidence_id
            for plan in plans
            for remap in plan.get("dependency_remaps", [])
            if remap["dependent_item_id"] == item_id
            for evidence_id in remap["evidence_ref_ids"]
        }
        if not expected_evidence_ids.issubset(evidence_refs_by_id):
            raise ValueError("event dependency reference evidence is outside its exact candidate")
        candidate_refs.append({
            "role": "target",
            "pair_ref": None,
            "side": "dependency_context",
            "candidate_retrieval_method": None,
            "embedding_identity": None,
            "item_id": item_id,
            "version_id": str(row["version_id"]),
            "source_id": str(row["source_id"]),
            "snapshot_id": str(row["snapshot_id"]),
            "content_hash": str(row["content_hash"]),
            "receipt_id": str(row["receipt_id"]),
            "generation": int(row["generation"]),
            "branch_id": scope["branch_id"],
            "canonicalization_status": canonicalization_status,
            "narrative_position": None,
            "evidence_refs": [
                evidence_refs_by_id[value] for value in sorted(expected_evidence_ids)
            ],
            "dependency_context": True,
            "entity_identity_pins": [],
            "event_repartition_requests": plans,
        })
    return candidate_refs

def _approved_same_occurrence_pairs(
    decisions: list[dict[str, Any]],
    mentions: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    approved: list[dict[str, Any]] = []
    for decision in decisions:
        record: EventIdentityReviewRecord = decision["record"]
        if record.relation != "same_occurrence" or record.status != "confirmed":
            continue
        target_id = str(decision["target_item_id"])
        candidate_id = str(decision["candidate_item_id"])
        approved.append({
            "target_item_id": target_id,
            "target_version_id": mentions[target_id]["version_id"],
            "candidate_item_id": candidate_id,
            "candidate_version_id": mentions[candidate_id]["version_id"],
            "review_id": record.review_id,
            "evidence_ref_ids": sorted(ref.id for ref in record.evidence_refs),
        })
    return approved

