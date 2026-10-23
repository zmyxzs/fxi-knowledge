"""Version-local edits with one projection of each editable style-card field."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import re
from typing import Any

from jsonschema import ValidationError, validate

from fxi_novel.knowledge.writing_methods import StyleMethodFields, StoredWritingStyleMethodPayload
from .service_support import parse_model_json_object
from .style_fingerprint import DnaFingerprint, StyleExcerptSelection
from .style_study_revision import _apply_text_edits, study_revision_blocks


_VALUE = {"oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]}
LEGACY_CARD_REVISION_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["edits", "scope_expansion"],
    "properties": {
        "edits": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["target_id", "field", "old_value", "new_value"],
            "properties": {"target_id": {"type": "string", "minLength": 1},
                "field": {"type": "string", "minLength": 1}, "old_value": _VALUE, "new_value": _VALUE}}},
        "scope_expansion": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["target_id", "reason"],
            "properties": {"target_id": {"type": "string", "minLength": 1},
                "reason": {"type": "string", "minLength": 1}}}},
    },
}
CARD_REVISION_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["replacements", "scope_expansion"],
    "properties": {
        "replacements": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["target_id", "fields"],
            "properties": {"target_id": {"type": "string", "minLength": 1},
                "fields": {"type": "object", "minProperties": 1, "additionalProperties": _VALUE}}}},
        "scope_expansion": deepcopy(LEGACY_CARD_REVISION_SCHEMA["properties"]["scope_expansion"]),
    },
}


def card_replacement_schema(targets: list[dict], selected: list[str]) -> dict:
    """Project the same exact-field contract enforced by the local applier."""
    schema = deepcopy(CARD_REVISION_SCHEMA)
    variants = []
    known = {row["target_id"]: row for row in targets}
    for identity in selected:
        target = known[identity]
        if not target["fields"]:
            continue
        properties = {key: ({"type": "string"} if isinstance(value, str)
            else {"type": "array", "items": {"type": "string"}})
            for key, value in target["fields"].items()}
        variants.append({"type": "object", "additionalProperties": False,
            "required": ["target_id", "fields"], "properties": {
                "target_id": {"type": "string", "const": identity},
                "fields": {"type": "object", "additionalProperties": False,
                    "properties": properties, "required": list(properties)}}})
    if variants:
        schema["properties"]["replacements"]["items"] = {"oneOf": variants}
    else:
        schema["properties"]["replacements"]["maxItems"] = 0
    return schema


def render_style_method(data: dict[str, Any]) -> str:
    return (f"### {data['title']}\n\n观察：{data['observation']}\n\n操作：{data['technique']}\n\n"
            f"适用：{data['use_when']}\n\n例外：{data['avoid_when']}\n\n"
            f"来源情境：{'；'.join(data['observed_contexts'])}\n\n作用：{data['effect']}\n\n"
            f"保留：{'；'.join(data['preserve'])}\n\n"
            + " ".join(f"【EVIDENCE:{identity}】" for identity in data["evidence_ids"]))


def _at(content: dict[str, Any], path: tuple) -> Any:
    value = content
    for key in path:
        value = value[key]
    return value


def _put(content: dict[str, Any], path: tuple, value: Any) -> None:
    parent = _at(content, path[:-1])
    parent[path[-1]] = deepcopy(value)


_QUOTE_LABEL = re.compile(r"^\s*(?:[-*]\s+)?(?:\*\*)?原句(?:\*\*)?\s*[:：]")
_INLINE_QUOTE_LABEL = re.compile(r"(?:\*\*)?原句(?:\*\*)?\s*[:：]")
_CONDITION_LABEL = r"(?:条件|适用条件|使用条件)(?:(?:与|及|和)[^：:\n]{1,12})?"
_INLINE_QUOTE_END = re.compile(r"[（(]s\d+[^）)\n]*[）)]\s*(?=(?:\*\*)?" + _CONDITION_LABEL + r"(?:\*\*)?\s*[:：])")
_SUMMARY_BOUNDARY = re.compile(r"(?m)^(?:#{1,4}\s+[^\n]+|【[^\n]+】[^\S\n]*$|\d+[.、]\s+[^\n]+)")
_SENTENCE_FIELD = re.compile(r"^\s*(?:[-*]\s+)?(?:\*\*)?(?:骨架|" + _CONDITION_LABEL + r"|说明|学习点|不适用)(?:\*\*)?\s*[:：]")


def _sentence_quote_spans(markdown: str) -> list[tuple[int, int]]:
    spans, start, offset, fence = [], None, 0, None
    for line in markdown.splitlines(keepends=True):
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if start is not None and fence is None and (_QUOTE_LABEL.match(line) or _SENTENCE_FIELD.match(line)):
            spans.append((start, offset))
            start = None
        if start is None and _QUOTE_LABEL.match(line):
            start = offset
        elif start is None and (inline := _INLINE_QUOTE_LABEL.search(line)):
            ending = _INLINE_QUOTE_END.search(line, inline.end())
            spans.append((offset + inline.start(), offset + (ending.end() if ending else len(line))))
        if start is not None and marker:
            value = marker.group(1)
            if fence is None:
                fence = value
            elif value[0] == fence[0] and len(value) >= len(fence):
                fence = None
        offset += len(line)
    if start is not None:
        spans.append((start, len(markdown)))
    return spans


def _sentence_quote_lines(markdown: str) -> list[str]:
    # Include continuations and locators, not just the first labelled quote line.
    return [markdown[start:end] for start, end in _sentence_quote_spans(markdown)]


def _summary_units(markdown: str) -> tuple[list, dict]:
    quote_spans = _sentence_quote_spans(markdown)
    boundaries = [row for row in _SUMMARY_BOUNDARY.finditer(markdown)
                  if not any(start <= row.start() < end for start, end in quote_spans)]
    starts = sorted({0, *(row.start() for row in boundaries), len(markdown)})
    targets, bindings, section = [], {}, "文风观察"
    for index, (start, end) in enumerate(zip(starts, starts[1:]), 1):
        text = markdown[start:end]
        heading = text.splitlines()[0] if text.splitlines() else ""
        label = re.split(r"(?:骨架|原句)\s*[:：]", heading, maxsplit=1)[0].strip()
        if heading.startswith(("#", "【")):
            section = heading.strip("# \r\n")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        identity = f"summary-unit-{index:04d}-{digest}"
        fields, spans, quotes = {}, {}, []
        cursor = start
        for quote_start, quote_end in quote_spans:
            if start <= quote_start < end:
                if quote_start > cursor:
                    field = f"part_{len(fields) + 1}"
                    fields[field], spans[field] = markdown[cursor:quote_start], (cursor, quote_start)
                quotes.append({"after_field": next(reversed(fields), None), "text": markdown[quote_start:quote_end]})
                cursor = quote_end
        if cursor < end:
            field = "markdown" if not quotes else f"part_{len(fields) + 1}"
            fields[field], spans[field] = markdown[cursor:end], (cursor, end)
        if not fields:
            # A quote-only unit remains visible but cannot be selected for changes.
            fields, spans = {}, {}
        target = {"target_id": identity, "kind": "summary", "label": section if heading == section else f"{section} / {label}",
                  "fields": fields}
        if quotes:
            target["read_only_quotes"] = quotes
        targets.append(target)
        bindings[identity] = {"summary_spans": spans}
    return targets, bindings


def card_revision_targets(item: dict[str, Any], samples: list[dict[str, Any]], *, legacy: bool = False) -> tuple[list, dict]:
    from .service_style import _style_selection_pin

    content = item["content"]
    fingerprint = content["author_style_fingerprint"]
    methods = content.get("craft_method_payload", {}).get("methods", [])
    if legacy:
        targets, bindings = [], {}
        for block in study_revision_blocks(fingerprint["summary"]):
            identity = "summary-" + block["block_id"]
            targets.append({"target_id": identity, "kind": "summary", "label": block["label"],
                            "fields": {"markdown": block["markdown"]}})
            bindings[identity] = {"summary_block": block["block_id"]}
    else:
        targets, bindings = _summary_units(fingerprint["summary"])

    # Pair by exact semantics AND source coordinates, not title or list position.
    used_operations = set()
    for index, method in enumerate(methods):
        matches = []
        for op_index, operation in enumerate(fingerprint.get("operations", [])):
            if any(operation.get(key) != method.get(key) for key in StyleMethodFields.model_fields):
                continue
            example_map = []
            for example in operation["examples"]:
                pin = _style_selection_pin(StyleExcerptSelection.model_validate(example["source_selection"]), samples)
                references = [ref for rows in method["coverage"].values() for ref in rows
                    if ref["source_id"] == pin["source_id"] and ref["snapshot_id"] == pin["snapshot_id"]
                    and ref["start_offset"] == pin["start_offset"]
                    and ref["end_offset"] == pin["start_offset"] + len(pin["text"])]
                found = [i for i, row in enumerate(method["examples"])
                    if any(row["evidence_id"] == ref["evidence_id"] for ref in references)
                    and all(row[key] == example[key] for key in ("role", "material_kind", "explanation"))]
                if len(found) != 1:
                    break
                example_map.append(found[0])
            else:
                matches.append((op_index, example_map))
        if len(matches) != 1 or matches[0][0] in used_operations:
            raise ValueError("style card method has no unique matching current operation; reconcile its projections first")
        op_index, example_map = matches[0]
        used_operations.add(op_index)
        identity = "method-" + method["method_id"]
        method_path = ("craft_method_payload", "methods", index)
        operation_path = ("author_style_fingerprint", "operations", op_index)
        paths = {key: [method_path + (key,), operation_path + (key,)] for key in StyleMethodFields.model_fields}
        fields = {key: deepcopy(method[key]) for key in StyleMethodFields.model_fields}
        for op_example, method_example in enumerate(example_map):
            key = f"example_{op_example + 1}_explanation"
            fields[key] = method["examples"][method_example]["explanation"]
            paths[key] = [method_path + ("examples", method_example, "explanation"),
                          operation_path + ("examples", op_example, "explanation")]
        targets.append({"target_id": identity, "kind": "method", "label": method["title"],
                        "fields": fields, "evidence_ids": [row["evidence_id"] for row in method["examples"]]})
        bindings[identity] = {"paths": paths}

    anchors = fingerprint.get("golden_voice_slices", [])
    directory = content.get("style_example_profile", {}).get("examples", [])
    evidence = {row["id"]: row for row in item["evidence"]}
    if len(anchors) != len(directory):
        raise ValueError("style card anchor directory differs from its fingerprint")
    for index, (anchor, entry) in enumerate(zip(anchors, directory)):
        span = evidence.get(entry["evidence_id"], {})
        if (anchor["quote"] != span.get("quote", span.get("text"))
                or any(anchor.get(key, "") != entry.get(key, "") for key in ("title", "cadence_notes"))):
            raise ValueError("style card anchor directory differs from its exact source or explanation")
        paths = {key: [("author_style_fingerprint", "golden_voice_slices", index, key),
                       ("style_example_profile", "examples", index, key)] for key in ("title", "cadence_notes")}
        # Synthesis copied anchor notes into supplemental context examples only.
        for mi, method in enumerate(methods):
            for ei, example in enumerate(method["examples"]):
                if (example["evidence_id"] == entry["evidence_id"] and example["role"] == "context"
                        and example["explanation"] == anchor.get("cadence_notes")
                        and not any(("craft_method_payload", "methods", mi, "examples", ei, "explanation") in values
                            for binding in bindings.values() for values in binding.get("paths", {}).values())):
                    paths["cadence_notes"].append(("craft_method_payload", "methods", mi, "examples", ei, "explanation"))
        identity = f"anchor-{index + 1}-{entry['evidence_id']}"
        targets.append({"target_id": identity, "kind": "anchor", "label": anchor["title"],
                        "fields": {key: anchor.get(key, "") for key in paths}, "evidence_ids": [entry["evidence_id"]]})
        bindings[identity] = {"paths": paths}
    return targets, bindings


def apply_card_revision(content: dict, targets: list, bindings: dict, selected: list[str], response_text: str) -> tuple[dict, dict]:
    """Replay the exact historical edit contract, never repair or reinterpret it."""
    try:
        response = parse_model_json_object(response_text)
        validate(response, LEGACY_CARD_REVISION_SCHEMA)
    except (ValueError, ValidationError) as exc:
        raise ValueError("style card refinement returned an invalid edit contract") from exc
    known = {row["target_id"]: row for row in targets}
    if not set(selected) <= known.keys():
        raise ValueError("style card targets do not belong to this exact version")
    expansion = set()
    for row in response["scope_expansion"]:
        if row["target_id"] not in known.keys() - set(selected) or row["target_id"] in expansion or not row["reason"].strip():
            raise ValueError("style card scope expansion must identify distinct unselected targets")
        expansion.add(row["target_id"])
    grouped = {}
    for index, edit in enumerate(response["edits"], 1):
        identity, field = edit["target_id"], edit["field"]
        if identity not in selected or field not in known[identity]["fields"]:
            raise ValueError(f"style card edit {index} is outside selected fields: {identity}.{field}")
        original, old = known[identity]["fields"][field], edit["old_value"]
        if isinstance(original, str) and isinstance(old, str) and old:
            start = original.find(old)
            if start < 0 or original.find(old, start + 1) >= 0:
                raise ValueError(f"style card edit {index} must match exactly once in its original field: {identity}.{field}")
        grouped.setdefault((identity, field), []).append(edit)
    updates = {}
    for (identity, field), edits in grouped.items():
        original = known[identity]["fields"][field]
        if isinstance(original, str):
            if original == "" and len(edits) == 1 and edits[0]["old_value"] == "" and isinstance(edits[0]["new_value"], str):
                updates[(identity, field)] = edits[0]["new_value"]
                continue
            if any(not isinstance(row["old_value"], str) or not row["old_value"]
                   or not isinstance(row["new_value"], str) for row in edits):
                raise ValueError("style card text edits require nonempty exact old text")
            try:
                value = _apply_text_edits(original, [{"old_text": row["old_value"], "new_text": row["new_value"]} for row in edits])
            except ValueError as exc:
                raise ValueError(f"invalid style card text edits for {identity}.{field}: {exc}") from exc
        else:
            if len(edits) != 1 or edits[0]["old_value"] != original or not isinstance(edits[0]["new_value"], list):
                raise ValueError("style card list edits require its exact complete original value")
            value = edits[0]["new_value"]
        updates[(identity, field)] = value
    if expansion:
        return deepcopy(content), response
    return _project_updates(content, bindings, updates), response


def apply_card_replacements(content: dict, targets: list, bindings: dict, selected: list[str], response_text: str) -> tuple[dict, dict]:
    try:
        response = parse_model_json_object(response_text)
        validate(response, CARD_REVISION_SCHEMA)
    except (ValueError, ValidationError) as exc:
        raise ValueError("style card refinement returned an invalid replacement contract") from exc
    known = {row["target_id"]: row for row in targets}
    if not set(selected) <= known.keys():
        raise ValueError("style card targets do not belong to this exact version")
    expansions = set()
    for row in response["scope_expansion"]:
        if row["target_id"] not in known.keys() - set(selected) or row["target_id"] in expansions or not row["reason"].strip():
            raise ValueError("style card scope expansion must identify distinct unselected targets")
        expansions.add(row["target_id"])
    seen, updates = set(), {}
    for index, replacement in enumerate(response["replacements"], 1):
        identity, fields = replacement["target_id"], replacement["fields"]
        if identity not in selected or identity in seen:
            raise ValueError(f"style card replacement {index} has an unselected or repeated target: {identity}")
        seen.add(identity)
        original = known[identity]["fields"]
        if not original or fields.keys() != original.keys():
            missing, extra = sorted(original.keys() - fields.keys()), sorted(fields.keys() - original.keys())
            raise ValueError(f"style card replacement {index} must contain exactly the complete editable fields: "
                             f"{identity}; missing={missing}, extra={extra}")
        for field, value in fields.items():
            if type(value) is not type(original[field]):
                raise ValueError(f"style card replacement {index} has the wrong field type: {identity}.{field}")
            updates[(identity, field)] = value
    if expansions:
        return deepcopy(content), response
    return _project_updates(content, bindings, updates), response


def _retain_boundary_newlines(original: str, replacement: str) -> str:
    """Keep paragraph separators owned by the card when replacing local text."""
    leading = len(original) - len(original.lstrip("\n"))
    trailing = len(original) - len(original.rstrip("\n"))
    replacement_leading = len(replacement) - len(replacement.lstrip("\n"))
    replacement_trailing = len(replacement) - len(replacement.rstrip("\n"))
    return ("\n" * max(0, leading - replacement_leading) + replacement
            + "\n" * max(0, trailing - replacement_trailing))


def _project_updates(content: dict, bindings: dict, updates: dict) -> dict:
    updated = deepcopy(content)
    summary = {row["block_id"]: row for row in study_revision_blocks(content["author_style_fingerprint"]["summary"])}
    summary_spans = []
    for (identity, field), value in updates.items():
        binding = bindings[identity]
        if "summary_block" in binding:
            block = summary[binding["summary_block"]]
            block["markdown"] = _retain_boundary_newlines(block["markdown"], value)
        elif "summary_spans" in binding:
            start, end = binding["summary_spans"][field]
            original = content["author_style_fingerprint"]["summary"][start:end]
            summary_spans.append((start, end, _retain_boundary_newlines(original, value)))
        else:
            for path in binding["paths"][field]:
                _put(updated, path, value)
    summary_text = "".join(row["markdown"] for row in summary.values())
    for start, end, value in sorted(summary_spans, reverse=True):
        summary_text = summary_text[:start] + value + summary_text[end:]
    updated["author_style_fingerprint"]["summary"] = summary_text
    if not updated["author_style_fingerprint"]["summary"].strip():
        raise ValueError("style card refinement cannot remove the complete summary")
    if (_sentence_quote_lines(updated["author_style_fingerprint"]["summary"])
            != _sentence_quote_lines(content["author_style_fingerprint"]["summary"])):
        raise ValueError("style card refinement cannot change sentence-library quote lines or their source locators")
    # Replace only affected rendered methods; inactive observations remain byte-identical.
    markdown_edits = []
    for before, after in zip(content["craft_method_payload"]["methods"], updated["craft_method_payload"]["methods"]):
        if before != after:
            after["reuse_qualification"] = None
            old, new = render_style_method(before), render_style_method(after)
            if old != new:
                markdown_edits.append({"old_text": old, "new_text": new})
    if markdown_edits:
        updated["markdown"] = _apply_text_edits(content["markdown"], markdown_edits)
    if updated != content:
        updated["craft_method_payload"]["profile"]["validation_status"] = "draft_unreviewed"
    DnaFingerprint.model_validate(updated["author_style_fingerprint"])
    StoredWritingStyleMethodPayload.model_validate(updated["craft_method_payload"])
    return updated
