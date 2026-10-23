"""Audited event identity decisions and exact canonical rewrites."""

from __future__ import annotations

import json
from typing import Any

from .repository_parse_event_attempt_validation import (
    _event_attempt_decisions as _event_attempt_decisions,
)

from .repository_parse_resolution_support import (
    _load_version,
    _version_evidence_ids,
)
from .event_canonicalization import derive_event_canonicalization
from .repository_parse_resolution_changes import (
    _RESOLUTION_EVENT_LIST_FIELDS,
    _resolution_content_changes,
)
from .event_reconciliation_decisions import (
    normalize_and_validate_event_decisions,
    unresolve_same_occurrence_without_manifest,
)
from .event_state import EventIdentityReviewRecord
from .query_contracts import EvidenceRef
from .repository_event_identity_reads import validate_current_event_entity_identity_pins
from .story_coordinates import NarrativePosition
from .event_reconciliation_validation import sha256_json
from .event_repartition import (
    normalize_event_repartition_requests,
)


def _verified_writing_event_positions(
    connection: Any,
    refs: list[dict[str, Any]],
    *,
    manifest: dict[str, Any],
    repository: Any,
) -> dict[str, NarrativePosition]:
    work_id = str(manifest.get("work_id") or "")
    branch_id = manifest.get("branch_id")
    revision = manifest.get("manifest_revision")
    manifest_hash = manifest.get("manifest_hash")
    if (
        manifest.get("space") != "writing"
        or not work_id
        or not isinstance(branch_id, str)
        or not branch_id
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 1
        or not isinstance(manifest_hash, str)
    ):
        raise ValueError("writing event resolution manifest pin is incomplete")
    try:
        manifest_hash = str(manifest_hash)
        if len(manifest_hash) != 64 or any(ch not in "0123456789abcdef" for ch in manifest_hash):
            raise ValueError("writing event resolution manifest hash is malformed")
        expected_positions = manifest.get("positions")
        expected_source_pins = manifest.get("source_pins")
        if not isinstance(expected_positions, dict) or not isinstance(expected_source_pins, dict):
            raise ValueError("writing batch manifest pin omitted source positions or artifact pins")

        mentions: list[
            tuple[str, str, str, str, list[EvidenceRef], int, dict[str, Any]]
        ] = []
        source_snapshots: dict[str, tuple[str, str]] = {}
        target_sources: set[str] = set()
        for ref in refs:
            item_id = str(ref.get("item_id") or "")
            source_id = str(ref.get("source_id") or "")
            snapshot_id = str(ref.get("snapshot_id") or "")
            content_hash = str(ref.get("content_hash") or "")
            if not item_id or not source_id or not snapshot_id or not content_hash:
                raise ValueError("event candidate source pin is incomplete")
            role = ref.get("role")
            if role not in {"target", "prior_published"}:
                raise ValueError("writing event candidate has an unsupported batch role")
            if ref.get("branch_id") != branch_id:
                raise ValueError("writing event candidate escaped the pinned branch")
            if role == "target":
                target_sources.add(source_id)
                if source_id not in expected_positions or source_id not in expected_source_pins:
                    raise ValueError("writing event target is absent from its exact batch source pins")
            previous = source_snapshots.setdefault(source_id, (snapshot_id, content_hash))
            if previous != (snapshot_id, content_hash):
                raise ValueError("writing event source has multiple candidate snapshots")

            version = _load_version(
                connection,
                item_id=item_id,
                version_id=str(ref.get("version_id") or ""),
            )
            event = version.get("typed_event")
            raw_position = ref.get("narrative_position")
            evidence_rows = ref.get("evidence_refs")
            if (
                not isinstance(event, dict)
                or event.get("branch_id") != branch_id
                or not isinstance(evidence_rows, list)
                or not evidence_rows
            ):
                raise ValueError("writing event candidate ref lacks typed event evidence")
            event_refs = event.get("evidence_refs")
            if not isinstance(event_refs, list) or sorted(
                str(value.get("id")) for value in event_refs if isinstance(value, dict)
            ) != sorted(
                str(value.get("id")) for value in evidence_rows if isinstance(value, dict)
            ):
                raise ValueError("writing event ref evidence differs from its exact candidate version")
            typed_refs = [EvidenceRef.model_validate(value) for value in evidence_rows]
            for evidence in typed_refs:
                if (
                    evidence.space != "writing"
                    or evidence.work_id != work_id
                    or evidence.branch_id != branch_id
                    or evidence.source_id != source_id
                    or evidence.snapshot_id != snapshot_id
                    or evidence.content_hash != content_hash
                    or evidence.offset_unit != "unicode_codepoint"
                ):
                    raise ValueError("writing event evidence escaped its current source pin")
            if not isinstance(raw_position, dict):
                raise ValueError("writing event candidate omitted its exact narrative position")
            mentions.append((
                item_id,
                source_id,
                snapshot_id,
                content_hash,
                typed_refs,
                min(reference.start_offset for reference in typed_refs),
                raw_position,
            ))

        if not target_sources:
            raise ValueError("writing event resolution has no target sources pinned to the batch")
        if len(source_snapshots) > 1000:
            raise ValueError("writing event manifest source scope exceeds its bound")
        source_ids = sorted(source_snapshots)
        rows = connection.execute(
            f"""
            SELECT source.id,source.space,source.work_id,source.deleted_at,
                   current.snapshot_id,snapshot.content,snapshot.content_hash
            FROM source
            JOIN source_current_snapshot AS current ON current.source_id=source.id
            JOIN source_snapshot AS snapshot
              ON snapshot.id=current.snapshot_id AND snapshot.source_id=source.id
            WHERE source.id IN ({','.join('?' for _ in source_ids)})
            """,
            source_ids,
        ).fetchall()
        sources_by_id = {str(row["id"]): row for row in rows}
        if set(sources_by_id) != set(source_ids):
            raise ValueError("writing event current source snapshots are unavailable")
        refs_by_source: dict[str, list[EvidenceRef]] = {source_id: [] for source_id in source_ids}
        for _, source_id, snapshot_id, content_hash, evidence_refs, _, _ in mentions:
            row = sources_by_id[source_id]
            if (
                row["deleted_at"] is not None
                or row["space"] != "writing"
                or row["work_id"] != work_id
                or row["snapshot_id"] != snapshot_id
                or row["content_hash"] != content_hash
                or source_snapshots[source_id] != (str(row["snapshot_id"]), str(row["content_hash"]))
            ):
                raise ValueError("writing event source snapshot is not current in its exact work scope")
            refs_by_source[source_id].extend(evidence_refs)

        source_inputs = [
            {
                "source_id": source_id,
                "snapshot_id": str(sources_by_id[source_id]["snapshot_id"]),
                "content_hash": str(sources_by_id[source_id]["content_hash"]),
                "snapshot_content": str(sources_by_id[source_id]["content"]),
                "evidence_refs": refs_by_source[source_id],
            }
            for source_id in source_ids
        ]
        resolved = repository.writing_narrative_positions(
            connection=connection,
            manifest_pin=manifest,
            work_id=work_id,
            branch_id=branch_id,
            expected_manifest_revision=revision,
            expected_manifest_hash=manifest_hash,
            sources=source_inputs,
            purpose="prepared_parse",
        )
        current_manifest_pin = resolved.get("manifest_pin")
        current_positions = resolved.get("positions")
        current_source_pins = resolved.get("source_pins")
        if (
            not isinstance(current_manifest_pin, dict)
            or not isinstance(current_positions, dict)
            or not isinstance(current_source_pins, dict)
            or any(current_manifest_pin.get(key) != manifest.get(key) for key in (
                "status", "space", "work_id", "branch_id", "manifest_revision",
                "manifest_hash", "manifest_coordinate_status",
            ))
        ):
            raise ValueError("writing event manifest changed before publication")

        def identity_fields(position: dict[str, Any]) -> dict[str, Any]:
            return {key: value for key, value in position.items() if key != "offset"}

        for source_id in target_sources:
            if (
                current_source_pins.get(source_id) != expected_source_pins.get(source_id)
                or not isinstance(current_positions.get(source_id), dict)
                or not isinstance(expected_positions.get(source_id), dict)
                or identity_fields(current_positions[source_id])
                != identity_fields(expected_positions[source_id])
            ):
                raise ValueError("writing event target changed from its exact batch source pin")

        positions: dict[str, NarrativePosition] = {}
        for item_id, source_id, snapshot_id, content_hash, _, offset, raw_position in mentions:
            base = current_positions.get(source_id)
            if not isinstance(base, dict):
                raise ValueError("writing event source is absent from the current manifest")
            expected = NarrativePosition.model_validate({**base, "offset": offset})
            actual = NarrativePosition.model_validate(raw_position)
            if actual != expected or (
                actual.space != "writing"
                or actual.work_id != work_id
                or actual.branch_id != branch_id
                or actual.source_id != source_id
                or actual.snapshot_id != snapshot_id
                or actual.content_hash != content_hash
            ):
                raise ValueError("writing event candidate position differs from its current artifact pin")
            previous = positions.setdefault(item_id, actual)
            if previous != actual:
                raise ValueError("writing event attempts disagree on a candidate's verified position")
        return positions
    except (KeyError, TypeError, ValueError, RuntimeError) as exc:
        raise ValueError(f"writing event candidate has no current verified position: {exc}") from exc


