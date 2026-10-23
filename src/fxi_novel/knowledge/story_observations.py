"""Source-local reading views of existing public knowledge observations.

These are selectable material, not a new fact owner or stable entity identity.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict

from .query_contracts import EvidenceRef
from .scope_selection import current_prose_projection_filter


def current_register_pin_source_ids(connection, source_pins):
    """Authorize dependency sources only through still-current exact caller pins."""
    rows = connection.execute(f"""
        SELECT DISTINCT snapshot.source_id
        FROM json_each(?) AS pin
        JOIN source_snapshot AS snapshot
          ON snapshot.source_id = json_extract(pin.value, '$.source_id')
         AND snapshot.id = json_extract(pin.value, '$.snapshot_id')
         AND snapshot.content_hash = json_extract(pin.value, '$.content_hash')
        JOIN source_current_snapshot AS current
          ON current.source_id = snapshot.source_id AND current.snapshot_id = snapshot.id
        JOIN source AS s ON s.id = snapshot.source_id
        WHERE s.deleted_at IS NULL AND {current_prose_projection_filter('s')}
    """, (json.dumps(source_pins, ensure_ascii=False),)).fetchall()
    return [str(row["source_id"]) for row in rows]


def source_local_observations(records, *, source_pins, branch_id):
    pins = {row["source_id"]: row for row in source_pins}
    groups = defaultdict(list)
    for record in records:
        content, publication = record["content"], record.get("publication", {})
        if (content.get("candidate_role") != "fact" or content.get("branch_id") != branch_id
                or not publication.get("published")
                or not publication.get("capabilities", {}).get("base_claim")):
            continue
        evidence = [ref for ref in record.get("evidence", [])
                    if (pin := pins.get(ref["source_id"]))
                    and pin["snapshot_id"] == ref["snapshot_id"]
                    and pin["content_hash"] == ref["source_version_hash"]]
        if not evidence:
            continue
        learned = content.get("query_learning_claim") or {}
        proof = learned.get("evidence_refs", [])
        if any(not (pin := pins.get(ref["source_id"]))
               or pin["snapshot_id"] != ref["snapshot_id"]
               or pin["content_hash"] != ref["content_hash"] for ref in proof):
            continue
        if proof:
            expression = learned["original_expression"]
            anchors = [ref for ref in proof if expression in ref["quote"]]
            if not anchors:
                continue
            anchor = min(anchors, key=lambda ref: (
                ref["start_offset"] + ref["quote"].index(expression), ref["id"]))
            start = anchor["start_offset"] + anchor["quote"].index(expression)
            evidence = [ref for ref in evidence if ref["source_id"] == anchor["source_id"]
                        and ref["snapshot_id"] == anchor["snapshot_id"]
                        and ref["start_offset"] == start and ref["quote"] == expression]
            if not evidence:
                continue
        qualifiers = {key: content[key] for key in (
            "claim_basis", "asserted_by", "asserted_by_entity_ref", "observed_by",
            "observed_by_entity_ref", "holder", "holder_entity_ref", "audience",
            "audience_entity_refs", "story_time_expression", "story_order",
            "valid_from_story_order", "valid_until_story_order",
        ) if content.get(key) is not None}
        if learned.get("conditions"):
            qualifiers["conditions"] = learned["conditions"]
        if learned.get("speaker"):
            qualifiers["speaker"] = learned["speaker"]
        basis = content.get("claim_basis")
        interpretation = ("attributed_claim" if basis in {"belief", "dialogue"}
                          else "recorded_inference" if basis == "inference"
                          else "recorded_observation")
        certainty = content.get("state_value_status", "known_value")
        details = {
            "subject": content["subject"], "predicate": content["predicate"],
            "value": content.get("value"), "category": content["category"],
            "entity_ref": content.get("entity_ref"), "entity_name": content.get("entity_name"),
            "qualifiers": qualifiers, "state_value_status": certainty,
            "certainty": "explicit_unknown" if certainty == "explicit_unknown" else interpretation,
            "interpretation_status": interpretation, "identity_scope": "source_local",
            "publication_basis": publication.get("basis"),
        }
        for key in ("target_entity_ref", "target_entity_name", "state_slot", "entity_kind", "entity_granularity"):
            if content.get(key) is not None:
                details[key] = content[key]
        assessment = learned.get("semantic_assessment") or (content.get("semantic_review") or {}).get("assessment")
        if assessment:
            details["semantic_assessment"] = assessment
        # A stable parser-local reference groups history within one source only.
        # Query publications use their own item reference; a matching name never
        # authorizes combining identities between chapters or distinct claims.
        for ref in evidence:
            pin = pins[ref["source_id"]]
            group = (branch_id, ref["source_id"], ref["snapshot_id"],
                     content.get("entity_ref") or record["id"], content["category"],
                     content["predicate"], json.dumps(qualifiers, ensure_ascii=False, sort_keys=True))
            groups[group].append({
                "id": record["id"], "version_id": record["version_id"],
                "chapter_id": pin["chapter_id"], "branch_id": branch_id,
                "description": content.get("value") or ref["quote"], "mode": "observation",
                "amount": None, "unit": None, "event_kind": None,
                "source_id": ref["source_id"], "snapshot_id": ref["snapshot_id"],
                "content_hash": ref["source_version_hash"], "start": ref["start_offset"],
                "end": ref["end_offset"], "quote": ref["quote"], "details": details,
                "evidence_refs": proof or [EvidenceRef.from_exact_span(
                    space="writing", work_id=record.get("work_id"), branch_id=branch_id,
                    source_id=item["source_id"], snapshot_id=item["snapshot_id"],
                    content_hash=item["source_version_hash"], start_offset=item["start_offset"],
                    quote=item["quote"], purpose="story_register",
                ).model_dump(mode="json") for item in evidence],
                "position": (pin["chapter_order"], ref["start_offset"]),
            })
    result = []
    for group, history in sorted(groups.items()):
        history.sort(key=lambda item: item["position"])
        latest = history[-1]
        details = latest["details"]
        result.append({
            "id": "observed-" + hashlib.sha256(json.dumps(group).encode()).hexdigest()[:20],
            "subject": details["subject"], "key": details["predicate"],
            "predicate": details["predicate"], "value": details["value"],
            "category": details["category"], "label": details["predicate"],
            "origin": "published_parse", "observation_kind": "recorded_observation",
            "identity_scope": "source_local", "entity_ref": details["entity_ref"],
            "certainty": details["certainty"], "interpretation_status": details["interpretation_status"],
            "qualifiers": details["qualifiers"], "history_count": len(history),
            "last_observation": latest["description"], "chapter_id": latest["chapter_id"],
            "as_of": "recorded_observation",
            "history": [{key: value for key, value in item.items() if key != "position"}
                        for item in history],
        })
    return result
