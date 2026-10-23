"""Lossless, request-local material sharing for semantic query assessment."""

from __future__ import annotations

from copy import deepcopy
import json
from typing import Any


def project_assessment_materials(payload: dict[str, Any]) -> dict[str, Any]:
    """Share identical contents while retaining every ordered candidate binding.

    Domain evidence and candidate IDs stay unchanged. Evidence references use
    request-local model IDs backed by complete original identities in the table.
    Candidate sharing requires exact JSON equality. Overlapping source text
    is shared only after exact overlap checks; evidence identities and citation
    permissions remain distinct. Equal passage text can share a string while
    each source retains its own segments and coordinates. The complete tables
    travel in this request, so its existing immutable model receipt can
    reconstruct all supplied rows.
    """
    projected = deepcopy(payload)
    occupied = set(_evidence_identifiers(payload))
    aliases = {}
    index = 1
    for reference in payload["evidence"]:
        while f"e{index}" in occupied:
            index += 1
        aliases[reference["id"]] = f"e{index}"
        index += 1
    projected = _map_evidence_fields(projected, aliases)
    for reference in projected["evidence"]:
        reference["model_id"] = aliases[reference["id"]]
    passages = _share_overlapping_quotes(projected["evidence"])
    if passages:
        projected["source_passages"] = passages
        _share_passage_texts(projected)
    quotes: dict[str, str] = {}
    for reference in projected["evidence"]:
        quote = reference.get("quote")
        if not isinstance(quote, str) or "quote_ref" in reference:
            continue
        if quote in quotes:
            reference["quote_ref"] = quotes[quote]
            del reference["quote"]
        else:
            quotes[quote] = reference["model_id"]
    records: dict[str, dict[str, Any]] = {}
    contexts: dict[str, dict[str, Any]] = {}
    provenance_contexts: dict[str, dict[str, Any]] = {}
    record_keys: dict[str, str] = {}
    context_keys: dict[str, str] = {}
    provenance_keys: dict[str, str] = {}

    for operation in projected["operation_results"]:
        context = operation.pop("source_context")
        operation["source_context_ref"] = _intern(context, contexts, context_keys, "scope")
        bindings = []
        for record in operation["records"]:
            candidate = record.pop("query_candidate_id")
            provenance = record.get("evidence_provenance")
            if isinstance(provenance, dict):
                for identifier, row in provenance.items():
                    if not isinstance(row, dict) or "context_ref" in row:
                        continue
                    local = {key: value for key, value in row.items()
                             if key in {"evidence_id", "source_location"}}
                    context = {key: value for key, value in row.items() if key not in local}
                    local["context_ref"] = _intern(
                        context, provenance_contexts, provenance_keys, "provenance",
                    )
                    provenance[identifier] = local
            content = record.get("content")
            if (isinstance(content, dict) and isinstance(record.get("semantic_review"), dict)
                    and json.dumps(content.get("semantic_review"), sort_keys=True, allow_nan=False)
                    == json.dumps(record["semantic_review"], sort_keys=True, allow_nan=False)
                    and "semantic_review_ref" not in content):
                del content["semantic_review"]
                content["semantic_review_ref"] = "record.semantic_review"
            bindings.append({
                "query_candidate_id": candidate,
                "record_ref": _intern(record, records, record_keys, "record"),
            })
        operation["records"] = bindings

    projected["candidate_records"] = records
    projected["operation_source_contexts"] = contexts
    if provenance_contexts:
        projected["evidence_provenance_contexts"] = provenance_contexts
    _share_evidence_sources(projected)
    return projected


