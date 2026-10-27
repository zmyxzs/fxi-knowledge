"""SQLite-backed knowledge records and rebuildable lexical search projection."""

from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
import base64
import binascii
import hashlib
import json
import sqlite3
import unicodedata
from typing import Any, Iterable, Iterator
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from fxi_novel.knowledge.chunking import (
    CHUNKER_VERSION,
    OFFSET_UNIT,
    TextChunk,
    split_text,
)
from fxi_novel.knowledge.chapter_sections import (
    SECTIONING_VERSION,
    segment_source_text,
)
from fxi_novel.knowledge.models import (
    EvidenceReference,
    KNOWLEDGE_GRAPH_IDENTITY_METADATA_FIELDS,
    KNOWLEDGE_PARSE_DOWNSTREAM_STAGES,
    KnowledgeCardMatch,
    KnowledgeGraphCandidateContent,
    KnowledgeParseBatchConflictError,
    KnowledgeParseStageContent,
    knowledge_graph_identity_source_content_hash,
    SearchResult,
    SourceReadingPosition,
    SnapshotReference,
    SourceImportResult,
    SourceReference,
    TemporalRecordContent,
    normalize_knowledge_graph_entity_kind,
    validate_scope,
    validate_temporal_branch_id,
    validate_knowledge_parse_client_batch_id,
    validate_knowledge_parse_batch_request_hash,
)
from fxi_novel.knowledge.tokenizer import (
    fts_document,
    fts_documents,
    fts_phrase_query,
    fts_query,
    fts_query_requiring_phrases,
)
from fxi_novel.knowledge.embedding_repository import EmbeddingProjectionRepository
from fxi_novel.knowledge.scope_selection import (
    current_prose_projection_filter,
    source_selection_filter,
)
from fxi_novel.storage.database import Database


class KnowledgeItemVersionConflictError(RuntimeError):
    """Raised when an update's expected knowledge-item version is stale."""


class KnowledgeItemReferencedError(RuntimeError):
    """Raised when hard deletion would leave persisted knowledge references dangling."""

    def __init__(self, *, reference_count: int, references: list[dict[str, Any]]) -> None:
        self.reference_count = reference_count
        self.references = references
        super().__init__("知识项仍被其他作品内容引用，未删除。请先处理这些引用后重试。")


class KnowledgeSourceReferencedError(RuntimeError):
    """Raised when deleting a source would orphan surviving author knowledge."""

    def __init__(
        self, *, source_ids: list[str], reference_count: int,
        references: list[dict[str, Any]],
    ) -> None:
        self.source_ids = source_ids
        self.reference_count = reference_count
        self.references = references
        super().__init__("资料生成的知识仍被其他作品内容引用，资料及其历史未删除。请先处理这些引用。")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_PURGE_DELETE_GUARDS = {
    "knowledge_version_no_delete": "knowledge_version",
    "knowledge_query_claim_publication_no_delete": "knowledge_query_reviewed_claim_publication",
    "source_section_no_delete": "source_section",
    "source_snapshot_no_delete": "source_snapshot",
    "knowledge_source_ownership_transfer_item_no_delete": "knowledge_source_ownership_transfer_item",
    "knowledge_source_ownership_transfer_no_delete": "knowledge_source_ownership_transfer",
    "prose_detector_comparison_no_delete": "prose_detector_comparison",
}


@contextmanager
def _allow_authorized_purge(
    connection: sqlite3.Connection, *, parser_results: bool = False,
) -> Iterator[None]:
    """Suspend only the append-only guards during one already-scoped purge transaction."""
    saved: list[str] = []
    guards = dict(_PURGE_DELETE_GUARDS)
    if parser_results:
        guards.update({
            "knowledge_parse_stage_attempt_no_delete": "knowledge_parse_stage_attempt",
            "knowledge_query_review_receipt_no_delete": "knowledge_query_review_receipt",
        })
    for name, table in guards.items():
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
            (name,),
        ).fetchone()
        if row is None:
            continue
        sql = str(row["sql"] or "")
        normalized = " ".join(sql.casefold().split())
        if (
            f"before delete on {table}" not in normalized
            or "raise(abort" not in normalized
        ):
            raise ValueError(f"refusing to suspend unexpected deletion guard {name}")
        saved.append(sql)
        connection.execute(f'DROP TRIGGER "{name}"')
    try:
        yield
    finally:
        for sql in saved:
            connection.execute(sql)


def _json_mentions(value: Any, identifiers: set[str]) -> bool:
    if isinstance(value, str):
        return value in identifiers
    if isinstance(value, dict):
        return any(_json_mentions(child, identifiers) for child in value.values())
    if isinstance(value, list):
        return any(_json_mentions(child, identifiers) for child in value)
    return False


