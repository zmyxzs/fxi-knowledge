"""Evidence-backed register views over the existing versioned knowledge owner."""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .asset_projection import AssetProjectionScope, project_asset_balance
from .event_state import NarrativePosition, TypedEvent
from .query_contracts import EvidenceRef


class StoryRegisterEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    subject: str = Field(min_length=1, max_length=200)
    key: str = Field(pattern=r"^[a-z][a-z0-9_.-]*$", max_length=120)
    category: Literal["currency", "inventory", "skill", "level", "condition", "identity", "relationship", "goal", "rule"]
    label: str = Field(min_length=1, max_length=160)
    mode: Literal["state", "change", "rule"]
    description: str = Field(min_length=1, max_length=1800)
    amount: Decimal | None = None
    unit: str | None = Field(default=None, min_length=1, max_length=40)
    event_kind: Literal["balance_snapshot", "acquire", "consume"] | None = None
    source_id: str
    snapshot_id: str
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    quote: str = Field(min_length=1, max_length=5000)

    @model_validator(mode="after")
    def quantities_are_explicit(self):
        if self.end - self.start != len(self.quote):
            raise ValueError("register quote must match its exact source interval")
        if self.event_kind is not None:
            if self.amount is None or self.amount < 0 or not self.unit:
                raise ValueError("numeric events require an exact nonnegative amount and unit")
            expected = "state" if self.event_kind == "balance_snapshot" else "change"
            if self.mode != expected:
                raise ValueError("balance snapshots and changes have different meanings")
        elif self.amount is not None or self.unit is not None:
            raise ValueError("numeric register values require an explicit event kind")
        return self


def register_identity(work_id, branch_id, entry):
    point = [work_id, branch_id, entry.subject, entry.key, entry.mode,
             entry.source_id, entry.snapshot_id, entry.start, entry.end]
    return "story-register:" + hashlib.sha256(json.dumps(point, ensure_ascii=False).encode()).hexdigest()


def project_register(records, *, source_pins, work_id, branch_id, manifest_revision, manifest_hash, truncated=False):
    """A last observed state is not a proven current balance or a complete ledger."""
    pins = {row["source_id"]: row for row in source_pins}
    groups, excluded = defaultdict(list), []
    for record in records:
        content = record["content"]
        if content.get("predicate") != "writing.register":
            continue
        if record["status"] != "reviewed" or content.get("branch_id") != branch_id:
            excluded.append({"id": record["id"], "reason": "not_reviewed_in_branch"})
            continue
        try:
            entry = StoryRegisterEntry.model_validate(content["value"])
        except ValueError:
            excluded.append({"id": record["id"], "reason": "invalid_register_value"})
            continue
        pin = pins.get(entry.source_id)
        evidence = next((ref for ref in record["evidence"] if ref["source_id"] == entry.source_id
                         and ref["snapshot_id"] == entry.snapshot_id
                         and ref["source_version_hash"] == entry.content_hash
                         and ref["start_offset"] <= entry.start and entry.end <= ref["end_offset"]
                         and ref["quote"][entry.start-ref["start_offset"]:entry.end-ref["start_offset"]] == entry.quote), None)
        if not pin or pin["snapshot_id"] != entry.snapshot_id or pin["content_hash"] != entry.content_hash or evidence is None:
            excluded.append({"id": record["id"], "reason": "outside_current_adopted_sources"})
            continue
        groups[(entry.subject, entry.key)].append({"id": record["id"], "version_id": record["version_id"],
            "entry": entry, "evidence": evidence, "position": (pin["chapter_order"], entry.start),
            "chapter_id": pin["chapter_id"]})
    result = []
    for (subject, key), history in sorted(groups.items()):
        history.sort(key=lambda row: row["position"])
        latest = history[-1]
        entry = latest["entry"]
        identity = hashlib.sha256(json.dumps([subject, key], ensure_ascii=False).encode()).hexdigest()[:20]
        row = {"id": identity, "subject": subject, "key": key, "category": entry.category,
               "label": entry.label, "last_observation": entry.description,
               "chapter_id": latest["chapter_id"], "as_of": "latest_recorded_observation",
               "history": [{"id": item["id"], "version_id": item["version_id"], "chapter_id": item["chapter_id"],
                            **item["entry"].model_dump(mode="json")} for item in history]}
        events = []
        for item in history:
            value = item["entry"]
            if value.event_kind is None:
                continue
            events.append(TypedEvent(id=item["id"], canonical_event_id=item["id"], kind=value.event_kind,
                disposition="actual", asset_id=key, asset_granularity="type", actor_id=subject,
                holder_before_id=subject if value.event_kind == "consume" else None,
                holder_after_id=None if value.event_kind == "consume" else subject,
                quantity={"kind": "exact", "amount": value.amount, "unit": value.unit},
                claim_basis="narration", branch_id=branch_id,
                narrative_source_id=value.source_id, narrative_snapshot_id=value.snapshot_id,
                narrative_position=NarrativePosition(chapter_order=item["position"][0], offset=value.start,
                    manifest_revision=manifest_revision, manifest_hash=manifest_hash),
                evidence_refs=[EvidenceRef.from_exact_span(space="writing", work_id=work_id, branch_id=branch_id,
                    source_id=value.source_id, snapshot_id=value.snapshot_id, content_hash=value.content_hash,
                    start_offset=value.start, quote=value.quote, purpose="story_register")]))
        if events:
            row["quantity_projection"] = project_asset_balance(events, asset_id=key, holder_id=subject,
                scope=AssetProjectionScope(branch_id=branch_id, coverage_status="partial",
                    as_of_narrative_position=NarrativePosition(chapter_order=latest["position"][0],
                        offset=latest["position"][1], manifest_revision=manifest_revision, manifest_hash=manifest_hash)),
                event_support={event.id: "supported" for event in events}).model_dump(mode="json")
        # Parallel claims at the same point remain visible; neither wins by ID.
        same_point = [item for item in history if item["position"] == latest["position"]]
        if len({item["entry"].description for item in same_point}) > 1:
            row["as_of"] = "conflicting_observations"
            row["last_observation"] = "；".join(item["entry"].description for item in same_point)
        result.append(row)
    return {"work_id": work_id, "branch_id": branch_id, "records": result,
            "truncated": truncated, "excluded": excluded, "ledger_coverage": "partial"}