def expand_assessment_materials(payload: dict[str, Any]) -> dict[str, Any]:
    """Reconstruct the exact prior semantic payload for receipt/audit checks.

    This is an audit reader, never a selectable model-input or execution path.
    It does not infer missing definitions or normalize malformed references.
    """
    expanded = deepcopy(payload)
    records = expanded.pop("candidate_records")
    contexts = expanded.pop("operation_source_contexts")
    provenance_contexts = expanded.pop("evidence_provenance_contexts", {})
    for operation in expanded["operation_results"]:
        operation["source_context"] = deepcopy(contexts[operation.pop("source_context_ref")])
        rows = []
        for binding in operation["records"]:
            if set(binding) != {"query_candidate_id", "record_ref"}:
                raise ValueError("candidate binding contains unexpected fields")
            record = deepcopy(records[binding["record_ref"]])
            provenance = record.get("evidence_provenance")
            if isinstance(provenance, dict):
                for identifier, row in provenance.items():
                    if isinstance(row, dict) and "context_ref" in row and provenance_contexts:
                        local = dict(row)
                        context = deepcopy(provenance_contexts[local.pop("context_ref")])
                        provenance[identifier] = {**context, **local}
            content = record.get("content")
            if isinstance(content, dict) and content.get("semantic_review_ref") == "record.semantic_review":
                content["semantic_review"] = deepcopy(record["semantic_review"])
                del content["semantic_review_ref"]
            rows.append({
                **record,
                "query_candidate_id": binding["query_candidate_id"],
            })
        operation["records"] = rows
    passages = expanded.pop("source_passages", {})
    passage_texts = expanded.pop("source_passage_texts", {})
    for passage in passages.values():
        if "text_ref" not in passage:
            continue  # Historical receipts keep text inline in each segment.
        text = passage_texts[passage.pop("text_ref")]
        segments = passage["segments"]
        if (not isinstance(text, str) or not segments
                or any("text" in row or type(row.get("start_offset")) is not int
                       or type(row.get("end_offset")) is not int
                       or row["start_offset"] < 0 or row["end_offset"] <= row["start_offset"]
                       for row in segments)
                or segments[-1]["end_offset"] - segments[0]["start_offset"] != len(text)
                or any(left["end_offset"] != right["start_offset"]
                       for left, right in zip(segments, segments[1:]))):
            raise ValueError("invalid shared source passage text")
        start = segments[0]["start_offset"]
        for row in segments:
            row["text"] = text[row["start_offset"] - start:row["end_offset"] - start]
    sources = expanded.pop("evidence_sources", {})
    for reference in [*expanded["evidence"], *passages.values()]:
        if "source_ref" in reference:
            source = sources[reference.pop("source_ref")]
            if not source or set(source) - set(_SOURCE_FIELDS) or set(source) & set(reference):
                raise ValueError("invalid shared evidence source identity")
            reference.update(deepcopy(source))
    for reference in expanded["evidence"]:
        if "passage_ref" in reference:
            passage = passages[reference.pop("passage_ref")]
            segments = [row for row in passage["segments"] if reference["model_id"] in row["evidence_ids"]]
            if (not segments or any(reference.get(key) != passage.get(key) for key in _SOURCE_FIELDS)
                    or segments[0]["start_offset"] != reference["start_offset"]
                    or segments[-1]["end_offset"] != reference["end_offset"]
                    or any(row["end_offset"] - row["start_offset"] != len(row["text"]) for row in segments)
                    or any(left["end_offset"] != right["start_offset"] for left, right in zip(segments, segments[1:]))):
                raise ValueError("shared source passage does not reconstruct the exact evidence interval")
            reference["quote"] = "".join(row["text"] for row in segments)
    quotes = {reference["model_id"]: reference["quote"] for reference in expanded["evidence"]
              if "quote" in reference}
    for reference in expanded["evidence"]:
        if "quote_ref" in reference and "quote" not in reference:
            reference["quote"] = quotes[reference.pop("quote_ref")]
    aliases = {reference["model_id"]: reference["id"] for reference in expanded["evidence"]}
    for reference in expanded["evidence"]:
        del reference["model_id"]
    return _map_evidence_fields(expanded, aliases)


_SOURCE_FIELDS = ("space", "work_id", "branch_id", "source_id", "snapshot_id", "content_hash")