def _verified_event_positions(
    connection: Any,
    attempts: list[dict[str, Any]],
    *,
    batch: Any,
    repository: Any,
) -> dict[str, NarrativePosition]:
    refs = [
        ref
        for attempt in attempts if attempt["phase"] == "event_resolution"
        for ref in attempt["candidate_refs"]
    ]
    if not refs:
        return {}
    manifest = (
        json.loads(str(batch["requested_manifest_pin_json"]))
        if batch["requested_manifest_pin_json"] is not None else None
    )
    if manifest is None:
        if any(ref.get("narrative_position") is not None for ref in refs):
            raise ValueError("event resolution supplied coordinates without a pinned manifest")
        return {}
    if str(batch["space"]) == "writing":
        return _verified_writing_event_positions(
            connection,
            refs,
            manifest=manifest,
            repository=repository,
        )
    if str(batch["space"]) != "learning":
        raise ValueError("event resolution manifest has an unsupported source space")
    work_id = str(manifest.get("work_id") or "")
    current = connection.execute(
        "SELECT revision FROM knowledge_story_order_current WHERE work_id = ?",
        (work_id,),
    ).fetchone()
    if current is None or int(current["revision"]) != int(manifest.get("manifest_revision", 0)):
        raise ValueError("event resolution manifest changed before publication")
    order = repository._read_chapter_order(
        connection, work_id, int(manifest["manifest_revision"]),
    )
    if (
        order.get("coordinate_status") != "verified"
        or not order.get("is_current_revision")
        or not order.get("is_complete_for_current_scope")
        or order.get("manifest_hash") != manifest.get("manifest_hash")
    ):
        raise ValueError("event resolution requires the same complete current manifest")
    entries = {str(row["source_id"]): row for row in order["entries"]}
    positions: dict[str, NarrativePosition] = {}
    for ref in refs:
        if ref.get("side") == "dependency_context":
            continue
        item_id = str(ref.get("item_id") or "")
        source_id = str(ref.get("source_id") or "")
        snapshot_id = str(ref.get("snapshot_id") or "")
        try:
            version = _load_version(
                connection, item_id=item_id, version_id=str(ref.get("version_id") or ""),
            )
            event = version.get("typed_event")
            raw_position = ref.get("narrative_position")
            evidence = ref.get("evidence_refs")
            if not isinstance(event, dict) or not isinstance(evidence, list) or not evidence:
                raise ValueError("event candidate ref lacks typed event evidence")
            event_refs = event.get("evidence_refs")
            if not isinstance(event_refs, list) or sorted(
                str(value.get("id")) for value in event_refs if isinstance(value, dict)
            ) != sorted(
                str(value.get("id")) for value in evidence if isinstance(value, dict)
            ):
                raise ValueError("event position ref evidence differs from the exact candidate version")
            entry = entries[source_id]
            if (
                entry["snapshot_id"] != snapshot_id
                or entry["content_hash"] != ref.get("content_hash")
                or not entry["snapshot_is_current"]
            ):
                raise ValueError("event candidate source snapshot is not current in the manifest")
            offsets: list[int] = []
            for evidence_ref in evidence:
                if not isinstance(evidence_ref, dict):
                    raise ValueError("event evidence ref is malformed")
                if (
                    evidence_ref.get("space") != "learning"
                    or evidence_ref.get("source_id") != source_id
                    or evidence_ref.get("snapshot_id") != snapshot_id
                    or evidence_ref.get("content_hash") != ref.get("content_hash")
                    or evidence_ref.get("offset_unit") != "unicode_codepoint"
                    or evidence_ref.get("branch_id") is not None
                ):
                    raise ValueError("event evidence ref escaped the pinned manifest source")
                start = evidence_ref.get("start_offset")
                end = evidence_ref.get("end_offset")
                if (
                    isinstance(start, bool) or not isinstance(start, int) or start < 0
                    or isinstance(end, bool) or not isinstance(end, int) or end <= start
                ):
                    raise ValueError("event evidence ref has an invalid exact offset")
                offsets.append(start)
            expected = NarrativePosition(
                domain="narrative_order",
                space="learning",
                work_id=work_id,
                branch_id=None,
                manifest_revision=int(manifest["manifest_revision"]),
                manifest_hash=str(manifest["manifest_hash"]),
                manifest_coordinate_status="verified",
                source_id=source_id,
                snapshot_id=snapshot_id,
                content_hash=str(ref.get("content_hash") or ""),
                chapter_title=str(entry["chapter_title"]),
                narrative_ordinal=int(entry["narrative_ordinal"]),
                offset=min(offsets),
                offset_unit="unicode_codepoint",
                coordinate_basis="verified",
            )
            actual = NarrativePosition.model_validate(raw_position)
            if actual != expected:
                raise ValueError("event candidate narrative position differs from its current manifest pin")
            previous = positions.setdefault(item_id, actual)
            if previous != actual:
                raise ValueError("event attempts disagree on a candidate's verified position")
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"event candidate {item_id} has no current verified position: {exc}") from exc
    return positions