def _json_has_work_scope(
    value: Any, work_id: str, space: str | None, inherited_space: str | None = None
) -> bool:
    if isinstance(value, dict):
        current_space = value.get("space", inherited_space)
        if any(
            key in {"work_id", "workId", "project_id", "projectId"}
            and child == work_id
            for key, child in value.items()
        ) and (space is None or current_space is None or current_space == space):
            return True
        return any(
            _json_has_work_scope(child, work_id, space, current_space)
            for child in value.values()
        )
    if isinstance(value, list):
        return any(
            _json_has_work_scope(child, work_id, space, inherited_space)
            for child in value
        )
    return False


def _workflow_run_ids_to_purge(
    connection: sqlite3.Connection,
    *,
    source_identifiers: set[str] | None = None,
    work_id: str | None = None,
    space: str | None = None,
) -> list[sqlite3.Row]:
    selected: dict[str, sqlite3.Row] = {}
    for row in connection.execute(
        "SELECT run_id, status, spec_json, inputs_json, outputs_json, error FROM workflow_run"
    ):
        try:
            inputs = json.loads(str(row["inputs_json"]))
            outputs = json.loads(str(row["outputs_json"])) if row["outputs_json"] else None
        except (TypeError, json.JSONDecodeError):
            inputs = outputs = None
        matched = bool(work_id and _json_has_work_scope(inputs, work_id, space))
        matched = matched or bool(
            source_identifiers
            and (_json_mentions(inputs, source_identifiers) or _json_mentions(outputs, source_identifiers))
        )
        if source_identifiers and not matched:
            encoded_identifiers = tuple(
                json.dumps(identifier, ensure_ascii=False) for identifier in source_identifiers
            )
            matched = any(
                identifier in str(row[field] or "")
                for field in ("spec_json", "inputs_json", "outputs_json", "error")
                for identifier in encoded_identifiers
            ) or any(
                identifier in str(row["error"] or "") for identifier in source_identifiers
            )
        if work_id and not matched:
            encoded_work_id = json.dumps(work_id, ensure_ascii=False)
            matched = any(
                encoded_work_id in str(row[field] or "")
                for field in ("spec_json", "inputs_json", "outputs_json")
            ) or work_id in str(row["error"] or "")
        if matched:
            selected[str(row["run_id"])] = row

    if source_identifiers:
        for row in connection.execute(
            "SELECT run_id, input_data_json, output_data_json, error FROM workflow_node_event"
        ):
            if str(row["run_id"]) in selected:
                continue
            matched = False
            for field in ("input_data_json", "output_data_json"):
                raw = row[field]
                if raw:
                    try:
                        matched = _json_mentions(json.loads(str(raw)), source_identifiers)
                    except (TypeError, json.JSONDecodeError):
                        matched = any(
                            json.dumps(identifier, ensure_ascii=False) in str(raw)
                            for identifier in source_identifiers
                        )
                if matched:
                    break
            if matched or (row["error"] and str(row["error"]) in source_identifiers):
                run = connection.execute(
                    "SELECT run_id, status, inputs_json, outputs_json FROM workflow_run WHERE run_id = ?",
                    (row["run_id"],),
                ).fetchone()
                if run is not None:
                    selected[str(run["run_id"])] = run
    return list(selected.values())


def _source_in_folder_scope(
    connection: sqlite3.Connection,
    source: sqlite3.Row,
    folder_ids: Iterable[str],
    *,
    space: str,
    work_id: str | None,
) -> bool:
    selected = sorted(set(str(folder_id) for folder_id in folder_ids))
    if not selected or source["folder_id"] is None:
        return False
    placeholders = ", ".join("?" for _ in selected)
    rows = connection.execute(
        f"SELECT path FROM knowledge_folder WHERE id IN ({placeholders}) AND space = ? AND work_id IS ?",
        [*selected, space, work_id],
    ).fetchall()
    source_folder = connection.execute(
        "SELECT path FROM knowledge_folder WHERE id = ? AND space = ? AND work_id IS ?",
        (source["folder_id"], source["space"], source["work_id"]),
    ).fetchone()
    if source_folder is None:
        return False
    source_path = str(source_folder["path"]).casefold()
    return any(
        source_path == str(row["path"]).casefold()
        or source_path.startswith(str(row["path"]).casefold().rstrip("/") + "/")
        for row in rows
    )


