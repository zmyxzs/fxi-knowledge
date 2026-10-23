"""Vector-assisted, evidence-pinned occurrence reconciliation during parsing."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from fxi_novel.knowledge.event_reconciliation_response import (
    parse_event_semantic_review_response,
)
from fxi_novel.knowledge.event_reconciliation_result_support import empty_result
from fxi_novel.knowledge.event_reconciliation_candidate_audit import (
    _approved_same_occurrence_pairs as approved_same_occurrence_pairs,
    _audit_candidate_refs as audit_candidate_refs,
)
from fxi_novel.knowledge.event_entity_identity import (
    event_entity_reference_ids,
    verified_entity_identity_aliases,
)
from fxi_novel.knowledge.event_state import EventIdentityReviewRecord, TypedEvent
from fxi_novel.knowledge.event_reconciliation_scoped_candidates import (
    complete_published_scope_event_candidates,
    candidate_scope_metadata,
    selected_current_batch_event_candidates,
    selected_scope_event_candidates,
)
from fxi_novel.knowledge.event_reconciliation_manifest_positions import (
    resolve_verified_event_positions,
)
from fxi_novel.knowledge.event_reconciliation_decisions import (
    normalize_and_validate_event_decisions,
    unresolve_same_occurrence_without_manifest,
)
from fxi_novel.knowledge.event_reconciliation_validation import (
    sha256_json as _sha256_json,
)
from fxi_novel.knowledge.event_reconciliation_review_execution import (
    execute_review_pairs,
)
from fxi_novel.knowledge.event_reconciliation_partial_replay import (
    recover_partial_event_review_groups,
)
from .event_reconciliation_preflight import PREFLIGHT_POLICY, partition_event_review_pairs
from fxi_novel.knowledge.event_reconciliation_review_request import (
    EventReconciliationReviewRequestMixin,
)
from fxi_novel.knowledge.event_reconciliation_vector_candidates import (
    build_event_identity_coverage,
    retrieve_published_event_pairs,
)
from fxi_novel.knowledge.models_base import knowledge_graph_identity_source_content_hash
from fxi_novel.knowledge.event_canonicalization import derive_event_canonicalization
from fxi_novel.knowledge.event_repartition import (
    normalize_event_repartition_requests,
    validate_event_repartition_decisions,
    validate_event_repartition_requests,
)
from fxi_novel.models.gateway import ModelGateway


_DENSE_QUERY_BATCH_SIZE = 5
_CONTRACT_VERSION = "event-occurrence-reconciliation.v5"




class EventVectorReconciliationService(EventReconciliationReviewRequestMixin):
    """Resolve repeated event mentions without guessing from names or wording."""

    def __init__(
        self,
        knowledge: Any,
        model_gateway: ModelGateway,
        prompt_path: Path,
        *,
        top_k: int = 20,
        review_concurrency: int = 10,
        stage_audit_runner: Any | None = None,
    ) -> None:
        if isinstance(top_k, bool) or not 1 <= top_k <= 100:
            raise ValueError("event candidate top_k must be between 1 and 100")
        if isinstance(review_concurrency, bool) or not 1 <= review_concurrency <= 20:
            raise ValueError("event review concurrency must be between 1 and 20")
        self.knowledge = knowledge
        self.model_gateway = model_gateway
        self.stage_audit_runner = stage_audit_runner
        self.top_k = top_k
        self.review_concurrency = review_concurrency
        self.prompt = Path(prompt_path).read_text(encoding="utf-8").strip()
        if not self.prompt:
            raise ValueError("event reconciliation prompt must not be empty")
        self.prompt_hash = hashlib.sha256(self.prompt.encode("utf-8")).hexdigest()
        self.contract_version = _CONTRACT_VERSION

    @property
    def contract_fingerprint(self) -> str:
        """Include prompt, routes, candidate bound, and deterministic compatibility policy."""
        embedding = getattr(self.knowledge.embedding_projection, "route_identity", None)
        chat_config = getattr(self.model_gateway, "config", None)
        routes = []
        if chat_config is not None:
            routes = [
                {
                    "enabled": route.enabled,
                    "base_url": route.base_url.rstrip("/"),
                    "model": route.model,
                    "timeout_seconds": route.timeout_seconds,
                    "reasoning_effort": route.reasoning_effort,
                    "max_output_tokens": route.max_output_tokens,
                }
                for route in (chat_config.chat, *chat_config.chat_fallbacks)
            ]
        material = {
            "version": self.contract_version,
            "event_compatibility_policy": "pinned-referent-ledger-v1",
            "preflight_policy": PREFLIGHT_POLICY,
            "prompt_hash": self.prompt_hash,
            "top_k": self.top_k,
            "embedding_route_fingerprint": getattr(embedding, "fingerprint", None),
            "chat_routes": routes,
        }
        return _sha256_json(material)

    async def reconcile_parse_batch_folder(
        self,
        *,
        batch_id: str,
        space: str,
        work_id: str | None,
        requested_work_id: str | None,
        folder_id: str | None,
        branch_id: str | None,
        source_snapshot_pins: dict[str, str],
        checked_version_ids: dict[str, str] | None = None,
        event_repartition_requests: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Review batch events against current published events in the pinned folder."""
        if not source_snapshot_pins:
            raise ValueError("event reconciliation requires active source snapshot pins")
        repository = self.knowledge.repository
        rows = await asyncio.to_thread(
            repository.list_event_reconciliation_candidates,
            batch_id=batch_id,
            space=space,
            work_id=work_id,
            requested_work_id=requested_work_id,
            folder_id=folder_id,
            branch_id=branch_id,
            source_snapshot_pins=source_snapshot_pins,
        )
        if rows.get("truncated"):
            raise RuntimeError(
                "event reconciliation candidate read exceeded 20,000 rows; refusing partial identity updates"
            )

        if checked_version_ids is not None and any(
            not isinstance(item_id, str) or not item_id.strip()
            or not isinstance(version_id, str) or not version_id.strip()
            for item_id, version_id in checked_version_ids.items()
        ):
            raise ValueError("event reconciliation check-stage version pins are malformed")
        requested_repartitions = normalize_event_repartition_requests(
            event_repartition_requests,
        )

        scope = {"space": space, "work_id": work_id, "branch_id": branch_id}
        event_rows: dict[str, dict[str, Any]] = {}
        events: dict[str, TypedEvent] = {}
        entity_identity_pins = rows.get("entity_identity_pins")
        if not isinstance(entity_identity_pins, dict):
            entity_identity_pins = {}
        entity_identity_aliases = verified_entity_identity_aliases(entity_identity_pins)
        targets: list[str] = []
        for row in rows.get("candidates", []):
            content = row.get("content")
            if not isinstance(content, dict):
                raise ValueError("event reconciliation candidate content is invalid")
            event_content = content.get("typed_event")
            quantity_content = content.get("typed_quantity")
            if not isinstance(event_content, dict) and not isinstance(quantity_content, dict):
                continue
            item_id = str(row["item_id"])
            event_rows[item_id] = row
            if not isinstance(event_content, dict):
                if not isinstance(quantity_content, dict):
                    continue
                for reference in quantity_content.get("evidence_refs", []):
                    if not isinstance(reference, dict) or any(
                        reference.get(key) != value for key, value in scope.items()
                    ):
                        raise ValueError("quantity evidence escaped the exact event correction scope")
                continue
            event = TypedEvent.model_validate(event_content)
            if event.id != item_id or event.branch_id != branch_id:
                raise ValueError("event candidate identity escaped its pinned branch")
            for reference in event.evidence_refs:
                if any(getattr(reference, key) != value for key, value in scope.items()):
                    raise ValueError("event evidence escaped the exact reconciliation scope")
            if row.get("is_batch_candidate") and checked_version_ids is not None:
                checked_version_id = checked_version_ids.get(item_id)
                if not isinstance(checked_version_id, str) or not checked_version_id.strip():
                    raise ValueError(
                        "batch event target has no immutable check-stage version pin"
                    )
            events[item_id] = event
            if row.get("is_batch_candidate"):
                targets.append(item_id)

        repartition_requests = validate_event_repartition_requests(
            requested_repartitions,
            events=events,
            event_rows=event_rows,
            evidence_scope=scope,
        )
        scope_metadata = candidate_scope_metadata(
            space=space,
            work_id=work_id,
            requested_work_id=requested_work_id,
            folder_id=folder_id,
        )
        if not targets:
            result = self._empty_result(rows, len(events))
            result["candidate_retrieval_method"] = "not_applied"
            result.update(scope_metadata)
            return result
        if not events:
            raise RuntimeError("event batch reports targets but no validated typed events")
        if self.stage_audit_runner is None:
            raise RuntimeError("event reconciliation has no durable parse-stage audit runner")
        batch_record = self.knowledge.get_knowledge_parse_batch(batch_id)
        if not isinstance(batch_record, dict) or batch_record.get("status") != "running":
            raise RuntimeError("event reconciliation batch is no longer active")
        batch_sources = {
            str(item["source_id"]): item
            for item in batch_record.get("sources", [])
            if isinstance(item, dict) and item.get("source_id")
        }
        selected_audit_pins: list[dict[str, Any]] = []
        for source_id, snapshot_id in sorted(source_snapshot_pins.items()):
            batch_item = batch_sources.get(source_id)
            row = next((value for value in event_rows.values() if value["source_id"] == source_id), None)
            if batch_item is None or row is None or not row.get("is_batch_candidate"):
                continue
            if batch_item.get("snapshot_id") != snapshot_id:
                raise ValueError("event reconciliation source differs from batch snapshot")
            if int(batch_item.get("generation", 0) or 0) != int(row.get("generation", 0) or 0):
                raise ValueError("event candidate generation differs from its active batch item")
            selected_audit_pins.append({
                "source_id": source_id,
                "snapshot_id": snapshot_id,
                "receipt_id": str(row["receipt_id"]),
                "generation": int(row["generation"]),
                "ordinal": int(batch_item["ordinal"]),
            })
        if not selected_audit_pins:
            raise RuntimeError("event reconciliation has no selected target source receipt pins")

        mentions = {
            item_id: {
                "item_id": item_id,
                "version_id": str(event_rows[item_id]["version_id"]),
                "scope": scope,
                "event": event,
                "source_id": str(event_rows[item_id]["source_id"]),
                "snapshot_id": str(event_rows[item_id]["snapshot_id"]),
                "content_hash": str(event_rows[item_id]["content_hash"]),
                "receipt_id": event_rows[item_id].get("receipt_id"),
                "generation": event_rows[item_id].get("generation"),
                "is_batch_candidate": bool(event_rows[item_id].get("is_batch_candidate")),
                "entity_identity_pins": {
                    entity_id: entity_identity_pins.get(
                        entity_id, {"status": "unavailable", "item_id": entity_id},
                    )
                    for entity_id in event_entity_reference_ids(event.model_dump(mode="json"))
                },
            }
            for item_id, event in events.items()
        }
        candidate_universe = [
            {
                "item_id": item_id,
                "version_id": mentions[item_id]["version_id"],
                "source_id": mentions[item_id]["source_id"],
                "snapshot_id": mentions[item_id]["snapshot_id"],
                "content_hash": mentions[item_id]["content_hash"],
                "evidence_refs": sorted(
                    [reference.model_dump(mode="json") for reference in events[item_id].evidence_refs],
                    key=lambda value: (
                        value["source_id"], value["snapshot_id"],
                        value["start_offset"], value["end_offset"], value["id"],
                    ),
                ),
            }
            for item_id in sorted(events)
        ]
        candidate_universe_hash = _sha256_json(candidate_universe)
        candidate_source_snapshot_pins = [
            {"source_id": source_id, "snapshot_id": snapshot_id, "content_hash": content_hash}
            for source_id, snapshot_id, content_hash in sorted({
                (
                    str(mention["source_id"]),
                    str(mention["snapshot_id"]),
                    str(mention["content_hash"]),
                )
                for mention in mentions.values()
            })
        ]
        positions_by_item, canonicalization_status, canonicalization_reason = await (
            resolve_verified_event_positions(
                self.knowledge,
                mentions=mentions,
                space=space,
                requested_work_id=requested_work_id,
                expected_manifest_pin=batch_record.get("requested_manifest_pin"),
            )
        )
        if repartition_requests and canonicalization_status != "verified":
            raise ValueError(
                "event repartition requires one current verified narrative manifest; the group remains unchanged"
            )
        max_scoped_candidates = max(
            (
                sum(
                    candidate_id != target_id
                    and candidate.canonical_event_id != events[target_id].canonical_event_id
                    for candidate_id, candidate in events.items()
                )
                for target_id in targets
            ),
            default=0,
        )
        prior_ids = sorted(item_id for item_id in events if item_id not in set(targets))
        current_pair_coverage = {
            "possible_candidate_pair_count": 0,
            "selected_candidate_pair_count": 0,
            "unreviewed_candidate_pair_count": 0,
        }
        if max_scoped_candidates <= self.top_k:
            retrieval_method = "selected_source_pairwise"
            candidate_selection_mode = "complete_scope_enumeration"
            hits_by_target, _pairwise_coverage = selected_scope_event_candidates(
                targets=targets,
                events=events,
                event_rows=event_rows,
                top_k=self.top_k,
            )
            embedding_identity = None
            dense_coverage = {
                "retrieval_mode": "complete_event_candidate_enumeration",
                "coverage_status": "complete_event_candidate_scope",
                "vector_status": "not_used",
                "query_count": len(targets),
                "event_candidate_count": len(events),
                "event_source_count": len({item["source_id"] for item in event_rows.values()}),
                "vector_query_count": 0,
            }
        else:
            # Private, checked candidates are compared by bounded enumeration. They
            # cannot have published vectors yet. Existing published candidates are
            # enumerated completely while small, then retrieved as typed events.
            current_hits, current_pair_coverage = selected_current_batch_event_candidates(
                targets=targets,
                events=events,
                event_rows=event_rows,
                top_k=self.top_k,
            )
            if len(prior_ids) <= self.top_k:
                prior_hits = complete_published_scope_event_candidates(
                    targets=targets,
                    published_candidate_ids=prior_ids,
                    events=events,
                    event_rows=event_rows,
                )
                embedding_identity = None
                dense_coverage = {
                    "retrieval_mode": "complete_published_event_enumeration",
                    "coverage_status": "complete",
                    "vector_status": "not_used",
                    "query_count": len(targets),
                    "event_candidate_count": len(events),
                    "published_event_candidate_count": len(prior_ids),
                    "event_source_count": len({event_rows[value]["source_id"] for value in prior_ids}),
                    "vector_query_count": 0,
                }
                candidate_selection_mode = "bounded_current_pairs_plus_complete_published_enumeration"
            else:
                prior_hits, embedding_identity, dense_coverage = await retrieve_published_event_pairs(
                    self.knowledge,
                    top_k=self.top_k,
                    targets=targets,
                    published_candidate_ids=prior_ids,
                    events=events,
                    event_rows=event_rows,
                )
                candidate_selection_mode = "bounded_current_pairs_plus_published_event_vectors"
            hits_by_target = {
                target_id: [*current_hits.get(target_id, []), *prior_hits.get(target_id, [])]
                for target_id in targets
            }
            methods = {
                str(hit["candidate_retrieval_method"])
                for hits in hits_by_target.values() for hit in hits
            }
            retrieval_method = (
                next(iter(methods)) if len(methods) == 1
                else "mixed" if methods
                else "selected_source_pairwise"
            )
            dense_coverage.update({
                "event_candidate_count": len(events),
                "published_event_candidate_count": len(prior_ids),
                "event_source_count": len({event_rows[value]["source_id"] for value in prior_ids}),
                "current_pair_coverage": current_pair_coverage,
                "pair_coverage_status": "bounded_candidate_review",
            })
        forced_repartition_pairs: set[tuple[str, str]] = set()
        if repartition_requests:
            for repartition in repartition_requests.values():
                member_ids = repartition["member_item_ids"]
                for index, left in enumerate(member_ids):
                    if left not in targets:
                        raise ValueError("event repartition member is not a selected batch target")
                    for right in member_ids[index + 1:]:
                        forced_repartition_pairs.add(tuple(sorted((left, right))))
                        existing = {
                            str(hit["item_id"]): hit for hit in hits_by_target.get(left, [])
                        }
                        existing.setdefault(right, {
                            "item_id": right,
                            "rank": len(existing) + 1,
                            "score": 1.0,
                            "candidate_retrieval_method": "selected_source_pairwise",
                            "embedding_identity": None,
                        })
                        hits_by_target[left] = list(existing.values())
                        reverse = {
                            str(hit["item_id"]): hit for hit in hits_by_target.get(right, [])
                        }
                        reverse.setdefault(left, {
                            "item_id": left,
                            "rank": len(reverse) + 1,
                            "score": 1.0,
                            "candidate_retrieval_method": "selected_source_pairwise",
                            "embedding_identity": None,
                        })
                        hits_by_target[right] = list(reverse.values())
        possible_pairs = {
            tuple(sorted((target_id, candidate_id)))
            for target_id in targets
            for candidate_id, candidate in events.items()
            if (
                candidate_id != target_id
                and candidate.canonical_event_id != events[target_id].canonical_event_id
            )
        }
        possible_pairs.update(forced_repartition_pairs)
        selected_pairs = {
            tuple(sorted((target_id, str(hit["item_id"]))))
            for target_id, hits in hits_by_target.items()
            for hit in hits
        }
        pairwise_coverage = {
            "possible_candidate_pair_count": len(possible_pairs),
            "selected_candidate_pair_count": len(selected_pairs),
            "unreviewed_candidate_pair_count": len(possible_pairs - selected_pairs),
        }
        dense_coverage.update(pairwise_coverage)
        pair_rows = self._unique_candidate_pairs(targets, hits_by_target, mentions)
        pair_rows = [
            {
                **pair,
                "target_version_id": mentions[pair["target_item_id"]]["version_id"],
                "candidate_version_id": mentions[pair["candidate_item_id"]]["version_id"],
                "target_event": events[pair["target_item_id"]],
                "candidate_event": events[pair["candidate_item_id"]],
            }
            for pair in pair_rows
        ]
        retrieved_pair_count = len(pair_rows)
        pair_rows, rule_exclusions = partition_event_review_pairs(
            pair_rows,
            mentions=mentions,
            scope=scope,
            positions_by_item=positions_by_item,
            canonicalization_status=canonicalization_status,
            entity_alias_to_canonical=entity_identity_aliases,
            forced_pairs=forced_repartition_pairs,
        )
        unresolved_review_groups: list[dict[str, Any]] = []
        try:
            semantic_pairs, model_attempt_ids = await self._review_pairs(
                pair_rows,
                scope,
                embedding_identity,
                batch_id=batch_id,
                selected_source_pins=selected_audit_pins,
                manifest_pin=batch_record.get("requested_manifest_pin"),
                mentions=mentions,
                positions_by_item=positions_by_item,
                canonicalization_status=canonicalization_status,
                candidate_retrieval_method=retrieval_method,
                batch_max_concurrency=int(batch_record.get("max_concurrency") or self.review_concurrency),
                event_repartition_requests=list(repartition_requests.values()),
                event_rows=event_rows,
            )
        except Exception as exc:
            if repartition_requests:
                raise RuntimeError(
                    "approved event repartition review did not complete; the original canonical group remains published"
                ) from exc
            try:
                recovered = await recover_partial_event_review_groups(
                    self,
                    pair_rows,
                    scope,
                    batch_id=batch_id,
                    selected_source_pins=selected_audit_pins,
                    manifest_pin=batch_record.get("requested_manifest_pin"),
                    mentions=mentions,
                    positions_by_item=positions_by_item,
                    canonicalization_status=canonicalization_status,
                    candidate_retrieval_method=retrieval_method,
                    event_repartition_requests=list(repartition_requests.values()),
                    event_rows=event_rows,
                )
            except Exception as recovery_exc:
                raise RuntimeError(
                    f"event review execution failed: {type(exc).__name__}: {str(exc)[:700]}; "
                    f"saved-result recovery failed: {type(recovery_exc).__name__}: {str(recovery_exc)[:300]}"
                ) from exc
            semantic_pairs = recovered["reviewed"]
            model_attempt_ids = recovered["validated_attempt_ids"]
            unresolved_review_groups = recovered["unresolved_review_groups"]
            if not unresolved_review_groups:
                raise RuntimeError(
                    f"event review failed without a localized unresolved group: {type(exc).__name__}: {exc}"
                ) from exc

        decisions, same_edges = normalize_and_validate_event_decisions(
            semantic_pairs,
            events,
            entity_alias_to_canonical=entity_identity_aliases,
            repartition_partitions_by_canonical={
                canonical_id: repartition["partitions"]
                for canonical_id, repartition in repartition_requests.items()
            },
        )
        if repartition_requests:
            validate_event_repartition_decisions(
                list(repartition_requests.values()), decisions,
            )
        unresolved_same_occurrence_count = 0
        if canonicalization_status == "verified":
            canonicalization = derive_event_canonicalization(
                canonical_ids_by_item={
                    item_id: event.canonical_event_id for item_id, event in events.items()
                },
                mention_ids_by_item={
                    item_id: event.mention_ids for item_id, event in events.items()
                },
                accepted_edges=same_edges,
                positions_by_item=positions_by_item,
                event_ids_by_item={
                    item_id: event.id for item_id, event in events.items()
                },
                repartition_partitions_by_canonical={
                    canonical_id: repartition["partitions"]
                    for canonical_id, repartition in repartition_requests.items()
                },
                repartition_alias_owners_by_canonical={
                    canonical_id: {
                        remap["reference_id"]: remap["target_member_item_id"]
                        for remap in repartition.get("dependency_remaps", [])
                        if remap["reference_kind"] == "mention_alias"
                    }
                    for canonical_id, repartition in repartition_requests.items()
                },
            )
            if not canonicalization["available"]:
                canonicalization_status = "unavailable"
                canonicalization_reason = str(canonicalization["reason"])
                if repartition_requests:
                    raise ValueError(
                        "event repartition lost its verified manifest; the old group remains unchanged"
                    )
            else:
                groups = canonicalization["components"]
                canonical_map = canonicalization["canonical_event_id_map"]
                canonical_id_by_item = canonicalization["canonical_event_id_by_item"]
                mention_ids_by_item = canonicalization["mention_ids_by_item"]
        if canonicalization_status != "verified":
            unresolved_same_occurrence_count = sum(
                decision["record"].relation == "same_occurrence" for decision in decisions
            )
            decisions = unresolve_same_occurrence_without_manifest(decisions)
            decisions, _same_edges = normalize_and_validate_event_decisions(
                decisions,
                events,
                entity_alias_to_canonical=entity_identity_aliases,
            )
            groups = []
            canonical_map = {}
            canonical_id_by_item = {
                item_id: event.canonical_event_id for item_id, event in events.items()
            }
            mention_ids_by_item = {
                item_id: list(event.mention_ids) for item_id, event in events.items()
            }
        event_identity_coverage = build_event_identity_coverage(
            targets=targets,
            checked_version_ids=checked_version_ids,
            events=events,
            mentions=mentions,
            hits_by_target=hits_by_target,
            decisions=decisions,
            vector_coverage=dense_coverage,
            candidate_universe_hash=candidate_universe_hash,
            candidate_universe_count=len(candidate_universe),
            candidate_source_snapshot_pins=candidate_source_snapshot_pins,
            resolution_batch_id=batch_id,
            contract_version=self.contract_version,
            contract_fingerprint=self.contract_fingerprint,
            prompt_hash=self.prompt_hash,
            canonicalization_status=canonicalization_status,
            rule_exclusions=rule_exclusions,
        )
        if unresolved_review_groups:
            # A retrieved pair is not covered until an immutable, validated
            # semantic response reviews it. Count only reviewed pairs here;
            # the retrieval diagnostics below retain the original selection
            # counts for operational analysis.
            for item_id, record in event_identity_coverage.items():
                reviewed_count = len(record.get("reviewed_candidates") or [])
                possible_count = int(record.get("possible_candidate_pair_count") or 0)
                record["selected_candidate_pair_count"] = reviewed_count
                record["unreviewed_candidate_pair_count"] = max(
                    0, possible_count - reviewed_count,
                )
                if record["unreviewed_candidate_pair_count"]:
                    record["status"] = "partial"
        approved_same_occurrence_pairs = self._approved_same_occurrence_pairs(
            decisions, mentions,
        )
        batch_candidate_rows = [
            row for row in rows["candidates"] if bool(row.get("is_batch_candidate"))
        ]
        dependency_reference_remaps: dict[str, dict[tuple[str, str, str | None], str]] = {}
        for repartition in repartition_requests.values():
            for remap in repartition.get("dependency_remaps", []):
                if remap["reference_kind"] == "mention_alias":
                    continue
                target_canonical_id = canonical_id_by_item.get(
                    remap["target_member_item_id"],
                )
                if not target_canonical_id:
                    raise ValueError("event dependency target has no validated partition canonical ID")
                dependency_reference_remaps.setdefault(
                    remap["dependent_item_id"], {},
                )[(remap["reference_kind"], remap["reference_id"], remap["reference_relation"])] = (
                    target_canonical_id
                )
        updates = self._build_updates(
            rows=batch_candidate_rows,
            decisions=decisions,
            canonical_map=canonical_map,
            canonical_id_by_item=canonical_id_by_item,
            mention_ids_by_item=mention_ids_by_item,
            dependency_reference_remaps=dependency_reference_remaps,
        )
        if updates:
            expected = {
                str(row["item_id"]): str(row["version_id"])
                for row in batch_candidate_rows
                if str(row["item_id"]) in updates
            }
            update_result = await asyncio.to_thread(
                repository.apply_event_reconciliation_content_updates,
                batch_id=batch_id,
                space=space,
                work_id=work_id,
                requested_work_id=requested_work_id,
                folder_id=folder_id,
                branch_id=branch_id,
                source_snapshot_pins=source_snapshot_pins,
                updates=updates,
                expected_version_ids=expected,
            )
        else:
            update_result = {"updated_candidates": 0, "appended_versions": 0}

        reviewed_counts = {
            relation: sum(item["record"].relation == relation for item in decisions)
            for relation in ("same_occurrence", "distinct_occurrence", "unresolved")
        }
        pair_review_coverage = {
            "rule_excluded_pair_count": len(rule_exclusions),
            "model_reviewed_pair_count": len(decisions),
            "selected_not_reviewed_pair_count": max(0, len(pair_rows) - len(decisions)),
            "not_retrieved_pair_count": len(possible_pairs - selected_pairs),
            "semantic_unresolved_pair_count": reviewed_counts["unresolved"],
        }
        targets_without_candidates = sum(not hits_by_target.get(item_id) for item_id in targets)
        overall_coverage_status = (
            "unavailable" if any(
                item["status"] == "unavailable" for item in event_identity_coverage.values()
            ) else "partial" if any(
                item["status"] == "partial" for item in event_identity_coverage.values()
            ) else "complete"
        )
        return {
            "status": (
                "partial"
                if overall_coverage_status != "complete" or unresolved_review_groups
                else "completed"
            ),
            "contract_version": self.contract_version,
            "contract_fingerprint": self.contract_fingerprint,
            "target_event_count": len(targets),
            "event_candidate_count": len(events),
            "candidate_pair_count": len(pair_rows),
            "retrieved_candidate_pair_count": retrieved_pair_count,
            "rule_excluded_pair_count": len(rule_exclusions),
            "rule_exclusions": rule_exclusions,
            "pair_review_coverage": pair_review_coverage,
            "semantic_reviewed_pair_count": len(decisions),
            "same_occurrence_pair_count": reviewed_counts["same_occurrence"],
            "distinct_occurrence_pair_count": reviewed_counts["distinct_occurrence"],
            "unresolved_pair_count": reviewed_counts["unresolved"],
            "targets_without_event_candidates": targets_without_candidates,
            "canonical_group_count": sum(len(group) > 1 for group in groups),
            "embedding_identity": embedding_identity,
            "embedding_coverage": dense_coverage,
            "candidate_retrieval_method": retrieval_method,
            "candidate_selection_mode": candidate_selection_mode,
            "candidate_pair_coverage": pairwise_coverage,
            "candidate_universe_hash": candidate_universe_hash,
            "candidate_universe_count": len(candidate_universe),
            "candidate_universe_source_snapshot_pins": candidate_source_snapshot_pins,
            "event_identity_coverage_status": overall_coverage_status,
            "event_identity_coverage": event_identity_coverage,
            "model_attempt_ids": model_attempt_ids,
            "unresolved_review_groups": unresolved_review_groups,
            "review_diagnostic": (
                "; ".join(
                    str(group.get("diagnostic") or "event review unresolved")
                    for group in unresolved_review_groups
                )[:1600]
                if unresolved_review_groups else None
            ),
            "canonical_event_id_map": canonical_map,
            "event_repartition_requests": list(repartition_requests.values()),
            "approved_same_occurrence_pairs": approved_same_occurrence_pairs,
            "canonicalization_status": canonicalization_status,
            "canonicalization_coverage": {
                "status": canonicalization_status,
                "reason": canonicalization_reason,
                "candidate_event_count": len(events),
                "position_count": len(positions_by_item),
                "unresolved_same_occurrence_count": unresolved_same_occurrence_count,
            },
            "updated_candidate_ids": sorted(updates),
            "updated_candidate_versions": expected if updates else {},
            **scope_metadata,
            "updated_items": int(update_result.get("updated_candidates", 0)),
            "appended_versions": int(update_result.get("appended_versions", 0)),
        }

    @staticmethod
    def _empty_result(rows: dict[str, Any], event_count: int) -> dict[str, Any]:
        return empty_result(rows, event_count)

    @staticmethod
    def _unique_candidate_pairs(
        targets: list[str],
        hits_by_target: dict[str, list[dict[str, Any]]],
        mentions: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        pairs: dict[tuple[str, str], dict[str, Any]] = {}
        for target_id in targets:
            for hit in hits_by_target.get(target_id, []):
                candidate_id = str(hit["item_id"])
                key = tuple(sorted((target_id, candidate_id)))
                target_is_batch = bool(mentions[target_id]["is_batch_candidate"])
                candidate_is_batch = bool(mentions[candidate_id]["is_batch_candidate"])
                if target_is_batch and not candidate_is_batch:
                    left_id, right_id = target_id, candidate_id
                elif candidate_is_batch and not target_is_batch:
                    left_id, right_id = candidate_id, target_id
                else:
                    left_id, right_id = key
                value = {
                    "target_item_id": left_id,
                    "candidate_item_id": right_id,
                    "rank": int(hit["rank"]),
                    "score": float(hit["score"]),
                    "candidate_retrieval_method": str(
                        hit.get("candidate_retrieval_method", "dense_vector")
                    ),
                    "embedding_identity": hit.get("embedding_identity"),
                }
                existing = pairs.get(key)
                if existing is None or (value["score"], -value["rank"]) > (
                    existing["score"], -existing["rank"],
                ):
                    pairs[key] = value
        return sorted(
            pairs.values(),
            key=lambda row: (-row["score"], row["rank"], row["target_item_id"], row["candidate_item_id"]),
        )

    async def _review_pairs(
        self,
        pairs: list[dict[str, Any]],
        scope: dict[str, Any],
        embedding_identity: dict[str, Any] | None,
        *,
        batch_id: str,
        selected_source_pins: list[dict[str, Any]],
        manifest_pin: dict[str, Any] | None,
        mentions: dict[str, dict[str, Any]],
        positions_by_item: dict[str, dict[str, Any]],
        canonicalization_status: str,
        candidate_retrieval_method: str,
        batch_max_concurrency: int,
        event_repartition_requests: list[dict[str, Any]] | None = None,
        event_rows: dict[str, dict[str, Any]] | None = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        return await execute_review_pairs(
            self,
            pairs,
            scope,
            batch_id=batch_id,
            selected_source_pins=selected_source_pins,
            manifest_pin=manifest_pin,
            mentions=mentions,
            positions_by_item=positions_by_item,
            canonicalization_status=canonicalization_status,
            candidate_retrieval_method=candidate_retrieval_method,
            review_concurrency=self.review_concurrency,
            batch_max_concurrency=batch_max_concurrency,
            event_repartition_requests=event_repartition_requests,
            event_rows=event_rows or {},
        )





    @staticmethod
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
        return audit_candidate_refs(
            pairs,
            mentions,
            scope,
            positions_by_item=positions_by_item,
            canonicalization_status=canonicalization_status,
            event_repartition_requests=event_repartition_requests,
            event_rows=event_rows,
        )

    @staticmethod
    def _approved_same_occurrence_pairs(
        decisions: list[dict[str, Any]],
        mentions: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return approved_same_occurrence_pairs(decisions, mentions)

    def _build_updates(
        self,
        *,
        rows: list[dict[str, Any]],
        decisions: list[dict[str, Any]],
        canonical_map: dict[str, str],
        mention_ids_by_item: dict[str, list[str]],
        canonical_id_by_item: dict[str, str] | None = None,
        dependency_reference_remaps: dict[str, dict[tuple[str, str, str | None], str]] | None = None,
    ) -> dict[str, dict[str, Any]]:
        decisions_by_item: dict[str, list[EventIdentityReviewRecord]] = {}
        for decision in decisions:
            record: EventIdentityReviewRecord = decision["record"]
            for item_id in (record.target_item_id, record.candidate_item_id):
                decisions_by_item.setdefault(item_id, []).append(record)
        updates: dict[str, dict[str, Any]] = {}
        for row in rows:
            item_id = str(row["item_id"])
            content = json.loads(json.dumps(row["content"], ensure_ascii=False))
            event_raw = content.get("typed_event")
            quantity_raw = content.get("typed_quantity")
            changed = False
            if isinstance(event_raw, dict):
                event = TypedEvent.model_validate(event_raw)
                next_event = event.model_dump(mode="json")
                canonical_id = (canonical_id_by_item or {}).get(
                    item_id,
                    canonical_map.get(event.canonical_event_id, event.canonical_event_id),
                )
                if canonical_id != event.canonical_event_id:
                    next_event["canonical_event_id"] = canonical_id
                    changed = True
                new_mentions = mention_ids_by_item.get(item_id, event.mention_ids)
                if new_mentions != event.mention_ids:
                    next_event["mention_ids"] = new_mentions
                    changed = True
                relation_fields = (
                    ("condition_event_ids", "event.condition"),
                    ("precondition_event_ids", "event.precondition"),
                    ("result_event_ids", "event.result"),
                    ("caused_by_event_ids", "event.caused_by"),
                )
                item_remaps = (dependency_reference_remaps or {}).get(item_id, {})
                for field, kind in relation_fields:
                    updated = list(dict.fromkeys(
                        item_remaps.get((kind, value, None), canonical_map.get(value, value))
                        for value in getattr(event, field)
                    ))
                    if updated != getattr(event, field):
                        next_event[field] = updated
                        changed = True
                constraints = []
                for constraint in event.order_constraints:
                    updated_id = canonical_map.get(
                        constraint.related_event_id, constraint.related_event_id,
                    )
                    updated_id = item_remaps.get(
                        ("event.order", constraint.related_event_id, constraint.relation),
                        updated_id,
                    )
                    value = constraint.model_dump(mode="json")
                    value["related_event_id"] = updated_id
                    constraints.append(value)
                unique_constraints: dict[tuple[str, str], dict[str, Any]] = {}
                for constraint in constraints:
                    key = (constraint["related_event_id"], constraint["relation"])
                    previous = unique_constraints.get(key)
                    if previous is None:
                        unique_constraints[key] = constraint
                    else:
                        refs = {
                            reference["id"]: reference
                            for reference in [*previous["evidence_refs"], *constraint["evidence_refs"]]
                        }
                        previous["evidence_refs"] = list(refs.values())
                next_constraints = list(unique_constraints.values())
                if next_constraints != event.model_dump(mode="json")["order_constraints"]:
                    next_event["order_constraints"] = next_constraints
                    changed = True
                def review_key(record: EventIdentityReviewRecord) -> tuple[str, str, str]:
                    return record.review_id, record.target_version_id, record.candidate_version_id

                review_ids = {review_key(item) for item in event.identity_reviews}
                event_reviews = list(event.model_dump(mode="json")["identity_reviews"])
                for record in decisions_by_item.get(item_id, []):
                    if review_key(record) not in review_ids:
                        event_reviews.append(record.model_dump(mode="json"))
                        review_ids.add(review_key(record))
                        changed = True
                if len(event_reviews) > 500:
                    raise RuntimeError(
                        "event identity review history reached its storage limit; no partial update was written"
                    )
                if event_reviews != next_event["identity_reviews"]:
                    next_event["identity_reviews"] = event_reviews
                    changed = True
                content["typed_event"] = next_event
                # Run the authoritative schema validation after exact reference rewrites.
                if changed:
                    TypedEvent.model_validate(next_event)
            if isinstance(quantity_raw, dict) and (canonical_map or dependency_reference_remaps):
                quantity_ids = list(dict.fromkeys(
                    (dependency_reference_remaps or {}).get(item_id, {}).get(
                        ("quantity.condition", value, None), canonical_map.get(value, value),
                    )
                    for value in quantity_raw.get("condition_event_ids", [])
                ))
                if quantity_ids != quantity_raw.get("condition_event_ids", []):
                    quantity_raw["condition_event_ids"] = quantity_ids
                    changed = True
            if changed:
                content["identity_source_content_hash"] = (
                    knowledge_graph_identity_source_content_hash(content)
                )
                updates[item_id] = content
        return updates
__all__ = [
    "EventVectorReconciliationService",
    "parse_event_semantic_review_response",
]