def _derive_event_authorized_map(
    decisions: list[dict[str, Any]],
    event_by_item: dict[str, dict[str, Any]],
    positions_by_item: dict[str, NarrativePosition],
) -> tuple[dict[str, str], dict[str, set[str]], dict[str, str], dict[str, set[str]]]:
    """Rebuild event merge and explicit split maps from persisted review attempts."""
    same_decisions = [row for row in decisions if row["relation"] == "same_occurrence"]
    repartition_requests = _event_repartition_plans_from_decisions(decisions)
    repartition_by_canonical = {
        plan["canonical_event_id"]: plan["partitions"]
        for plan in repartition_requests
    }
    repartition_alias_owners = {
        plan["canonical_event_id"]: {
            remap["reference_id"]: remap["target_member_item_id"]
            for remap in plan.get("dependency_remaps", [])
            if remap["reference_kind"] == "mention_alias"
        }
        for plan in repartition_requests
    }
    if not same_decisions and not repartition_requests:
        return {}, {}, {}, {}
    if not event_by_item or set(positions_by_item) != set(event_by_item):
        raise ValueError("confirmed event merge has no complete current manifest positions")
    canonicalization = derive_event_canonicalization(
        canonical_ids_by_item={
            item_id: str(row["event"].get("canonical_event_id") or "")
            for item_id, row in event_by_item.items()
        },
        mention_ids_by_item={
            item_id: [str(value) for value in row["event"].get("mention_ids", [])]
            for item_id, row in event_by_item.items()
        },
        accepted_edges=[
            (str(row["target"]["item_id"]), str(row["candidate"]["item_id"]))
            for row in same_decisions
        ],
        positions_by_item=positions_by_item,
        event_ids_by_item={
            item_id: str(row["event"].get("id") or "")
            for item_id, row in event_by_item.items()
        },
        repartition_partitions_by_canonical=repartition_by_canonical,
        repartition_alias_owners_by_canonical=repartition_alias_owners,
    )
    if not canonicalization["available"]:
        raise ValueError("confirmed event merge has unavailable manifest positions")
    mention_components: dict[str, set[str]] = {}
    mention_ids_by_item: dict[str, set[str]] = {}
    for item_id, row in event_by_item.items():
        canonical_id = str(row["event"].get("canonical_event_id") or "")
        mentions = set(str(value) for value in canonicalization["mention_ids_by_item"][item_id])
        mention_components.setdefault(canonical_id, set()).update(mentions)
        mention_ids_by_item[item_id] = mentions
    return (
        canonicalization["canonical_event_id_map"],
        mention_components,
        canonicalization["canonical_event_id_by_item"],
        mention_ids_by_item,
    )