def _active_assistant_run_uses_source(
    connection: sqlite3.Connection, source: sqlite3.Row
) -> bool:
    runs = connection.execute(
        """
        SELECT run.scope_json, conversation.space, conversation.work_id
        FROM assistant_run AS run
        JOIN assistant_conversation AS conversation
          ON conversation.id = run.conversation_id
        WHERE run.status IN ('accepted', 'running')
        """
    ).fetchall()
    for run in runs:
        try:
            scope = json.loads(str(run["scope_json"]))
        except (TypeError, json.JSONDecodeError):
            if run["space"] == source["space"] and run["work_id"] == source["work_id"]:
                return True
            continue
        if not isinstance(scope, dict):
            if run["space"] == source["space"] and run["work_id"] == source["work_id"]:
                return True
            continue
        scope_space = str(scope.get("space") or run["space"])
        scope_work_id = scope.get("work_id", run["work_id"])
        direct_scope = (
            scope_space == source["space"]
            and scope_work_id == source["work_id"]
            and scope_space == run["space"]
            and scope_work_id == run["work_id"]
        )
        if direct_scope:
            source_ids = scope.get("source_ids")
            folder_ids = scope.get("folder_ids")
            if isinstance(source_ids, list) and str(source["id"]) in source_ids:
                return True
            if isinstance(folder_ids, list) and folder_ids:
                if _source_in_folder_scope(
                    connection,
                    source,
                    folder_ids,
                    space=scope_space,
                    work_id=scope_work_id,
                ):
                    return True
            elif not isinstance(source_ids, list) or not source_ids:
                return True

        if scope_space != "writing" or scope_work_id != run["work_id"] or not scope.get("include_linked_scopes"):
            continue
        consumer_work_id = str(run["work_id"] or "")
        if source["space"] == "learning":
            if connection.execute(
                "SELECT 1 FROM writing_learning_source_reference WHERE work_id = ? AND source_id = ? LIMIT 1",
                (consumer_work_id, source["id"]),
            ).fetchone() is not None:
                return True
            scope_rows = connection.execute(
                """
                SELECT folder_id FROM writing_learning_scope_reference
                WHERE work_id = ? AND learning_work_id IS ?
                """,
                (consumer_work_id, source["work_id"]),
            ).fetchall()
            if any(row["folder_id"] is None for row in scope_rows):
                return True
            if scope_rows and _source_in_folder_scope(
                connection,
                source,
                (str(row["folder_id"]) for row in scope_rows if row["folder_id"] is not None),
                space="learning",
                work_id=source["work_id"],
            ):
                return True
            if connection.execute(
                """SELECT 1 FROM writing_base_source_reference
                   WHERE work_id = ? AND source_id = ? LIMIT 1""",
                (consumer_work_id, source["id"]),
            ).fetchone() is not None:
                return True
            base_rows = connection.execute(
                """
                SELECT base.base_work_id
                FROM writing_base_work_reference AS base
                WHERE base.work_id = ? AND base.base_space = 'learning'
                  AND (
                      base.base_work_id IS ?
                      OR EXISTS (
                          SELECT 1
                          FROM knowledge_learning_work_folder AS assigned
                          JOIN knowledge_folder AS root ON root.id = assigned.folder_id
                          JOIN knowledge_folder AS source_folder
                            ON source_folder.id = ?
                           AND source_folder.space = 'learning'
                           AND source_folder.work_id IS ?
                          WHERE assigned.work_id = base.base_work_id
                            AND root.work_id IS source_folder.work_id
                            AND (source_folder.path = root.path COLLATE NOCASE
                              OR substr(source_folder.path, 1, length(root.path) + 1)
                                 = root.path || '/' COLLATE NOCASE)
                      )
                  )
                """,
                (
                    consumer_work_id, source["work_id"],
                    source["folder_id"], source["work_id"],
                ),
            ).fetchall()
            for base_row in base_rows:
                base_scopes = connection.execute(
                    "SELECT folder_id FROM writing_base_scope_reference WHERE work_id = ?",
                    (consumer_work_id,),
                ).fetchall()
                if any(scope["folder_id"] is None for scope in base_scopes):
                    return True
                if base_scopes and _source_in_folder_scope(
                    connection,
                    source,
                    (str(scope["folder_id"]) for scope in base_scopes),
                    space="learning",
                    work_id=source["work_id"],
                ):
                    return True
        elif source["space"] == "writing":
            if connection.execute(
                "SELECT 1 FROM writing_base_source_reference WHERE work_id = ? AND source_id = ? LIMIT 1",
                (consumer_work_id, source["id"]),
            ).fetchone() is not None:
                return True
            base = connection.execute(
                """SELECT base_work_id FROM writing_base_work_reference
                   WHERE work_id = ? AND base_space = 'writing' AND base_work_id = ?""",
                (consumer_work_id, source["work_id"]),
            ).fetchone()
            if base is not None:
                base_folders = connection.execute(
                    "SELECT folder_id FROM writing_base_scope_reference WHERE work_id = ?",
                    (consumer_work_id,),
                ).fetchall()
                if not base_folders or any(row["folder_id"] is None for row in base_folders):
                    return True
                if _source_in_folder_scope(
                    connection,
                    source,
                    (str(row["folder_id"]) for row in base_folders if row["folder_id"] is not None),
                    space="writing",
                    work_id=source["work_id"],
                ):
                    return True
    return False


