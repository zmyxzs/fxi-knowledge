"""Author inputs retained by the outline owner, independently of model judgments.

These are source snapshots, not a second set of inferred constraints. Their
meaning and continuing applicability are read in the current task's context.
"""
from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from typing import Any


_KINDS = {"author_request", "author_history", "assistant_context", "historical_context", "adopted_plan", "creative_note"}


def normalize_author_sources(value: Any, *, work_id: str | None = None,
                             chapter_id: str | None = None) -> dict[str, Any]:
    if (not isinstance(value, Mapping) or set(value) != {"schema_version", "sources"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or not isinstance(value["sources"], list)):
        raise ValueError("outline author_sources requires versioned source snapshots")
    identities = set()
    for row in value["sources"]:
        if (not isinstance(row, Mapping) or set(row) != {"id", "kind", "text", "locator"}
                or not isinstance(row["kind"], str) or row["kind"] not in _KINDS or not isinstance(row["text"], str)
                or not row["text"].strip() or not isinstance(row["locator"], Mapping)
                or not isinstance(row["id"], str) or row["id"] != _source_id(row)
                or row["id"] in identities):
            raise ValueError("outline author source has invalid identity or content")
        identities.add(row["id"])
        if work_id is not None and row["locator"].get("work_id") not in (None, work_id):
            raise ValueError("outline author source belongs to another work")
        if (chapter_id is not None and row["kind"] != "adopted_plan"
                and row["locator"].get("chapter_id") not in (None, chapter_id)):
            raise ValueError("outline author source belongs to another chapter")
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def build_author_sources(*, request: str, author_context=(), bindings=None,
                         previous=None, replace_creative_notes=False) -> dict[str, Any]:
    rows = (normalize_author_sources(previous)["sources"] if previous is not None else [])
    if replace_creative_notes:
        rows = [row for row in rows if row["kind"] != "creative_note"]

    def add(kind, text, locator):
        if not isinstance(text, str) or not text.strip():
            return
        row = {"kind": kind, "text": text, "locator": copy.deepcopy(dict(locator))}
        row["id"] = _source_id(row)
        # A source edit with a known message identity supersedes its old snapshot.
        # Absence from a bounded history does not mean the old requirement ceased.
        if locator.get("id"):
            rows[:] = [old for old in rows if not (
                old["kind"] == kind and old["locator"].get("id") == locator["id"]
                and all(old["locator"].get(key) == locator.get(key)
                        for key in ("work_id", "conversation_id")))]
        rows[:] = [old for old in rows if old["id"] != row["id"]]
        rows.append(row)

    for row in author_context or ():
        add("author_history" if row["role"] == "user" else "assistant_context",
            row["content"], row["source"])
    for binding in (bindings or {}).values():
        if binding.get("kind") == "creative_note":
            add("creative_note", binding["text"], {
                **binding["identity"], "title": binding["title"],
                "attributes": copy.deepcopy(binding["attributes"]),
            })
        if binding.get("kind") == "creative_plan" and binding.get("support_eligible") is True:
            add("adopted_plan", binding.get("text"), {
                **binding.get("identity", {}), "section": binding.get("section"),
            })
    add("author_request", request, {})
    return normalize_author_sources({"schema_version": 1, "sources": rows})


def author_sources_for_reading(content: Mapping[str, Any], *,
                               creative_note_context: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if creative_note_context is not None:
        context = normalize_creative_note_context(creative_note_context)
        historical = author_sources_for_reading(content)
        historical = project_author_sources(historical, context.get("author_inputs", []), context["pin"]["work_id"])
        bindings = {note["id"]: {
            "kind": "creative_note", "text": note["content"], "title": note["title"],
            "identity": {"work_id": context["pin"]["work_id"], **pin},
            "attributes": note["attributes"],
        } for note, pin in zip(context["notes"], context["pin"]["records"], strict=True)}
        return build_author_sources(request="", previous=historical, bindings=bindings,
                                    replace_creative_notes=True)
    attachment = content.get("author_sources")
    if attachment is not None:
        return normalize_author_sources(attachment)
    # Older outlines only kept author text when the model cited it. Recover
    # that available source without inventing missing messages or editing history.
    rows = {}
    for source in (content.get("continuity_context") or {}).get("sources", {}).values():
        kind = source.get("kind")
        if kind not in {"author_request", "author_history", "creative_plan"}:
            continue
        if kind == "creative_plan" and source.get("support_eligible") is not True:
            continue
        for excerpt in source.get("excerpts", []):
            if not isinstance(excerpt.get("text"), str) or not excerpt["text"].strip():
                continue
            row = {"kind": {"creative_plan": "adopted_plan", "author_history": "historical_context"}.get(kind, kind),
                   "text": excerpt["text"], "locator": copy.deepcopy(excerpt.get("locator") or {})}
            row["id"] = _source_id(row)
            rows[row["id"]] = row
    return normalize_author_sources({"schema_version": 1, "sources": list(rows.values())})


def author_source_input(row):
    """Locate legacy artifact attachments without treating plans as author speech."""
    source = copy.deepcopy(row["locator"])
    if not source.get("id"):
        source.update(id=row["id"], kind=row["kind"])
    elif row["kind"] not in {"author_history", "author_request"}:
        source.setdefault("kind", row["kind"])
    return {"role": "user" if row["kind"] in {"author_request", "author_history"} else "assistant",
            "content": row["text"], "source": source}


def merge_author_source_history(history, attachment, *, work_id):
    """Keep located author chronology without replaying anonymous copies of it."""
    result = copy.deepcopy(list(history))
    located = {row["content"] for row in result if row.get("role") == "user"
               and isinstance(row.get("content"), str)
               and (source := row.get("source") or {}).get("id")
               and source.get("conversation_id")
               and source.get("work_id") in (None, work_id)}
    for row in normalize_author_sources(attachment, work_id=work_id)["sources"]:
        locator = row["locator"]
        anonymous_request = (not locator.get("conversation_id") and (
            row["kind"] == "author_request" and not locator.get("id")
            or row["kind"] == "author_history" and locator.get("kind") == "author_request"))
        if anonymous_request and row["text"] in located:
            continue
        result.append(author_source_input(row))
    return result


def project_author_source_texts(attachment, author_context):
    """Share exact words in a model input while retaining each source's identity."""
    result = copy.deepcopy(attachment)
    texts = {}
    for row in author_context:
        if row.get("role") in {"user", "assistant"} and isinstance(row.get("basis"), str):
            texts.setdefault((row["role"], row["content"]), row["basis"])
    for row in result.get("sources", []):
        role = {"author_request": "user", "author_history": "user", "assistant_context": "assistant"}.get(row.get("kind"))
        basis = texts.get((role, row.get("text"))) if role is not None else None
        if basis is not None:
            row.pop("text")
            row["text_ref"] = basis
    return result


def project_author_sources(attachment, coverage, work_id):
    """Apply exact handled inputs to a reading projection, leaving the artifact intact."""
    from fxi_novel.knowledge.author_input_coverage import input_identity, text_hash
    result = normalize_author_sources(attachment)
    by_id = {row["id"]: row for row in coverage}
    projected = []
    for row in result["sources"]:
        match = by_id.get(input_identity(work_id, row["locator"], row["text"]))
        match = match or by_id.get(input_identity(work_id, author_source_input(row)["source"], row["text"]))
        if row["locator"].get("projection_hash") == text_hash(row["text"]):
            match = by_id.get(row["locator"].get("author_input_id")) or match
        if match is None and row["locator"].get("id") and not row["locator"].get("conversation_id"):
            matches = [item for item in coverage if item["source"].get("id") == row["locator"]["id"]
                       and item["original_hash"] == text_hash(row["text"])
                       and all(row["locator"].get(key) in (None, item["source"].get(key))
                               for key in ("chapter_id", "branch_id"))]
            if len(matches) == 1:
                match = matches[0]
        if match is None and row["kind"] == "author_request" and not row["locator"]:
            # Old request attachments had no locator. Only unanimous exact-body
            # dispositions can project them; never match numeric substrings.
            matches = [item for item in coverage if item["original_hash"] == text_hash(row["text"])]
            if matches and len({item["remaining_text"] for item in matches}) == 1:
                match = matches[0]
        if match is not None:
            if not match["remaining_text"].strip():
                continue
            row = {**row, "text": match["remaining_text"]}
            row["locator"] = {**row["locator"], "author_input_id": match["id"],
                              "projection_hash": text_hash(row["text"])}
            row["id"] = _source_id(row)
        projected.append(row)
    return {**result, "sources": projected}


def project_author_conversation(messages, coverage, *, conversation_id):
    """A legacy plan-sync message has no locator; restrict exact-body matches to its conversation."""
    from fxi_novel.knowledge.author_input_coverage import text_hash
    result = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, str) or not conversation_id:
            result.append(copy.deepcopy(message))
            continue
        candidates = [row for row in coverage if row["source"].get("conversation_id") == conversation_id
                      and row["original_hash"] == text_hash(content)
                      and (not message.get("id") or row["source"].get("id") == message["id"])]
        if candidates and len({row["remaining_text"] for row in candidates}) == 1:
            content = candidates[0]["remaining_text"]
        if content.strip():
            result.append({**copy.deepcopy(message), "content": content})
    return result


def normalize_creative_note_context(value: Mapping[str, Any], *,
                                    work_id: str | None = None) -> dict[str, Any]:
    """Validate an owner-read packet without treating it as a current database read."""
    if (not isinstance(value, Mapping) or set(value) not in ({"pin", "notes"}, {"pin", "notes", "author_inputs"})
            or not isinstance(value["pin"], Mapping)
            or set(value["pin"]) not in ({"work_id", "records"}, {"work_id", "records", "author_input_hash"},
                                        {"work_id", "records", "author_input_hash", "author_input_dependencies"})
            or not isinstance(value["pin"]["work_id"], str) or not value["pin"]["work_id"].strip()
            or work_id is not None and value["pin"]["work_id"] != work_id
            or not isinstance(value["pin"]["records"], list) or not isinstance(value["notes"], list)
            or len(value["notes"]) != len(value["pin"]["records"])):
        raise ValueError("creative-note reading context has an invalid scope or structure")
    ids = []
    for index, (note, pin) in enumerate(zip(value["notes"], value["pin"]["records"], strict=True), 1):
        if (not isinstance(pin, Mapping)
                or set(pin) != {"source_id", "edit_revision", "snapshot_id", "content_hash"}
                or type(pin["edit_revision"]) is not int or pin["edit_revision"] < 1
                or any(not isinstance(pin[key], str) or not pin[key].strip()
                       for key in ("source_id", "snapshot_id", "content_hash"))
                or len(pin["content_hash"]) != 64
                or any(char not in "0123456789abcdef" for char in pin["content_hash"])
                or not isinstance(note, Mapping)
                or set(note) != {"id", "title", "content", "attributes"}
                or note["id"] != f"N{index}"
                or not isinstance(note["title"], str) or not note["title"].strip()
                or not isinstance(note["content"], str) or not note["content"].strip()
                or hashlib.sha256(note["content"].encode("utf-8")).hexdigest() != pin["content_hash"]
                or not isinstance(note["attributes"], Mapping)):
            raise ValueError("creative-note reading context has an invalid source snapshot")
        ids.append(pin["source_id"])
    if ids != sorted(set(ids)):
        raise ValueError("creative-note reading context membership is invalid")
    from fxi_novel.knowledge.author_input_coverage import material_hash, material_dependencies
    coverage = value.get("author_inputs", [])
    if not isinstance(coverage, list):
        raise ValueError("author input reading context has an invalid dependency hash")
    for row in coverage:
        if (not isinstance(row, Mapping) or set(row) != {"id", "source", "original_hash", "remaining_text", "resolution_id", "disposition"}
                or not isinstance(row["source"], Mapping) or not isinstance(row["remaining_text"], str)
                or not isinstance(row["disposition"], str) or row["disposition"] not in {"active", "recorded", "withdrawn", "context"}
                or any(not isinstance(row[key], str) or len(row[key]) != 64
                       for key in ("id", "original_hash", "resolution_id"))):
            raise ValueError("author input reading context has an invalid disposition")
    if material_hash(coverage) != value["pin"].get("author_input_hash"):
        raise ValueError("author input reading context has an invalid dependency hash")
    if ("author_input_dependencies" in value["pin"]
            and value["pin"]["author_input_dependencies"] != material_dependencies(coverage)):
        raise ValueError("author input reading context has invalid input versions")
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def render_author_sources(content: Mapping[str, Any], *,
                          creative_note_context: Mapping[str, Any] | None = None) -> str:
    rows = author_sources_for_reading(content, creative_note_context=creative_note_context)["sources"]
    if not rows:
        return ""
    labels = {"author_request": "规划时的作者任务", "author_history": "作者历史原话",
              "assistant_context": "用于理解作者短答的助手上下文", "historical_context": "效力未明的历史上下文",
              "adopted_plan": "当时采用的计划栏目", "creative_note": "规划时读取的创作笔记版本"}
    if creative_note_context is not None:
        labels["creative_note"] = "本轮读取的当前创作笔记"
    text = ["章纲版本保留的作者来源：结合当前任务判断仍适用的创作要求，后来的修改优先。"
            "历史操作不重新执行；助手上下文不是作者要求；本任务确认不等于永久设定。"
            "相同作者原话只展开一次，重复来源保留原位置并指向原文项。"]
    author_text_positions: dict[str, int] = {}
    for index, row in enumerate(rows, 1):
        qualification = ""
        if row["kind"] == "creative_note":
            locator = row["locator"]
            attributes = locator.get("attributes", {})
            status = attributes.get("decision_status") if isinstance(attributes, Mapping) else None
            if status == "confirmed":
                authority = "作者已确认的创作决定，按所记范围生效。"
            elif status == "proposal":
                authority = "待选创作提案，尚未确认为作者决定。"
            else:
                authority = "创作记录的决定状态未明，不能自行视为已确认。"
            qualification = (f"\n标题：{locator.get('title', '')}\n记录属性："
                             + json.dumps(attributes, ensure_ascii=False)
                             + "\n" + authority + "不表示事件已发生或人物已知。")
        body = row["text"]
        if row["kind"] in {"author_request", "author_history"}:
            previous_position = author_text_positions.setdefault(body, index)
            if previous_position != index:
                body = f"原话文字同第{previous_position}项。"
        text.append(f"{index}. {labels[row['kind']]}{qualification}\n{body}")
    return "\n\n".join(text)


def project_outline_authority(content: Mapping[str, Any], *, confirmed: bool = False,
                              creative_note_context: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Downgrade identifiable legacy model interpretations without rewriting artifacts."""
    projected = copy.deepcopy(dict(content))
    if creative_note_context is not None:
        context = normalize_creative_note_context(creative_note_context)
        continuity = projected.get("continuity_context") or {}
        sources = continuity.get("sources", {})
        changed = set()
        notes = {pin["source_id"]: note for pin, note in zip(context["pin"]["records"], context["notes"], strict=True)}
        for ref, source in sources.items():
            if source.get("kind") in {"author_request", "author_history"}:
                for excerpt in source.get("excerpts", []):
                    row = {"kind": source["kind"], "text": excerpt["text"], "locator": excerpt["locator"]}
                    row["id"] = _source_id(row)
                    original = {"schema_version": 1, "sources": [row]}
                    if project_author_sources(original, context.get("author_inputs", []), context["pin"]["work_id"]) != original:
                        changed.add(ref)
            elif source.get("kind") == "creative_note":
                for excerpt in source.get("excerpts", []):
                    current = notes.get(excerpt["locator"].get("source_id"))
                    if current is None or current["content"] != excerpt["text"]:
                        changed.add(ref)
        if changed:
            # These judgments depended on replaced author input, not on an
            # immutable story passage. Do not hand their old inference to a
            # writer as a current constraint; the current notes/remaining input
            # still deliver every retained condition through their own source.
            retired = [row for row in continuity.get("judgments", []) if changed.intersection(row["basis"])]
            continuity["judgments"] = [row for row in continuity.get("judgments", []) if row not in retired]
            used = {ref for row in continuity["judgments"] for ref in row["basis"]}
            continuity["sources"] = {ref: row for ref, row in sources.items() if ref in used}
            projected["must_keep"] = [text for text in projected.get("must_keep", [])
                                      if text not in {row["statement"] for row in retired}]
    if confirmed:
        return projected
    judgments = (projected.get("continuity_context") or {}).get("judgments", [])
    interpreted = {row["statement"] for row in judgments if row.get("kind") == "author_constraint"}
    if interpreted:
        projected["must_keep"] = [text for text in projected.get("must_keep", []) if text not in interpreted]
        for row in (projected.get("continuity_context") or {}).get("judgments", []):
            if row.get("kind") == "author_constraint":
                row["continuity"] = False
    return projected


def _source_id(row):
    identity = {key: row[key] for key in ("kind", "text", "locator")}
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True,
                                    allow_nan=False, separators=(",", ":")).encode()).hexdigest()