def _event_decision_for_review(
    decisions: list[dict[str, Any]], record: dict[str, Any],
) -> bool:
    for decision in decisions:
        expected = decision.get("expected_review_record")
        if isinstance(expected, dict) and expected == record:
            return True
    return False


def _appended_event_review_records(
    connection: Any,
    event_by_item: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Read exact review records appended after each attempt-pinned event version.

    One pair review is persisted on each participating event candidate. A
    source-scoped publication gate sees only one source's version transition,
    while a validated event attempt can contain pairs across several sources.
    Collecting the suffix of each exact participant version chain lets the gate
    prove a decision from either side without treating an unpersisted attempt
    response as publication evidence.
    """
    records_by_key: dict[
        tuple[str, str, str, str, tuple[str, ...]], dict[str, Any]
    ] = {}
    for item_id, reference in event_by_item.items():
        if str(reference.get("item_id") or "") != item_id:
            raise ValueError("event review participant identity differs from its pinned ref")
        if reference.get("role") not in {"target", "prior_published"}:
            raise ValueError("event review participant has an unsupported batch role")
        start_version_id = str(reference.get("version_id") or "")
        if not start_version_id:
            raise ValueError("event review participant omitted its exact input version")
        history = connection.execute(
            "SELECT id, version, content_json FROM knowledge_version "
            "WHERE item_id = ? ORDER BY version",
            (item_id,),
        ).fetchall()
        start = next(
            (index for index, row in enumerate(history) if str(row["id"]) == start_version_id),
            None,
        )
        if start is None:
            raise ValueError("event review participant input version is absent from history")
        tail = history[start:]
        if not tail or str(tail[0]["id"]) != start_version_id:
            raise ValueError("event review participant version chain is incomplete")

        previous = _load_version(
            connection, item_id=item_id, version_id=start_version_id,
        )
        for current_row in tail[1:]:
            current_version_id = str(current_row["id"])
            current = _load_version(
                connection, item_id=item_id, version_id=current_version_id,
            )
            if _version_evidence_ids(connection, start_version_id) != _version_evidence_ids(
                connection, current_version_id,
            ):
                raise ValueError("event review participant changed its pinned source evidence")
            _, event_fields = _resolution_content_changes(previous, current)
            if "typed_event" not in event_fields:
                previous = current
                continue
            old_event = previous.get("typed_event")
            new_event = current.get("typed_event")
            if not isinstance(old_event, dict) or not isinstance(new_event, dict):
                raise ValueError("event review participant removed or malformed its typed event")
            old_reviews = old_event.get("identity_reviews") or []
            new_reviews = new_event.get("identity_reviews") or []
            if (
                not isinstance(old_reviews, list)
                or not isinstance(new_reviews, list)
                or new_reviews[:len(old_reviews)] != old_reviews
            ):
                raise ValueError("event review participant rewrote prior review history")
            for raw_record in new_reviews[len(old_reviews):]:
                try:
                    record = EventIdentityReviewRecord.model_validate(raw_record)
                except Exception as exc:
                    raise ValueError("appended event review record is malformed") from exc
                target = event_by_item.get(record.target_item_id)
                candidate = event_by_item.get(record.candidate_item_id)
                if target is None or candidate is None or item_id not in {
                    record.target_item_id, record.candidate_item_id,
                }:
                    continue
                if (
                    str(target.get("version_id") or "") != record.target_version_id
                    or str(candidate.get("version_id") or "") != record.candidate_version_id
                ):
                    continue
                key = (
                    record.target_item_id,
                    record.candidate_item_id,
                    record.target_version_id,
                    record.candidate_version_id,
                    tuple(sorted(reference.id for reference in record.evidence_refs)),
                )
                serialized = record.model_dump(mode="json")
                previous_record = records_by_key.get(key)
                if previous_record is None:
                    records_by_key[key] = serialized
                elif previous_record != serialized:
                    raise ValueError("event participants persisted conflicting records for one exact pair")
            previous = current
    return list(records_by_key.values())


def _verified_event_entity_identity_aliases(
    connection: Any,
    *,
    repository: Any,
    batch: Any,
    item: Any,
    event_by_item: dict[str, dict[str, Any]],
) -> dict[str, str]:
    pins_by_item: dict[str, dict[str, Any]] = {}
    for row in event_by_item.values():
        pins = row.get("entity_identity_pins")
        if not isinstance(pins, dict):
            raise ValueError("event resolution omitted pinned entity identity context")
        for entity_id, pin in pins.items():
            previous = pins_by_item.setdefault(str(entity_id), pin)
            if previous != pin:
                raise ValueError("event attempts disagree on an entity identity version or scope")

    manifest_scope_json = repository._current_event_reconciliation_manifest_scope(
        connection,
        space=str(batch["space"]),
        requested_work_id=(
            str(batch["requested_work_id"])
            if batch["requested_work_id"] is not None else None
        ),
        requested_manifest_pin_json=batch["requested_manifest_pin_json"],
        source_snapshot_pins={str(item["source_id"]): str(item["snapshot_id"])},
    )
    return validate_current_event_entity_identity_pins(
        connection,
        pins_by_item=pins_by_item,
        batch_id=str(batch["id"]),
        space=str(batch["space"]),
        work_id=(str(batch["work_id"]) if batch["work_id"] is not None else None),
        branch_id=(str(batch["branch_id"]) if batch["branch_id"] is not None else None),
        manifest_scope_json=manifest_scope_json,
        allow_historical_versions=True,
    )


def _normalize_attempt_event_reviews(
    decisions: list[dict[str, Any]],
    event_by_item: dict[str, dict[str, Any]],
    appended_reviews: list[dict[str, Any]],
    *,
    entity_aliases: dict[str, str],
) -> list[dict[str, Any]]:
    """Rebuild exact persisted review records from attempts and the shared validator."""
    review_by_pair: dict[tuple[str, str, str, str, tuple[str, ...]], dict[str, Any]] = {}
    for raw_record in appended_reviews:
        try:
            record = EventIdentityReviewRecord.model_validate(raw_record)
        except Exception as exc:
            raise ValueError("appended event review record is malformed") from exc
        key = (
            record.target_item_id,
            record.candidate_item_id,
            record.target_version_id,
            record.candidate_version_id,
            tuple(sorted(reference.id for reference in record.evidence_refs)),
        )
        if key in review_by_pair:
            raise ValueError("appended event review records duplicate one exact pair")
        review_by_pair[key] = record.model_dump(mode="json")

    normalized_inputs: list[dict[str, Any]] = []
    legacy_inputs: list[dict[str, Any]] = []
    repartition_requests = _event_repartition_plans_from_decisions(decisions)
    repartition_by_canonical = {
        plan["canonical_event_id"]: plan["partitions"]
        for plan in repartition_requests
    }
    raw_by_key: dict[tuple[str, str, str, str, tuple[str, ...]], dict[str, Any]] = {}
    for decision in decisions:
        target = decision["target"]
        candidate = decision["candidate"]
        evidence_ids = tuple(sorted(str(value) for value in decision["evidence_ref_ids"]))
        key = (
            str(target["item_id"]), str(candidate["item_id"]),
            str(target["version_id"]), str(candidate["version_id"]), evidence_ids,
        )
        stored_raw = review_by_pair.get(key)
        if stored_raw is None:
            # A validated response may include unused decisions. Only persisted
            # exact-pair records can authorize a transition; the transition gate
            # still rejects every appended record or rewrite lacking a proof.
            continue
        stored = EventIdentityReviewRecord.model_validate(stored_raw)
        retrieval = decision.get("candidate_retrieval_method")
        embedding = decision.get("embedding_identity")
        if (
            stored.semantic_model_id != decision.get("semantic_model_id")
            or stored.semantic_prompt_hash != decision.get("semantic_prompt_hash")
            or stored.candidate_retrieval_method != retrieval
            or sorted(reference.id for reference in stored.evidence_refs) != list(evidence_ids)
        ):
            continue
        if embedding is None:
            if stored.embedding_model_id is not None or stored.embedding_route_fingerprint is not None:
                continue
        elif (
            not isinstance(embedding, dict)
            or stored.embedding_model_id != embedding.get("model_id")
            or stored.embedding_route_fingerprint != embedding.get("route_fingerprint")
        ):
            continue
        review_identity = {
            "target_item_id": key[0],
            "candidate_item_id": key[1],
            "relation": decision["relation"],
            "candidate_retrieval_method": retrieval,
            "semantic_model_id": stored.semantic_model_id,
            "semantic_route_fingerprint": stored.semantic_route_fingerprint,
            "semantic_prompt_hash": stored.semantic_prompt_hash,
            "evidence_ids": list(evidence_ids),
        }
        if embedding is not None:
            review_identity["embedding_route_fingerprint"] = stored.embedding_route_fingerprint
        legacy_id = sha256_json(review_identity)
        review_identity.update(target_version_id=key[2], candidate_version_id=key[3])
        raw_record = stored.model_copy(update={
            "review_id": sha256_json(review_identity),
            "relation": decision["relation"],
            "status": {
                "same_occurrence": "confirmed",
                "distinct_occurrence": "reviewed_distinct",
                "unresolved": "unresolved",
            }[decision["relation"]],
            "rationale": decision["rationale"],
            "evidence_refs": [
                EvidenceRef.model_validate(reference)
                for reference in decision["evidence_refs"]
            ],
        })
        normalized_inputs.append({
            "target_item_id": key[0],
            "candidate_item_id": key[1],
            "relation": decision["relation"],
            "rationale": decision["rationale"],
            "record": raw_record,
        })
        # Read-only adapter for existing audit records whose ID omitted the
        # participant versions. All scope, version, evidence and record fields
        # still have to round-trip exactly through the current validator.
        legacy_inputs.append({**normalized_inputs[-1], "record": raw_record.model_copy(update={"review_id": legacy_id})})
        raw_by_key[key] = decision

    normalized = []
    for inputs in (normalized_inputs, legacy_inputs):
        values, _same_edges = normalize_and_validate_event_decisions(
            inputs, {item_id: row["event_model"] for item_id, row in event_by_item.items()},
            entity_alias_to_canonical=entity_aliases,
            repartition_partitions_by_canonical=repartition_by_canonical,
        )
        if any(row.get("canonicalization_status") != "verified" for row in event_by_item.values()):
            values = unresolve_same_occurrence_without_manifest(values)
        normalized.extend(values)

    for normalized_decision in normalized:
        record: EventIdentityReviewRecord = normalized_decision["record"]
        key = (
            normalized_decision["target_item_id"],
            normalized_decision["candidate_item_id"],
            record.target_version_id,
            record.candidate_version_id,
            tuple(sorted(reference.id for reference in record.evidence_refs)),
        )
        original = raw_by_key.get(key)
        if original is None or record.model_dump(mode="json") != review_by_pair.get(key):
            continue
        original["relation"] = record.relation
        original["rationale"] = record.rationale
        original["expected_review_record"] = record.model_dump(mode="json")
    return [decision for decision in decisions if "expected_review_record" in decision]


def _event_repartition_plans_from_decisions(
    decisions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_canonical: dict[str, dict[str, Any]] = {}
    for decision in decisions:
        plans = decision.get("event_repartition_requests") or []
        for plan in normalize_event_repartition_requests(plans):
            canonical_id = plan["canonical_event_id"]
            previous = by_canonical.get(canonical_id)
            if previous is not None and previous != plan:
                raise ValueError("event decision rows disagree on an exact repartition request")
            by_canonical[canonical_id] = plan
    return [by_canonical[key] for key in sorted(by_canonical)]


def _validate_event_resolution_transition(
    previous: dict[str, Any],
    current: dict[str, Any],
    *,
    item_id: str,
    canonical_map: dict[str, str],
    mention_components: dict[str, set[str]],
    decisions: list[dict[str, Any]],
    canonical_id_by_item: dict[str, str] | None = None,
    mention_ids_by_item: dict[str, set[str]] | None = None,
) -> None:
    dependency_maps: dict[str, dict[tuple[str, str, str | None], str]] = {}
    repartition_member_canonicals: dict[str, str] = {}
    for plan in _event_repartition_plans_from_decisions(decisions):
        for member in plan["member_item_ids"]:
            repartition_member_canonicals[member] = plan["canonical_event_id"]
        for remap in plan.get("dependency_remaps", []):
            target = (canonical_id_by_item or {}).get(remap["target_member_item_id"])
            if not target:
                raise ValueError("event dependency target has no exact approved partition canonical ID")
            key = (
                remap["reference_kind"], remap["reference_id"],
                remap["reference_relation"],
            )
            mapping = dependency_maps.setdefault(remap["dependent_item_id"], {})
            previous_target = mapping.setdefault(key, target)
            if previous_target != target:
                raise ValueError("one event reference has conflicting author-approved partition owners")
    old_event = previous.get("typed_event")
    new_event = current.get("typed_event")
    if isinstance(old_event, dict) and isinstance(new_event, dict):
        expected = json.loads(json.dumps(old_event, ensure_ascii=False))
        old_canonical = str(old_event.get("canonical_event_id") or "")
        expected["canonical_event_id"] = (canonical_id_by_item or {}).get(
            item_id, canonical_map.get(old_canonical, old_canonical),
        )
        before_mentions = old_event.get("mention_ids") or []
        next_mentions = new_event.get("mention_ids") or []
        removable_split_mentions: set[str] = set()
        for repartition in _event_repartition_plans_from_decisions(decisions):
            members = set(repartition["member_item_ids"])
            if item_id not in members:
                continue
            same_partition = next(
                set(partition) for partition in repartition["partitions"]
                if item_id in partition
            )
            removable_split_mentions.update(members - same_partition)
        authorized_mentions = (mention_ids_by_item or {}).get(
            item_id, mention_components.get(old_canonical, set()),
        )
        planned_mentions = (mention_ids_by_item or {}).get(item_id)
        if item_id in repartition_member_canonicals and planned_mentions is not None:
            if (
                not isinstance(next_mentions, list)
                or len(next_mentions) != len(set(next_mentions))
                or set(next_mentions) != planned_mentions
            ):
                raise ValueError("event mention aliases do not match their explicitly approved partition owners")
        elif (
            not isinstance(next_mentions, list)
            or not (
                set(str(value) for value in before_mentions) - removable_split_mentions
            ).issubset(set(str(value) for value in next_mentions))
            or item_id not in next_mentions
            or not set(str(value) for value in next_mentions).issubset(
                authorized_mentions | set(str(value) for value in before_mentions) | {item_id}
            )
        ):
            raise ValueError("event mention list includes an unaudited identity member")
        expected["mention_ids"] = next_mentions
        expected["identity_reviews"] = new_event.get("identity_reviews")
        for record in (new_event.get("identity_reviews") or [])[len(old_event.get("identity_reviews") or []):]:
            if not isinstance(record, dict) or not _event_decision_for_review(decisions, record):
                raise ValueError("appended event identity review has no exact validated model decision")
        for field in _RESOLUTION_EVENT_LIST_FIELDS:
            old_values = old_event.get(field) or []
            kind = {
                "condition_event_ids": "event.condition",
                "precondition_event_ids": "event.precondition",
                "result_event_ids": "event.result",
                "caused_by_event_ids": "event.caused_by",
            }[field]
            item_remaps = dependency_maps.get(item_id, {})
            expected[field] = list(dict.fromkeys(
                item_remaps.get((kind, str(value), None),
                                canonical_map.get(str(value), str(value)))
                for value in old_values
            ))
        expected["order_constraints"] = _event_constraints_after_dependency_map(
            old_event.get("order_constraints") or [],
            canonical_map,
            dependency_maps.get(item_id, {}),
        )
        if expected != new_event:
            raise ValueError("event resolution is not an exact evidence-backed canonical reference rewrite")
    old_quantity = previous.get("typed_quantity")
    new_quantity = current.get("typed_quantity")
    if isinstance(old_quantity, dict) and isinstance(new_quantity, dict):
        expected_quantity = json.loads(json.dumps(old_quantity, ensure_ascii=False))
        expected_quantity["condition_event_ids"] = list(dict.fromkeys(
            dependency_maps.get(item_id, {}).get(
                ("quantity.condition", str(value), None),
                canonical_map.get(str(value), str(value)),
            )
            for value in (old_quantity.get("condition_event_ids") or [])
        ))
        if expected_quantity != new_quantity:
            raise ValueError("quantity condition references are not an exact canonical event rewrite")


def _event_constraints_after_dependency_map(
    value: Any,
    canonical_map: dict[str, str],
    dependency_map: dict[tuple[str, str, str | None], str],
) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError("typed event order constraints are malformed")
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError("typed event order constraint is malformed")
        constraint = json.loads(json.dumps(raw, ensure_ascii=False))
        related = str(constraint.get("related_event_id") or "")
        relation = str(constraint.get("relation") or "")
        if not related or relation not in {"before", "simultaneous"}:
            raise ValueError("typed event order constraint is incomplete")
        constraint["related_event_id"] = dependency_map.get(
            ("event.order", related, relation), canonical_map.get(related, related),
        )
        key = (constraint["related_event_id"], relation)
        previous = merged.get(key)
        if previous is None:
            merged[key] = constraint
        else:
            refs = {
                str(reference.get("id")): reference
                for reference in [
                    *(previous.get("evidence_refs") or []),
                    *(constraint.get("evidence_refs") or []),
                ]
                if isinstance(reference, dict) and reference.get("id")
            }
            previous["evidence_refs"] = list(refs.values())
    return list(merged.values())