def _share_passage_texts(payload: dict[str, Any]) -> None:
    """Share exact continuous text without sharing any source or segment binding."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for passage in payload["source_passages"].values():
        text = "".join(row["text"] for row in passage["segments"])
        groups.setdefault(text, []).append(passage)
    texts: dict[str, str] = {}
    def compact(value):
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))

    for text, passages in groups.items():
        if len(passages) < 2:
            continue
        identifier = f"text-{len(texts) + 1}"
        replacements = [
            {**passage, "text_ref": identifier,
             "segments": [{key: value for key, value in row.items() if key != "text"}
                          for row in passage["segments"]]}
            for passage in passages
        ]
        if compact([replacements, {"source_passage_texts": {identifier: text}}]) >= compact(passages):
            continue  # Keep the simpler inline representation when sharing adds overhead.
        texts[identifier] = text
        for passage, replacement in zip(passages, replacements):
            passage.clear()
            passage.update(replacement)
    if texts:
        payload["source_passage_texts"] = texts


def _share_evidence_sources(payload: dict[str, Any]) -> None:
    """Intern exact source identities only when the complete representation shrinks."""
    references = [*payload["evidence"], *payload.get("source_passages", {}).values()]
    sources, keys, replacements = {}, {}, []
    for reference in references:
        source = {key: reference[key] for key in _SOURCE_FIELDS if key in reference}
        if not source or "source_ref" in reference:
            return  # Keep unusual or already projected input unchanged.
        identifier = _intern(source, sources, keys, "source")
        replacements.append({**{key: value for key, value in reference.items() if key not in source},
                             "source_ref": identifier})
    if not sources:
        return
    def compact(value):
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))

    if compact([replacements, {"evidence_sources": sources}]) >= compact(references):
        return
    payload["evidence_sources"] = sources
    for reference, replacement in zip(references, replacements):
        reference.clear()
        reference.update(replacement)


def _share_overlapping_quotes(references: list[dict[str, Any]]) -> dict[str, Any]:
    """Show each source character once, with its existing citation IDs beside it."""
    groups: dict[tuple, list] = {}
    for row in references:
        start, end, quote = row.get("start_offset"), row.get("end_offset"), row.get("quote")
        if (not row.get("source_id") or not row.get("snapshot_id")
                or type(start) is not int or type(end) is not int or start < 0
                or not isinstance(quote, str) or end <= start or len(quote) != end - start
                or "quote_ref" in row or "passage_ref" in row):
            continue
        groups.setdefault(tuple(row.get(key) for key in _SOURCE_FIELDS), []).append(row)
    passages = {}
    for rows in groups.values():
        components: list[list] = []
        right = -1
        for row in sorted(rows, key=lambda row: (row["start_offset"], row["end_offset"])):
            if row["start_offset"] >= right:
                components.append([])
            components[-1].append(row)
            right = max(right, row["end_offset"])
        for component in components:
            if len({(row["start_offset"], row["end_offset"]) for row in component}) < 2:
                continue  # Exact duplicates use the existing quote_ref representation.
            start = component[0]["start_offset"]
            text = component[0]["quote"]
            consistent = True
            for row in component[1:]:
                offset = row["start_offset"] - start
                overlap = min(len(text) - offset, len(row["quote"]))
                if text[offset:offset + overlap] != row["quote"][:overlap]:
                    consistent = False
                    break
                text += row["quote"][overlap:]
            if not consistent:
                continue  # Never repair conflicting source text by choosing one copy.
            boundaries = sorted({row[key] for row in component for key in ("start_offset", "end_offset")})
            segments = [{"start_offset": left, "end_offset": right,
                         "evidence_ids": [row["model_id"] for row in component
                                          if row["start_offset"] <= left and row["end_offset"] >= right],
                         "text": text[left - start:right - start]}
                        for left, right in zip(boundaries, boundaries[1:])]
            passage = {**{key: component[0][key] for key in _SOURCE_FIELDS if key in component[0]},
                       "segments": segments}
            identifier = f"passage-{len(passages) + 1}"
            projected = [{**{key: value for key, value in row.items() if key != "quote"},
                          "passage_ref": identifier} for row in component]
            if len(json.dumps([projected, passage], ensure_ascii=False)) >= len(json.dumps(component, ensure_ascii=False)):
                continue
            passages[identifier] = passage
            for row in component:
                del row["quote"]
                row["passage_ref"] = identifier
    return passages


def restore_assessment_row_evidence(row: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Restore a model row before the existing per-requirement authorization check.

    Unknown aliases fail only their row. Long domain IDs are audit identities,
    not an alternate accepted output syntax. Candidate ownership and each
    candidate's allowed evidence remain checked by the existing domain validator.
    """
    aliases = {reference["model_id"]: reference["id"] for reference in payload["evidence"]}
    restored = deepcopy(row)

    def restore_ids(value):
        if not isinstance(value, list):
            return value  # The domain schema reports malformed field types.
        if any(not isinstance(identifier, str) or identifier not in aliases for identifier in value):
            raise ValueError("assessment cited an unknown request-local evidence ID")
        return [aliases[identifier] for identifier in value]

    if "evidence_ids" in restored:
        restored["evidence_ids"] = restore_ids(restored["evidence_ids"])
    if isinstance(restored.get("candidate_decisions"), list):
        for decision in restored["candidate_decisions"]:
            if isinstance(decision, dict) and "evidence_ids" in decision:
                decision["evidence_ids"] = restore_ids(decision["evidence_ids"])
    return restored


def _map_evidence_fields(value, aliases):
    if isinstance(value, list):
        return [_map_evidence_fields(child, aliases) for child in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, child in value.items():
        if (key == "evidence_ids" or key.endswith("_evidence_ids")) and isinstance(child, list):
            result[key] = [aliases.get(identifier, identifier) if isinstance(identifier, str)
                           else identifier for identifier in child]
        elif (key == "evidence_id" or key.endswith("_evidence_id")) and isinstance(child, str):
            result[key] = aliases.get(child, child)
        elif key == "evidence_provenance" and isinstance(child, dict):
            result[key] = {aliases.get(identifier, identifier): _map_evidence_fields(row, aliases)
                           for identifier, row in child.items()}
        else:
            result[key] = _map_evidence_fields(child, aliases)
    return result


def _evidence_identifiers(value):
    if isinstance(value, list):
        for child in value:
            yield from _evidence_identifiers(child)
    elif isinstance(value, dict):
        for key, child in value.items():
            if (key == "evidence_ids" or key.endswith("_evidence_ids")) and isinstance(child, list):
                yield from (identifier for identifier in child if isinstance(identifier, str))
            elif (key == "evidence_id" or key.endswith("_evidence_id")) and isinstance(child, str):
                yield child
            elif key == "evidence_provenance" and isinstance(child, dict):
                yield from child
                yield from _evidence_identifiers(child)
            else:
                yield from _evidence_identifiers(child)


def _intern(value, definitions, keys, prefix):
    key = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                     separators=(",", ":"))
    identifier = keys.get(key)
    if identifier is None:
        identifier = f"{prefix}-{len(definitions) + 1}"
        keys[key] = identifier
        definitions[identifier] = value
    return identifier
