"""Resolve declared query permissions into exact physical source scope pins."""

import hashlib
from dataclasses import dataclass, field
from collections.abc import Sequence

from .query_contracts import (
    EvidenceRef, EvidenceScopePin, KnowledgeQueryScope, QuerySessionContext,
)


_SQLITE_NOCASE_ASCII = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"
)


def _sqlite_nocase(value: str) -> str:
    """Mirror SQLite's built-in NOCASE collation (ASCII-only case folding)."""
    return value.translate(_SQLITE_NOCASE_ASCII)


def resolve_query_evidence_scope_pins(
    knowledge, scope, resolved_scopes, *, extra_scope_pins=(),
):
    """Materialize exact citation permissions from already-resolved retrieval scopes.

    Retrieval may retain registered folder roots for efficient SQL execution. Pins
    still expand those roots to exact source IDs so citation validation cannot
    accidentally authorize sibling folders sharing the same physical work ID.
    """
    base = (
        knowledge.list_work_base_reference(scope.work_id)
        if scope.space == "writing" and scope.work_id is not None
        else None
    )
    pins = []
    resolved_source_ids = getattr(resolved_scopes, "exact_source_ids_by_scope", None)
    logical_work_ids = getattr(
        resolved_scopes, "logical_scope_work_ids_by_scope", None
    )
    for index, (space, work_id, source_ids, folder_ids) in enumerate(resolved_scopes):
        pre_resolved_ids = (
            resolved_source_ids[index]
            if resolved_source_ids is not None and index < len(resolved_source_ids)
            else None
        )
        if pre_resolved_ids is not None:
            # Stable learning-work resolution already applied the registration and
            # caller selectors. Reuse its exact source list instead of reading the
            # same source and folder indexes a second time.
            exact = list(dict.fromkeys(pre_resolved_ids))
        else:
            rows = knowledge.list_source_selection_index(space, work_id, limit=10_000)
            selected = set(source_ids) if source_ids is not None else None
            folders_by_id = {
                item["id"]: item["path"]
                for item in knowledge.list_folders(space, work_id)
            }
            paths = [
                _sqlite_nocase(folders_by_id[value])
                for value in folder_ids or []
                if value in folders_by_id
            ]
            exact = []
            for row in rows:
                row_id = row["id"]
                row_path = _sqlite_nocase(str(row.get("folder_path") or ""))
                if selected is None and folder_ids is None:
                    exact.append(row_id)
                elif selected is not None and row_id in selected:
                    exact.append(row_id)
                elif any(
                    row_path == path or row_path.startswith(path + "/")
                    for path in paths
                ):
                    exact.append(row_id)
        branch = None
        logical_scope_work_id = (
            logical_work_ids[index]
            if logical_work_ids is not None and index < len(logical_work_ids)
            else None
        )
        if space == "writing":
            if work_id == scope.work_id:
                branch = scope.branch
            elif base and work_id == base.get("base_work_id"):
                branch = base.get("base_branch_id")
        for candidate in extra_scope_pins:
            if candidate.space == space and candidate.work_id == work_id:
                branch = candidate.branch_id
                break
        pins.append(
            EvidenceScopePin(
                space=space,
                work_id=work_id,
                logical_scope_work_id=logical_scope_work_id,
                branch_id=branch,
                source_ids=exact,
                folder_ids=None,
            )
        )
    return pins


def resolve_query_evidence_scopes(
    knowledge, scope, *, resolved_scopes=None, extra_scope_pins=(),
):
    resolved = resolved_scopes
    if resolved is None:
        resolved = knowledge._retrieval._search_scopes(
            scope.space, scope.work_id, scope.source_ids, scope.folder_ids,
            scope.include_linked_scopes,
        )
    return resolve_query_evidence_scope_pins(
        knowledge, scope, resolved, extra_scope_pins=extra_scope_pins,
    )


def revalidate_query_session_context(knowledge, scope, raw):
    context = QuerySessionContext.model_validate(raw)
    if context.scope != scope:
        return None
    pins = knowledge.resolve_query_evidence_scopes(scope)
    checked = list(revalidate_current_query_evidence(
        knowledge, context.evidence_refs, pins,
    ).valid_refs)
    valid_ids = {ref.id for ref in checked}
    context.evidence_scopes = pins
    context.evidence_refs = checked
    context.time_constraints = [constraint for constraint in context.time_constraints
        if all(ref.id in valid_ids for ref in constraint.basis_evidence_refs)]
    return context


@dataclass(frozen=True)
class CurrentQueryEvidence:
    """An execution-time read, never a reusable authorization cache."""

    valid_refs: tuple[EvidenceRef, ...]
    rejected_ids: frozenset[str]
    source_snapshots: dict = field(default_factory=dict, repr=False)


