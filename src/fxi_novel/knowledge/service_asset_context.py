"""Read asset evidence through the shared query owner without inventing state."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from .query_contracts import (
    EvidenceRef,
    KnowledgeQuerySpec,
    QueryCoverage,
    RequirementBatchResult,
    RequirementCoverage,
)


@dataclass(frozen=True)
class AssetContextResult:
    query_spec: KnowledgeQuerySpec
    batch: RequirementBatchResult
    materials: dict[str, Any]

    @property
    def evidence_refs(self) -> list[EvidenceRef]:
        return list({
            ref.id: ref
            for operation in self.batch.operation_results
            for ref in operation.evidence_refs
        }.values())


class KnowledgeAssetContextServiceMixin:
    async def read_asset_context(
        self,
        spec: KnowledgeQuerySpec,
        requirement_ids: list[str] | None = None,
    ) -> AssetContextResult:
        """Deliver requested definitions, instances, states and conditions.

        The caller owns the questions and any subsequent read. This operation
        neither expands to every asset nor infers current holdings from an
        incomplete event history. Pagination and unassessed results stay visible.
        """
        selected = (
            [item.id for item in spec.requirements]
            if requirement_ids is None else list(requirement_ids)
        )
        known_ids = {item.id for item in spec.requirements}
        if len(selected) != len(set(selected)) or not set(selected) <= known_ids:
            raise ValueError("asset requirement IDs must be unique members of this query spec")
        if selected:
            batch = await self.retrieve_query_operation_batch(spec, selected)
        else:
            batch = RequirementBatchResult(
                requirements=[],
                applied_scope={"query_scope": spec.scope.model_dump(mode="json")},
            )

        # The standard coverage contract lets existing consumers distinguish
        # retrieved candidates from an actual semantic judgment.
        by_id = {item.requirement_id: item for item in batch.requirements}
        requirements = [
            by_id.get(item.id) or RequirementCoverage(
                requirement_id=item.id,
                outcome="not_executed",
                explanation="本次未读取此需求。",
            )
            for item in spec.requirements
        ]
        unexecuted = [item.requirement_id for item in requirements
                      if item.outcome == "not_executed"]
        coverage = QueryCoverage(
            requirements=requirements,
            budget=spec.budget,
            budget_exhausted=any(
                item.status == "budget_skipped" for item in batch.operation_results
            ),
            unexecuted_requirement_ids=unexecuted,
            applied_scope=batch.applied_scope,
            temporal_filter_applied=batch.temporal_filter_applied,
            temporal_filter_status=batch.temporal_filter_status,
            knowledge_filter_applied=batch.knowledge_filter_applied,
            knowledge_filter_status=batch.knowledge_filter_status,
            notes=batch.retrieval_notes,
            scope_receipts=batch.scope_receipts,
            operation_plan=[item for item in spec.operations if item.requirement_id in selected],
            learning_receipts=batch.learning_receipts,
            # A completed read does not mean all current state has been established.
            completeness="partial" if batch.operation_results else "not_started",
        )
        materials = _asset_materials(batch, coverage)
        return AssetContextResult(query_spec=spec, batch=batch, materials=materials)


def _asset_materials(batch: RequirementBatchResult, coverage: QueryCoverage) -> dict[str, Any]:
    """Share identical rows across questions; keep full retrieval audit in batch.

    Identity alone is not a deduplication key: different states, evidence,
    directions or conditions on the same object must remain separate rows.
    """
    records: dict[str, Any] = {}
    record_keys: dict[str, str] = {}
    evidence: dict[str, EvidenceRef] = {}
    universes: dict[str, Any] = {}
    operations = []
    for operation in batch.operation_results:
        identifiers = []
        for record in operation.records:
            row = json.loads(json.dumps(record, ensure_ascii=False, allow_nan=False))
            key = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            identifier = record_keys.get(key)
            if identifier is None:
                identifier = f"asset-record-{len(records) + 1}"
                record_keys[key] = identifier
                # A verified parse review proves attribution to the source, not
                # present usability. Keep its status and the actual claim basis;
                # its execution transcript belongs in the receipt.
                if row.get("semantic_review_status") == "verified" and row.get("semantic_assessment") == "supported":
                    row.pop("semantic_review", None)
                    content = row.get("content")
                    if isinstance(content, dict):
                        content.pop("semantic_review", None)
                provenance = row.get("query_provenance")
                if isinstance(provenance, dict) and all(row.get(k) == v for k, v in provenance.items()):
                    row.pop("query_provenance")
                records[identifier] = row
            if identifier not in identifiers:
                identifiers.append(identifier)
        evidence.update({ref.id: ref for ref in operation.evidence_refs})
        for universe in operation.event_identity_universes:
            value = universe.model_dump(mode="json")
            universes[json.dumps(value, sort_keys=True, ensure_ascii=False)] = value
        value = operation.model_dump(mode="json", exclude={
            "records", "evidence_refs", "event_identity_universes",
        })
        value["record_ids"] = identifiers
        value["evidence_ids"] = [ref.id for ref in operation.evidence_refs]
        operations.append(value)
    return {
        "query_coverage": coverage.model_dump(mode="json", exclude={
            "scope_receipts", "operation_plan", "learning_receipts", "budget",
        }),
        "records": records,
        "operation_results": operations,
        "evidence_refs": [ref.model_dump(mode="json") for ref in evidence.values()],
        **({"event_identity_universes": list(universes.values())} if universes else {}),
    }
