"""Read adjacent raw scenes from exact, already-discovered query evidence."""

from .query_contracts import EvidenceRef, QueryOperationResult
from .query_materials import scene_context_span
from .query_scope_refs import revalidate_current_query_evidence
from .service_query_read_scope import consistent_query_read


def grounded_scene_requested(operation):
    return (operation.kind == "locate" and operation.locator_kind == "scene"
            and operation.order_selection == "relevant" and bool(operation.basis_evidence_ids))


class KnowledgeQuerySceneReadMixin:
    @consistent_query_read
    def _retrieve_grounded_scene_operation(self, spec, requirement, operation, scopes, max_items):
        """Expand only current authorized discovery windows, within the query budget."""
        base = dict(operation_id=operation.id, requirement_id=requirement.id,
                    operation="locate", round_index=operation.introduced_round, complete=False)
        available = {ref.id: ref for ref in spec.reused_evidence_refs}
        if any(identifier not in available for identifier in operation.basis_evidence_ids):
            return QueryOperationResult(**base, status="unsupported",
                                        notes=["相邻场景读取的发现依据不可用。"])
        refs = [available[identifier] for identifier in operation.basis_evidence_ids]
        checked = revalidate_current_query_evidence(self, refs, scopes, scope=spec.scope)
        if checked.rejected_ids:
            return QueryOperationResult(**base, status="unsupported",
                                        notes=["相邻场景读取的范围、快照或精确发现依据已失效。"])
        windows, records = [], []
        for basis in checked.valid_refs:
            if len(windows) >= max_items:
                break
            source = checked.source_snapshots[basis.source_id]
            content = source["content"]
            start, end = scene_context_span(basis, content)
            if any(ref.source_id == basis.source_id and ref.start_offset <= start
                   and ref.end_offset >= end for ref in windows):
                continue
            window = EvidenceRef.from_exact_span(
                space=basis.space, work_id=basis.work_id, branch_id=basis.branch_id,
                source_id=basis.source_id, snapshot_id=basis.snapshot_id,
                content_hash=basis.content_hash, start_offset=start, quote=content[start:end],
                role="context", purpose="grounded_scene_context",
            )
            windows.append(window)
            records.append({"record_kind": "grounded_raw_scene", "source_id": basis.source_id,
                "snapshot_id": basis.snapshot_id, "content_hash": basis.content_hash,
                "start_offset": start, "end_offset": end, "offset_unit": window.offset_unit,
                "quote": window.quote, "basis_evidence_ids": [basis.id],
                "evidence_ids": [window.id], "selection_authoritative": False,
                "speaker_verified": False})
        return QueryOperationResult(**base, status="retrieved" if windows else "no_candidate",
            evidence_refs=windows, records=records,
            notes=["已按准确发现位置读取同快照相邻原文；窗口只提供当前问题的候选依据，不证明说话者归属、首次末次或完整清单。"])
