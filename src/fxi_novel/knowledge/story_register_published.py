"""Read-only register projection of the parser owner's published observations."""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict

from .candidate_query_projection import typed_payload_evidence_is_authorized
from .query_contracts import EvidenceRef
from .story_observations import source_local_observations


def published_register_records(records, *, source_pins, branch_id, work_id=None):
    pins = {row["source_id"]: row for row in source_pins}
    groups = defaultdict(list)
    projected_ids = set()
    for record in records:
        content = record["content"]
        if content.get("branch_id") != branch_id or content.get("candidate_role") != "fact":
            continue
        # Speech, belief and inference stay in the existing perspective query.
        # A supported attribution is not proof that its claim is a world fact.
        if content.get("claim_basis") not in {"narration", "observation", "author_note"}:
            continue
        identity = content.get("identity_group_id")
        if not identity or content.get("identity_resolution") == "unresolved":
            continue
        state_slot = content.get("state_slot")
        quantity, rule = content.get("typed_quantity"), content.get("typed_rule")
        evidence = [ref for ref in record.get("evidence", [])
                    if (pin := pins.get(ref["source_id"])) and pin["snapshot_id"] == ref["snapshot_id"]
                    and pin["content_hash"] == ref["source_version_hash"]]
        if quantity or rule:
            authorized = tuple(EvidenceRef.from_exact_span(space="writing", work_id=work_id,
                branch_id=branch_id, source_id=ref["source_id"], snapshot_id=ref["snapshot_id"],
                content_hash=ref["source_version_hash"], start_offset=ref["start_offset"], quote=ref["quote"])
                for ref in evidence)
            if not typed_payload_evidence_is_authorized(quantity or rule, authorized):
                continue
        if content.get("category") == "temporal_state" and state_slot:
            kind, slot, mode = "state", state_slot, "state"
        elif quantity:
            kind, slot, mode = "quantity", quantity["attribute"], "state"
        elif rule:
            kind, slot, mode = "rule", rule["name"], "rule"
        else:
            continue
        # No label guessing between manually registered keys and parser identities.
        # Scoped observations cannot overwrite an unconditional state observation.
        qualifiers = {key: content[key] for key in (
            "story_time_expression", "story_order", "valid_from_story_order", "valid_until_story_order",
            "claim_basis", "observed_by", "holder", "audience",
        ) if content.get(key) is not None}
        details = {"qualifiers": qualifiers}
        details["state_value_status"] = content.get("state_value_status", "known_value")
        if quantity:
            details["quantity"] = {key: quantity[key] for key in (
                "attribute", "quantity", "time_expression", "condition_event_ids", "condition_record_ids",
            ) if quantity.get(key) is not None}
        if rule:
            details["rule"] = {key: rule[key] for key in (
                "name", "conditions", "effect", "limitations", "costs", "exceptions", "scope",
            ) if rule.get(key) is not None}
        scope = json.dumps(qualifiers, ensure_ascii=False, sort_keys=True)
        if quantity:
            scope += json.dumps({key: quantity.get(key) for key in (
                "condition_event_ids", "condition_record_ids", "time_expression")}, sort_keys=True)
        for ref in evidence:
            pin = pins.get(ref["source_id"])
            if (not pin or pin["snapshot_id"] != ref["snapshot_id"]
                    or pin["content_hash"] != ref["source_version_hash"]):
                continue
            groups[(identity, kind, slot, scope)].append({
                "id": record["id"], "version_id": record["version_id"], "chapter_id": pin["chapter_id"],
                "subject": content.get("identity_display_name") or content["subject"],
                "label": (content.get("predicate") or slot) if kind == "state" else slot,
                "description": content.get("value") or ref["quote"], "mode": mode,
                "amount": None, "unit": None, "event_kind": None,
                "source_id": ref["source_id"], "snapshot_id": ref["snapshot_id"],
                "content_hash": ref["source_version_hash"], "start": ref["start_offset"],
                "end": ref["end_offset"], "quote": ref["quote"], "details": details,
                "position": (pin["chapter_order"], ref["start_offset"]),
            })
            projected_ids.add(record["id"])
    result = []
    for group, history in sorted(groups.items()):
        history.sort(key=lambda item: item["position"])
        latest = history[-1]
        same_point = [item for item in history if item["position"] == latest["position"]]
        conflict = len({json.dumps([item["description"], item["details"]], sort_keys=True)
                        for item in same_point}) > 1
        result.append({
            "id": "parsed-" + hashlib.sha256(json.dumps(group).encode()).hexdigest()[:20],
            "subject": latest["subject"], "identity_group_id": group[0], "key": group[2],
            "category": "rule" if group[1] == "rule" else "observation", "label": latest["label"],
            "origin": "published_parse", "observation_kind": group[1],
            "last_observation": "；".join(dict.fromkeys(item["description"] for item in same_point)),
            "chapter_id": latest["chapter_id"],
            "as_of": "conflicting_observations" if conflict else "latest_recorded_observation",
            "history": [{key: value for key, value in item.items() if key not in {"position", "subject", "label"}}
                        for item in history],
        })
    result.extend(source_local_observations(
        [record for record in records if record["id"] not in projected_ids],
        source_pins=source_pins, branch_id=branch_id,
    ))
    return result