def _delete_by_values(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    values: Iterable[str],
) -> None:
    for selected in _sql_value_batches(values):
        placeholders = ", ".join("?" for _ in selected)
        connection.execute(
            f"DELETE FROM {table} WHERE {column} IN ({placeholders})",
            selected,
        )


def _sql_value_batches(
    values: Iterable[str], *, batch_size: int = 400
) -> Iterator[list[str]]:
    """Yield bounded, deterministic value batches safe for multi-list SQL clauses."""
    if isinstance(batch_size, bool) or not 1 <= batch_size <= 400:
        raise ValueError("SQL value batches must contain between 1 and 400 values")
    selected = sorted(set(values))
    for start in range(0, len(selected), batch_size):
        yield selected[start:start + batch_size]


def _source_identity_key(
    space: str, work_id: str | None, title: str, source_uri: str | None
) -> str:
    normalized_title = title.strip()
    normalized_uri = source_uri.strip() if source_uri and source_uri.strip() else None
    return json.dumps(
        [
            "fxi-novel-source-v1",
            space,
            work_id,
            "uri" if normalized_uri is not None else "title",
            normalized_uri if normalized_uri is not None else normalized_title,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _source_import_result_json(result: SourceImportResult) -> str:
    return json.dumps(
        {
            "source_id": result.source_id,
            "snapshot_id": result.snapshot_id,
            "content_hash": result.content_hash,
            "chunk_ids": list(result.chunk_ids),
            "evidence_span_ids": list(result.evidence_span_ids),
            "chunker_version": result.chunker_version,
            "offset_unit": result.offset_unit,
            "section_count": result.section_count,
            "chapter_count": result.chapter_count,
            "sectioning_mode": result.sectioning_mode,
            "edit_revision": result.edit_revision,
        },
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


def _source_import_result_from_json(value: str) -> SourceImportResult:
    try:
        result = json.loads(value)
        if not isinstance(result, dict):
            raise ValueError
        return SourceImportResult(
            source_id=str(result["source_id"]),
            snapshot_id=str(result["snapshot_id"]),
            content_hash=str(result["content_hash"]),
            chunk_ids=tuple(str(item) for item in result["chunk_ids"]),
            evidence_span_ids=tuple(str(item) for item in result["evidence_span_ids"]),
            chunker_version=str(result["chunker_version"]),
            offset_unit=str(result["offset_unit"]),
            section_count=int(result["section_count"]),
            chapter_count=int(result["chapter_count"]),
            sectioning_mode=str(result["sectioning_mode"]),
            edit_revision=int(result["edit_revision"]) if result.get("edit_revision") is not None else None,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("stored source idempotency receipt is invalid") from exc


def _source_folder_path_transferable(path: str) -> bool:
    normalized_path = path.replace("\\", "/")
    if (
        normalized_path != path
        or normalized_path.startswith("/")
        or (len(normalized_path) >= 2 and normalized_path[1] == ":")
        or len(normalized_path) > 2000
    ):
        return False
    segments = normalized_path.split("/")
    return all(
        segment
        and segment == segment.strip()
        and segment not in {".", ".."}
        and len(segment) <= 200
        and "\x00" not in segment
        for segment in segments
    )


class ProseLifecycleEventSupersededError(ValueError):
    """A later adopted head won before this event could update the projection."""


def _encode_projection_cursor(
    key: tuple[int, int, int, str], scope_hash: str
) -> str:
    payload = json.dumps(
        {"key": key, "scope": scope_hash}, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_projection_cursor(value: str) -> tuple[tuple[int, int, int, str], str]:
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        key = decoded.get("key") if isinstance(decoded, dict) else None
        scope_hash = decoded.get("scope") if isinstance(decoded, dict) else None
        if (not isinstance(key, list) or len(key) != 4
                or any(isinstance(item, bool) or not isinstance(item, int) for item in key[:3])
                or not isinstance(key[3], str) or not key[3]
                or not isinstance(scope_hash, str) or len(scope_hash) != 64):
            raise ValueError
        return ((key[0], key[1], key[2], key[3]), scope_hash)
    except (ValueError, TypeError, UnicodeError, binascii.Error, json.JSONDecodeError) as exc:
        raise ValueError("projection cursor is invalid") from exc


def _dump_json(value: Any, field: str) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be JSON serializable") from exc


_GRAPH_IDENTITY_METADATA_KEYS = KNOWLEDGE_GRAPH_IDENTITY_METADATA_FIELDS
_GRAPH_IDENTITY_JSON_PATHS = ", ".join(
    f"'$.{key}'" for key in _GRAPH_IDENTITY_METADATA_KEYS
)

_KNOWLEDGE_ITEM_CARD_INDEX_FIELDS = frozenset({
    "title", "name", "heading", "subject", "summary", "description", "abstract",
    "candidate_role", "category", "entity_kind", "entity_name", "entity_ref",
    "identity_display_name", "identity_group_id", "identity_aliases",
    "identity_is_canonical", "identity_resolution", "source_chapter_order",
    "predicate", "target_entity_ref", "target_entity_name",
    "target_identity_group_id", "target_identity_display_name", "branch_id",
    "state_slot", "state_value_status", "value", "story_time_expression",
    "claim_basis", "asserted_by", "asserted_by_entity_ref",
    "asserted_by_identity_group_id", "observed_by", "observed_by_entity_ref",
    "observed_by_identity_group_id", "holder", "holder_entity_ref",
    "holder_identity_group_id", "audience", "audience_entity_refs",
    "audience_identity_group_ids", "evidence_quote_start", "record_type",
    "subject_id", "instance_type", "name_kind", "proposed_assessment",
    "aspect", "guidance_kind", "guidance", "branch_id",
})

_KNOWLEDGE_ITEM_CARD_READING_FIELDS = {
    "query_learning_claim": frozenset({"conditions", "speaker", "claim_basis"}),
    "typed_rule": frozenset({
        "name", "conditions", "effect", "limitations", "costs", "exceptions", "scope",
        "claim_basis", "branch_id",
    }),
    "typed_event": frozenset({
        "kind", "disposition", "asset_granularity", "asset_nature", "action_expression",
        "story_time_expression", "claim_basis", "branch_id", "conditions",
    }),
    "typed_quantity": frozenset({"attribute", "time_expression", "claim_basis", "branch_id"}),
    "typed_instance": frozenset({"distinguishing_properties", "claim_basis", "branch_id"}),
}
_KNOWLEDGE_ITEM_CARD_QUANTITY_FIELDS = frozenset({
    "kind", "unit", "amount", "minimum", "maximum", "tolerance",
})


def _knowledge_item_card_reading_content(content: dict[str, Any]) -> dict[str, Any]:
    """Keep saved reading context without exposing interpretation audit payloads.

    Both card-index and entity-relation rows use this read-only projection of
    the already publication-filtered version. It does not calculate state or
    upgrade the claim's interpretation/publication eligibility.
    """
    result = {key: value for key, value in content.items() if key in _KNOWLEDGE_ITEM_CARD_INDEX_FIELDS}
    for payload_key, fields in _KNOWLEDGE_ITEM_CARD_READING_FIELDS.items():
        payload = content.get(payload_key)
        if not isinstance(payload, dict):
            continue
        readable = {key: value for key, value in payload.items() if key in fields}
        quantity = payload.get("quantity")
        if payload_key in {"typed_event", "typed_quantity"} and isinstance(quantity, dict):
            readable["quantity"] = {
                key: value for key, value in quantity.items()
                if key in _KNOWLEDGE_ITEM_CARD_QUANTITY_FIELDS
            }
        if payload_key == "typed_event":
            participants = payload.get("participants")
            if isinstance(participants, list):
                readable["participants"] = [
                    {key: value for key, value in participant.items() if key in {"role", "mention", "status"}}
                    for participant in participants if isinstance(participant, dict)
                ]
            measurements = payload.get("measurements")
            if isinstance(measurements, list):
                readable["measurements"] = [
                    _knowledge_item_card_reading_content({"typed_quantity": measurement}).get("typed_quantity", {})
                    for measurement in measurements if isinstance(measurement, dict)
                ]
        if readable:
            result[payload_key] = readable
    return result


def _knowledge_item_card_semantic_context(content: dict[str, Any]) -> dict[str, Any]:
    """Separate saved meanings, independent of audit/evidence payload identity."""
    reading = _knowledge_item_card_reading_content(content)
    context = {key: reading[key] for key in _KNOWLEDGE_ITEM_CARD_READING_FIELDS if key in reading}
    event = content.get("typed_event")
    if isinstance(event, dict):
        # These are domain ownership references, not the event's audit ID.
        ownership = {key: event[key] for key in (
            "holder_before_id", "holder_after_id", "owner_before_id", "owner_after_id",
        ) if event.get(key) is not None}
        if ownership:
            context.setdefault("typed_event", {}).update(ownership)

    def clean(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: cleaned for key, nested in sorted(value.items())
                    if (cleaned := clean(nested)) not in (None, "", [], {})}
        if isinstance(value, list):
            return [cleaned for nested in value if (cleaned := clean(nested)) not in (None, "", [], {})]
        return value

    return clean(context)


_KNOWLEDGE_ITEM_CARD_SEARCH_FIELDS = (
    "title", "name", "heading", "subject", "canonical_name",
    "display_name", "relation_type", "aspect", "entity_name",
    "identity_display_name", "identity_aliases", "summary",
    "description", "abstract", "predicate", "target_identity_display_name",
    "target_entity_name", "state_slot", "value", "story_time_expression",
    "claim_basis", "asserted_by", "observed_by", "holder", "audience",
    "record_type", "name_kind", "guidance", "guidance_kind",
    "subject_id", "instance_type", "branch_id", "proposed_assessment",
    "identity_name_roles", "category", "candidate_role",
)

_KNOWLEDGE_ITEM_CARD_CATEGORY_IDS = (
    "character", "prop", "ability", "location", "organization", "event",
    "facts", "timeline", "claims", "other",
)
_KNOWLEDGE_ITEM_CARD_CATEGORY_BY_KIND = {
    "character": "character",
    "characters": "character",
    "character_card": "character",
    "role_card": "character",
    "person": "character",
    "role": "character",
    "story_identity": "character",
    "story_instance": "character",
    "story_name_binding": "character",
    "story_identity_relation": "facts",
    "story_voice_profile": "character",
    "temporal_state": "timeline",
    "ability": "ability",
    "country": "organization",
    "faction": "organization",
    "attribute": "facts",
    "relationship": "facts",
    "claim": "claims",
    "organization": "organization",
    "rule": "other",
    "concept": "other",
    "人物": "character",
    "角色": "character",
    "prop": "prop",
    "prop_card": "prop",
    "item_card": "prop",
    "object": "prop",
    "item": "prop",
    "道具": "prop",
    "物品": "prop",
    "location": "location",
    "location_card": "location",
    "place": "location",
    "scene": "location",
    "地点": "location",
    "场景": "location",
    "event": "event",
    "event_card": "event",
    "timeline_event": "event",
    "temporal_record": "timeline",
    "事件": "event",
}


def _knowledge_item_card_category_sql(kind_expression: str, content_expression: str) -> str:
    def mapped_case(expression: str) -> str:
        choices = " ".join(
            f"WHEN '{kind}' THEN '{category}'"
            for kind, category in _KNOWLEDGE_ITEM_CARD_CATEGORY_BY_KIND.items()
        )
        return f"CASE lower({expression}) {choices} ELSE 'other' END"

    content_category = (
        f"lower(trim(COALESCE(json_extract({content_expression}, '$.category'), '')))"
    )
    fact_category = f"""
        CASE
            WHEN {content_category} = 'event' THEN 'event'
            WHEN {content_category} = 'claim' THEN 'claims'
            WHEN {content_category} = 'temporal_state'
              OR (json_type({content_expression}, '$.story_time_expression') = 'text'
                  AND trim(json_extract({content_expression}, '$.story_time_expression')) != '')
                THEN 'timeline'
            WHEN (
                (json_type({content_expression}, '$.claim_basis') = 'text'
                 AND trim(json_extract({content_expression}, '$.claim_basis')) != ''
                 AND json_extract({content_expression}, '$.claim_basis') != 'narration')
                OR (json_type({content_expression}, '$.asserted_by') = 'text'
                    AND trim(json_extract({content_expression}, '$.asserted_by')) != '')
                OR (json_type({content_expression}, '$.observed_by') = 'text'
                    AND trim(json_extract({content_expression}, '$.observed_by')) != '')
                OR (json_type({content_expression}, '$.holder') = 'text'
                    AND trim(json_extract({content_expression}, '$.holder')) != '')
                OR (json_type({content_expression}, '$.audience') = 'array'
                    AND json_array_length({content_expression}, '$.audience') > 0)
            ) THEN 'claims'
            ELSE 'facts'
        END
    """
    return f"""
        CASE
            WHEN {kind_expression} = 'knowledge_graph_candidate'
             AND json_extract({content_expression}, '$.candidate_role') = 'fact'
                THEN {fact_category}
            WHEN {kind_expression} = 'knowledge_graph_candidate'
                THEN {mapped_case(content_category)}
            ELSE {mapped_case(kind_expression)}
        END
    """


def _knowledge_item_card_search_fields_sql(content_expression: str) -> str:
    values = ", ".join(
        "COALESCE(CAST(json_extract("
        + content_expression
        + ", '$."
        + field
        + "') AS TEXT) || '', '')"
        for field in _KNOWLEDGE_ITEM_CARD_SEARCH_FIELDS
    )
    return f"json_array({values})"

_KNOWLEDGE_CARD_SEARCH_ASCII_LOWER = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"
)


def _knowledge_item_card_search_bigrams(search_fields_json: str) -> list[str]:
    fields = json.loads(search_fields_json)
    if not isinstance(fields, list):
        return []
    grams: set[str] = set()
    for value in fields:
        if not isinstance(value, str):
            continue
        normalized = value.translate(_KNOWLEDGE_CARD_SEARCH_ASCII_LOWER)
        grams.update(
            normalized[index : index + 2]
            for index in range(len(normalized) - 1)
        )
    return sorted(grams)


def _knowledge_item_card_query_bigrams(query: str) -> list[str]:
    normalized = query.translate(_KNOWLEDGE_CARD_SEARCH_ASCII_LOWER)
    return sorted({
        normalized[index : index + 2]
        for index in range(len(normalized) - 1)
    })


def _knowledge_item_card_query_search_grams(query: str) -> list[str]:
    normalized = query.translate(_KNOWLEDGE_CARD_SEARCH_ASCII_LOWER)
    grams = set(normalized)
    grams.update(
        normalized[index : index + 2]
        for index in range(len(normalized) - 1)
    )
    return sorted(grams)

_KNOWLEDGE_GRAPH_FTS_VERSION = 2
_KNOWLEDGE_CARD_INDEX_VERSION = 5
_KNOWLEDGE_CARD_CANDIDATE_EVIDENCE_INDEX_VERSION = 1


def _normalized_graph_reference(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip().casefold()


def _graph_reference_key(identity_id: Any, local_ref: Any, name: Any) -> str:
    identity = _normalized_graph_reference(identity_id)
    if identity:
        return f"identity:{identity}"
    local = _normalized_graph_reference(local_ref)
    if local:
        return f"source-entity:{local}"
    return f"unlinked:{_normalized_graph_reference(name)}"


def _knowledge_graph_card_key(item_id: str, content: dict[str, Any]) -> str:
    role = str(content.get("candidate_role") or "")
    if role == "entity":
        identity_id = _normalized_graph_reference(content.get("identity_group_id"))
        if identity_id:
            entity_kind = normalize_knowledge_graph_entity_kind(
                str(content.get("entity_kind") or content.get("category") or "other")
            )
            return f"entity:{entity_kind}:{identity_id}"
        return f"entity:unresolved:{item_id}"
    if role != "fact":
        return f"item:{item_id}"

    audience = content.get("audience")
    audience_ids = content.get("audience_identity_group_ids")
    audience_refs = content.get("audience_entity_refs")
    if not isinstance(audience, list):
        audience = []
    if not isinstance(audience_ids, list):
        audience_ids = []
    if not isinstance(audience_refs, list):
        audience_refs = []
    audience_keys = [
        _graph_reference_key(
            audience_ids[index] if index < len(audience_ids) else None,
            audience_refs[index] if index < len(audience_refs) else None,
            name,
        )
        for index, name in enumerate(audience)
    ]
    key_fields = [
        _graph_reference_key(
            content.get("identity_group_id"), content.get("entity_ref"), content.get("entity_name")
        ),
        content.get("branch_id") or "main",
        content.get("category"),
        content.get("predicate"),
        _graph_reference_key(
            content.get("target_identity_group_id"),
            content.get("target_entity_ref"),
            content.get("target_entity_name"),
        ),
        content.get("state_slot"),
        content.get("state_value_status") or "known_value",
        content.get("value"),
        content.get("story_time_expression"),
        content.get("source_chapter_order") if content.get("category") == "temporal_state" else None,
        content.get("claim_basis"),
        _graph_reference_key(
            content.get("asserted_by_identity_group_id"),
            content.get("asserted_by_entity_ref"),
            content.get("asserted_by"),
        ),
        _graph_reference_key(
            content.get("observed_by_identity_group_id"),
            content.get("observed_by_entity_ref"),
            content.get("observed_by"),
        ),
        _graph_reference_key(
            content.get("holder_identity_group_id"),
            content.get("holder_entity_ref"),
            content.get("holder"),
        ),
        audience_keys,
        _knowledge_item_card_semantic_context(content),
    ]
    serialized = json.dumps(key_fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"fact:{hashlib.sha256(serialized.encode('utf-8')).hexdigest()}"


def _knowledge_graph_search_text(content: dict[str, Any]) -> tuple[str, str]:
    role = str(content.get("candidate_role") or "")
    title = str(
        content.get("identity_display_name")
        or content.get("entity_name")
        or content.get("subject")
        or "知识条目"
    )
    fields = (
        "category", "entity_kind", "entity_name", "identity_display_name",
        "identity_aliases", "subject", "predicate", "value", "state_slot",
        "state_value_status", "target_entity_name", "target_identity_display_name",
        "story_time_expression", "branch_id", "claim_basis", "asserted_by",
        "observed_by", "holder", "audience",
    )
    body_parts: list[str] = [role]
    for field in fields:
        value = content.get(field)
        if isinstance(value, list):
            body_parts.extend(str(item) for item in value if item is not None)
        elif isinstance(value, (str, int, float)):
            body_parts.append(str(value))
    query_claim = content.get("query_learning_claim")
    if isinstance(query_claim, dict):
        for field in ("original_expression", "speaker"):
            value = query_claim.get(field)
            if isinstance(value, str) and value.strip():
                body_parts.append(value)
        conditions = query_claim.get("conditions")
        if isinstance(conditions, list):
            body_parts.extend(str(value) for value in conditions if isinstance(value, str) and value.strip())
    return title, " ".join(body_parts)


def _knowledge_graph_card_match(
    content: dict[str, Any], *, matched_candidate_count: int
) -> KnowledgeCardMatch:
    role = str(content.get("candidate_role") or "fact")
    category = str(content.get("category") or "other")
    if role == "entity":
        title = str(content.get("identity_display_name") or content.get("entity_name") or "未命名实体")
        aliases = content.get("identity_aliases")
        if not isinstance(aliases, list):
            aliases = []
        summary = "、".join(
            dict.fromkeys(
                str(alias).strip()
                for alias in aliases
                if isinstance(alias, str) and alias.strip() and alias.strip() != title
            )
        )
        return KnowledgeCardMatch(
            card_type="entity",
            title=title,
            summary=summary or title,
            category=category,
            matched_candidate_count=matched_candidate_count,
            aliases=tuple(
                str(alias).strip()
                for alias in aliases
                if isinstance(alias, str) and alias.strip()
            ),
        )

    subject = str(
        content.get("identity_display_name")
        or content.get("entity_name")
        or content.get("subject")
        or "未命名主体"
    )
    predicate = str(content.get("predicate") or "")
    value = str(content.get("value") or "") or None
    target = str(
        content.get("target_identity_display_name") or content.get("target_entity_name") or ""
    ) or None
    time_expression = str(content.get("story_time_expression") or "") or None
    summary_value = value or target or ""
    summary = " · ".join(part for part in (subject, predicate, summary_value, time_expression) if part)
    return KnowledgeCardMatch(
        card_type="fact",
        title=subject,
        summary=summary,
        category=category,
        matched_candidate_count=matched_candidate_count,
        predicate=predicate or None,
        value=value,
        target=target,
        story_time_expression=time_expression,
        branch_id=str(content.get("branch_id") or "main"),
    )


def _without_graph_identity_metadata_sql(json_column: str) -> str:
    """Build a fixed SQLite expression for comparing candidate source content."""
    return (
        f"json_set(json_remove({json_column}, {_GRAPH_IDENTITY_JSON_PATHS}), "
        "'$.review_note', NULL)"
    )


def _linked_learning_candidates(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, dict):
        value = content.get("linked_learning_candidates", [])
    elif hasattr(content, "model_dump"):
        dumped = content.model_dump(mode="json")
        value = dumped.get("linked_learning_candidates", [])
    else:
        value = []
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError("linked_learning_candidates must be a list of reference objects")
    if len(value) > 50:
        raise ValueError("a story entity may cite at most 50 linked learning candidates")
    return value


_WORK_LINKED_LEARNING_SOURCE_FILTER = """
(
    EXISTS (
        SELECT 1 FROM writing_learning_source_reference AS exact
        WHERE exact.work_id = ? AND exact.source_id = s.id
    )
    OR EXISTS (
        SELECT 1
        FROM writing_learning_scope_reference AS scope
        WHERE scope.work_id = ?
          AND scope.learning_work_id IS s.work_id
          AND (scope.learning_work_id IS NULL OR EXISTS (
              SELECT 1 FROM knowledge_project AS active_library
              WHERE active_library.id = scope.learning_work_id
                AND active_library.space = 'learning'
                AND active_library.status = 'active'
          ))
          AND (
              scope.folder_id IS NULL
              OR EXISTS (
                  SELECT 1
                  FROM knowledge_folder AS selected_folder
                  JOIN knowledge_folder AS source_folder
                    ON source_folder.space = 'learning'
                   AND source_folder.work_id IS selected_folder.work_id
                  WHERE selected_folder.id = scope.folder_id
                    AND s.folder_id = source_folder.id
                    AND (
                        source_folder.path = selected_folder.path COLLATE NOCASE
                        OR substr(
                            source_folder.path, 1,
                            length(selected_folder.path) + 1
                        ) = selected_folder.path || '/' COLLATE NOCASE
                    )
              )
          )
    )
)
"""

__all__ = [name for name in globals() if not name.startswith('__')]