def revalidate_current_query_evidence(
    knowledge,
    references: Sequence[EvidenceRef],
    scope_pins: Sequence[EvidenceScopePin],
    *, scope=None,
) -> CurrentQueryEvidence:
    """Check current scope, snapshot, hash and exact text once per source.

    Historical quotes remain valid audit records, but cannot support a current
    query after their source was replaced or revoked. Every call reads current
    state; the within-call cache only avoids repeated full chapter hashing.
    """
    current_pins = list(scope_pins)
    if references and scope is not None:
        current_pins = list(knowledge.resolve_query_evidence_scopes(scope))
        # Context owners can execute a separately authorized direct-base query
        # while keeping the primary writing scope closed (for example, source_ids=[]).
        # Revalidate only that currently registered base, and only as a currentness
        # check: the receipt pins below remain the independent authorization cap.
        has_external_receipt_scope = any(
            pin.space != scope.space or pin.work_id != scope.work_id
            or pin.branch_id != scope.branch
            for pin in scope_pins
        )
        if (
            scope.space == "writing"
            and scope.work_id is not None
            and has_external_receipt_scope
        ):
            current_pins.extend(_current_direct_base_scope_pins(knowledge, scope))
    authorized_ids = {
        ref.source_id for ref in references
        if any(pin.space == ref.space and pin.work_id == ref.work_id
               and pin.branch_id == ref.branch_id
               and not (pin.folder_ids and pin.source_ids is None)
               and (pin.source_ids is None or ref.source_id in pin.source_ids)
               for pin in scope_pins)
        and any(pin.space == ref.space and pin.work_id == ref.work_id
                and pin.branch_id == ref.branch_id
                and (pin.source_ids is None or ref.source_id in pin.source_ids)
                and not (pin.folder_ids and pin.source_ids is None)
                for pin in current_pins)
    }
    snapshots = knowledge.get_current_query_source_snapshots(sorted(authorized_ids)) if authorized_ids else {}
    sources = {}
    checked: list[EvidenceRef] = []
    rejected: set[str] = set()
    for reference in references:
        try:
            parsed = EvidenceRef.model_validate(reference.model_dump(mode="json"))
            expected = EvidenceRef.from_exact_span(
                space=parsed.space, work_id=parsed.work_id, branch_id=parsed.branch_id,
                source_id=parsed.source_id, snapshot_id=parsed.snapshot_id,
                content_hash=parsed.content_hash, start_offset=parsed.start_offset,
                quote=parsed.quote, role=parsed.role, purpose=parsed.purpose,
            )
        except ValueError:
            rejected.add(reference.id)
            continue
        if expected != reference:
            rejected.add(reference.id)
            continue
        authorized = any(
            pin.space == reference.space
            and pin.work_id == reference.work_id
            and pin.branch_id == reference.branch_id
            and not (pin.folder_ids and pin.source_ids is None)
            and (pin.source_ids is None or reference.source_id in pin.source_ids)
            for pin in scope_pins
        )
        if not authorized or reference.source_id not in authorized_ids:
            rejected.add(reference.id)
            continue
        if reference.source_id not in sources:
            row = snapshots.get(reference.source_id)
            snapshot = ({"id": row["snapshot_id"], "content": row["content"],
                         "content_hash": row["content_hash"]} if row else None)
            content = snapshot.get("content") if snapshot else None
            sources[reference.source_id] = (
                snapshot,
                content,
                hashlib.sha256(content.encode("utf-8")).hexdigest()
                if isinstance(content, str) else None,
            )
        snapshot, content, digest = sources[reference.source_id]
        if (
            snapshot is None
            or snapshots[reference.source_id]["space"] != reference.space
            or snapshots[reference.source_id]["work_id"] != reference.work_id
            or snapshot.get("id") != reference.snapshot_id
            or snapshot.get("content_hash") != reference.content_hash
            or digest != reference.content_hash
            or content[reference.start_offset:reference.end_offset] != reference.quote
        ):
            rejected.add(reference.id)
        else:
            checked.append(reference)
    return CurrentQueryEvidence(tuple(checked), frozenset(rejected), snapshots)


def _current_direct_base_scope_pins(knowledge, scope) -> list[EvidenceScopePin]:
    """Resolve the current exact base registration without following other links."""
    base_scope, _note = resolve_direct_base_scope(knowledge, scope.work_id)
    return list(knowledge.resolve_query_evidence_scopes(base_scope)) if base_scope is not None else []


def resolve_direct_base_scope(knowledge, work_id):
    """Interpret the one registered base consistently for reads and revalidation."""
    try:
        base = knowledge.list_work_base_reference(work_id)
    except Exception as exc:
        return None, f"直接关联底座范围读取失败（{type(exc).__name__}）。"
    if not base:
        return None, None
    space = str(base.get("space") or "")
    base_work_id = str(base.get("base_work_id") or "")
    branch_id = base.get("base_branch_id")
    if (space not in {"learning", "writing"} or not base_work_id
            or (space == "learning" and branch_id is not None)
            or (space == "writing" and not isinstance(branch_id, str))):
        return None, "直接关联底座身份或分支不完整；未扩大检索范围。"
    selected_folders = base.get("scope_folder_ids") or []
    folders = [str(item) for item in selected_folders if item is not None]
    whole_base = any(item is None for item in selected_folders)
    source_ids = list(dict.fromkeys(str(item) for item in base.get("source_ids") or []))
    if not whole_base and not folders and not source_ids:
        return None, "直接关联底座没有有效的资料范围。"
    try:
        return KnowledgeQueryScope(
            space=space, work_id=base_work_id,
            purpose="canon_fact" if space == "learning" else "work_fact",
            branch=branch_id, source_ids=None if whole_base else source_ids,
            folder_ids=None if whole_base else (folders or None), include_linked_scopes=False,
        ), None
    except (TypeError, ValueError):
        return None, "直接关联底座的范围无法通过知识查询契约校验。"
