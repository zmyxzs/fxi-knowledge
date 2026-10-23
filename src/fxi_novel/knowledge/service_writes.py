"""Knowledge service methods for writes."""

from __future__ import annotations

from .service_support import *


class KnowledgeWriteServiceMixin:
    """Writes orchestration on the knowledge domain facade."""

    def rebuild_search_index(self) -> int:
        """Recreate the FTS projection from authoritative chunk text."""
        return self.repository.rebuild_search_index()

    def ensure_knowledge_item_index(self) -> int:
        """Backfill the rebuildable search projection for saved study analyses."""
        return self.repository.ensure_knowledge_item_index()

    def create_knowledge_item(
        self,
        *,
        space: str,
        work_id: str | None,
        kind: str,
        status: str,
        content: Any,
        evidence_span_ids: Iterable[str],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Create a knowledge item and its first evidence-backed version."""
        if kind in {
            "story_identity", "story_instance", "story_name_binding",
            "story_identity_relation", "story_voice_profile",
        }:
            raise ValueError("story entities must be created through create_story_entity")
        try:
            return self.repository.create_knowledge_item(
                space=space,
                work_id=work_id,
                kind=kind,
                status=status,
                content=content,
                evidence_span_ids=evidence_span_ids,
                idempotency_key=idempotency_key,
            )
        except sqlite3.IntegrityError as exc:
            if idempotency_key is None or "knowledge_item.idempotency_key" not in str(exc):
                raise
            existing = self.repository.get_knowledge_item_by_idempotency_key(idempotency_key)
            if existing is None:
                raise
            existing["reused_existing"] = True
            return existing

    def append_knowledge_version(
        self,
        item_id: str,
        *,
        status: str,
        content: Any,
        evidence_span_ids: Iterable[str],
        expected_version: str,
    ) -> dict[str, Any]:
        """Append a version while retaining prior content and provenance."""
        evidence_ids = tuple(dict.fromkeys(evidence_span_ids))
        current = self.repository.get_knowledge_item(item_id)
        if (
            expected_version is not None
            and current is not None
            and expected_version != current.get("version_id")
        ):
            raise KnowledgeItemVersionConflictError(
                f"knowledge item version changed (expected {expected_version}, current {current.get('version_id')})"
            )
        is_style_method_card = bool(
            current is not None
            and current.get("kind") == "source_study"
            and isinstance(current.get("content"), dict)
            and current["content"].get("analysis_type") == "style"
            and current["content"].get("learning_role") == "craft_method_synthesis"
        )
        current_correction = (
            current.get("content", {}).get("semantic_correction")
            if current is not None and isinstance(current.get("content"), dict)
            else None
        )
        incoming_correction = (
            content.get("semantic_correction") if isinstance(content, dict) else None
        )
        has_correction_metadata = (
            "semantic_correction" in current.get("content", {})
            if current is not None and isinstance(current.get("content"), dict)
            else False
        ) or (isinstance(content, dict) and "semantic_correction" in content)
        semantic_correction_changed = (
            has_correction_metadata and current_correction != incoming_correction
        )
        if semantic_correction_changed and not is_style_method_card:
            raise ValueError("semantic_correction metadata is only valid for a style-method study item")
        if is_style_method_card:
            if not isinstance(content, dict):
                raise ValueError("style-method study content must be an object")
            if (
                content.get("analysis_type") != "style"
                or content.get("learning_role") != "craft_method_synthesis"
            ):
                raise ValueError("style-method study identity is immutable")
            try:
                style_payload = StoredWritingStyleMethodPayload.model_validate(
                    content.get("craft_method_payload")
                )
            except ValidationError as exc:
                raise ValueError(f"invalid stored style-method payload: {exc}") from exc
            evidence_context = current
            if set(evidence_ids) - set(current.get("evidence_span_ids", [])):
                # A new card version may cite another registered excerpt of
                # the same pinned sources. Validate real coordinates, never
                # coordinates supplied in the incoming method payload.
                spans = self.repository.read_evidence_spans(evidence_ids)
                original_ids = set(current.get("evidence_span_ids", []))
                source_pins = {(row["source_id"], row["snapshot_id"], row["content_hash"])
                               for row in current["content"].get("source_refs", [])}
                if any(row["id"] not in original_ids and
                       (row["source_id"], row["snapshot_id"], row["source_version_hash"]) not in source_pins
                       for row in spans):
                    raise ValueError("new style evidence must belong to the original pinned sources")
                evidence_context = {**current, "evidence": spans}
            validate_stored_style_method_evidence(evidence_context, style_payload, evidence_ids)
            if semantic_correction_changed:
                if set(evidence_ids) != set(current.get("evidence_span_ids", [])):
                    raise ValueError("semantic corrections must preserve the study item's evidence references")
                if not isinstance(incoming_correction, dict):
                    raise ValueError("semantic_correction metadata must be an object")
                if incoming_correction.get("previous_version") != current.get("version_id"):
                    raise ValueError("semantic_correction previous_version must match the current version ID")
                corrected_method = next(
                    (method for method in style_payload.methods
                     if method.method_id == incoming_correction.get("method_id")),
                    None,
                )
                if corrected_method is None:
                    raise ValueError("semantic_correction must identify a method on this card")
                correction_evidence_ids = incoming_correction.get("evidence_ids")
                if (
                    not isinstance(correction_evidence_ids, list)
                    or not correction_evidence_ids
                    or any(not isinstance(value, str) for value in correction_evidence_ids)
                    or not set(correction_evidence_ids).issubset(set(evidence_ids))
                ):
                    raise ValueError("semantic_correction evidence_ids must cite this study item's evidence")
                field_diff = incoming_correction.get("field_diff")
                if (
                    not isinstance(field_diff, list)
                    or not field_diff
                    or any(not isinstance(row, dict) or not isinstance(row.get("field"), str)
                           or not row["field"].strip() for row in field_diff)
                ):
                    raise ValueError("semantic_correction field_diff must identify changed method fields")
                for field_name in ("reviewer", "reason", "scope"):
                    value = incoming_correction.get(field_name)
                    if not isinstance(value, str) or not value.strip():
                        raise ValueError(f"semantic_correction {field_name} must not be empty")
                expected_method_hash = hashlib.sha256(
                    json.dumps(
                        corrected_method.model_dump(mode="json"),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                if incoming_correction.get("corrected_method_sha256") != expected_method_hash:
                    raise ValueError("semantic_correction method hash does not match its typed method")
        if current is not None and status == "rejected":
            if current["kind"] not in {"temporal_record", "knowledge_graph_candidate"}:
                raise ValueError("only temporal records and extraction candidates may be rejected")
            reason = content.get("review_note") if isinstance(content, dict) else None
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError("a rejection reason is required")
        elif isinstance(content, dict) and content.get("review_note"):
            raise ValueError("review_note is only allowed while the item is rejected")
        if current is not None and current["kind"] == "temporal_record":
            previous = TemporalRecordContent.model_validate(current["content"])
            updated = TemporalRecordContent.model_validate(content)
            previous_data = previous.model_dump(mode="json")
            updated_data = updated.model_dump(mode="json")
            previous_data.pop("review_note", None)
            updated_data.pop("review_note", None)
            if updated_data != previous_data:
                raise ValueError(
                    "temporal record content is immutable; add a linked record for later corrections"
                )
        if current is not None and current["kind"] in {
            "story_identity", "story_instance",
            "story_name_binding", "story_identity_relation",
            "story_voice_profile",
        }:
            if status not in {"draft", "reviewed"}:
                raise ValueError("story entities can only be drafts or manually reviewed")
            if current["space"] != "writing" or current["work_id"] is None:
                raise ValueError("story entities must belong to a writing work")
            validated = TypeAdapter(StoryEntityContent).validate_python(content)
            if current["kind"] != f"story_{validated.record_type}":
                raise ValueError("story entity kind cannot change across versions")
            if status == "reviewed":
                if not evidence_ids and not getattr(
                    validated, "linked_learning_candidates", []
                ):
                    raise ValueError("a story entity needs original evidence or linked learning candidates before review")
                assessment = getattr(validated, "proposed_assessment", None)
                if assessment is not None and assessment not in {"supported", "reinterpreted"}:
                    raise ValueError("an unresolved, disputed, or contradicted story entity cannot be reviewed")
            self._validate_story_linked_candidate_names(validated)
            self._validate_story_entity_links(current["work_id"], validated)
            content = validated.model_dump(mode="json")
        if current is not None and current["kind"] == "knowledge_graph_candidate":
            if current["space"] not in {"learning", "writing"}:
                raise ValueError("knowledge graph candidates must belong to a knowledge space")
            previous = KnowledgeGraphCandidateContent.model_validate(current["content"])
            updated = KnowledgeGraphCandidateContent.model_validate(content)
            if updated.identity_bindings != previous.identity_bindings:
                raise ValueError(
                    "author-confirmed identity bindings can only be changed through the scoped binding workflow"
                )
            if updated.identity_review_decisions != previous.identity_review_decisions:
                raise ValueError(
                    "author identity review decisions can only be changed through the scoped review workflow"
                )
            immutable_link_fields = (
                "candidate_role",
                "entity_ref",
                "entity_name",
                "entity_kind",
                "target_entity_ref",
                "target_entity_name",
                "subject",
                "identity_group_id",
                "identity_display_name",
                "target_identity_group_id",
                "target_identity_display_name",
                "asserted_by",
                "asserted_by_entity_ref",
                "asserted_by_identity_group_id",
                "observed_by",
                "observed_by_entity_ref",
                "observed_by_identity_group_id",
                "holder",
                "holder_entity_ref",
                "holder_identity_group_id",
                "audience",
                "audience_entity_refs",
                "audience_identity_group_ids",
                "branch_id",
            )
            if any(
                getattr(updated, field) != getattr(previous, field)
                for field in immutable_link_fields
            ):
                raise ValueError("candidate entity links and scope are immutable")
            if (
                updated.evidence_quote_start != previous.evidence_quote_start
                or updated.evidence_quote_end != previous.evidence_quote_end
                or updated.narrative_order != previous.narrative_order
                or updated.source_chapter_order != previous.source_chapter_order
            ):
                raise ValueError("candidate evidence and chapter position are immutable; create a new candidate to change them")
            if set(evidence_ids) != set(current["evidence_span_ids"]):
                raise ValueError("candidate evidence references are immutable")
            if not any(
                evidence["start_offset"] == updated.evidence_quote_start
                and evidence["end_offset"] == updated.evidence_quote_end
                for evidence in current["evidence"]
            ):
                raise ValueError("candidate must retain its exact original evidence quote")
            content = updated.model_dump(mode="json")
        return self.repository.append_knowledge_version(
            item_id,
            status=status,
            content=content,
            evidence_span_ids=evidence_ids,
            expected_version=expected_version,
        )

    def create_relation(
        self,
        source_item_id: str,
        target_item_id: str,
        relation_type: str,
        evidence_span_id: str,
    ) -> str:
        """Create a scoped, evidence-backed relation between knowledge items."""
        return self.repository.create_relation(
            source_item_id,
            target_item_id,
            relation_type,
            evidence_span_id,
        )