def register_catalog(register):
    return [{**{key: row[key] for key in ("id", "subject", "category", "label", "last_observation", "chapter_id", "as_of")},
             **{key: row[key] for key in ("origin", "observation_kind", "identity_group_id",
                 "identity_scope", "entity_ref", "predicate", "certainty", "interpretation_status",
                 "qualifiers", "history_count") if key in row},
             **({"basis": row["basis"]} if "basis" in row else {}),
             **({"balance_status": row["quantity_projection"]["status"], "exact_balance": row["quantity_projection"]["exact_balance"]}
                if "quantity_projection" in row else {})}
            for row in register.get("records", [])]


def register_material(row):
    result = {key: row[key] for key in ("subject", "category", "label", "last_observation", "chapter_id", "as_of")}
    result.update({key: row[key] for key in ("origin", "observation_kind", "identity_group_id",
        "identity_scope", "entity_ref", "predicate", "value", "certainty", "interpretation_status",
        "qualifiers", "history_count") if key in row})
    result["history"] = [{key: item[key] for key in ("chapter_id", "description", "mode", "amount", "unit", "quote")}
                         for item in row["history"]]
    for projected, original in zip(result["history"], row["history"]):
        projected.update({key: original[key] for key in (
            "id", "version_id", "source_id", "snapshot_id", "content_hash", "start", "end",
            "branch_id",
        ) if key in original})
        other_refs = [reference for reference in original.get("evidence_refs", [])
                      if not (reference.get("source_id") == original.get("source_id")
                              and reference.get("snapshot_id") == original.get("snapshot_id")
                              and reference.get("start_offset") == original.get("start")
                              and reference.get("end_offset") == original.get("end")
                              and reference.get("quote") == original.get("quote"))]
        if other_refs:
            projected["evidence_refs"] = other_refs
        if "details" in original:
            projected["details"] = original["details"]
        if original.get("reading_context"):
            projected["observation_quote"] = original["quote"]
            projected["quote"] = original["reading_context"]["quote"]
            if "source_id" in original["reading_context"]:
                projected["reading_reference"] = {key: value for key, value in original["reading_context"].items()
                                                  if key != "quote"}
    if "quantity_projection" in row:
        result["quantity"] = {key: row["quantity_projection"][key] for key in
                              ("status", "exact_balance", "known_net_change", "net_change_complete", "ledger_coverage", "unit")}
    return result
