"""Knowledge service methods for story entities."""

from __future__ import annotations

from .service_support import *
from .service_query_read_scope import consistent_query_read


class KnowledgeStoryEntityServiceMixin:
    """Story entities orchestration on the knowledge domain facade."""

    def save_story_register_entry(self, *, work_id, branch_id, entry):
        """Record a source-checked observation through the existing item owner."""
        from .story_register import StoryRegisterEntry, register_identity
        value = StoryRegisterEntry.model_validate(entry)
        source = self.get_source(value.source_id)
        if (source is None or source["space"] != "writing" or source["work_id"] != work_id
                or source["snapshot"]["id"] != value.snapshot_id
                or source["snapshot"]["content_hash"] != value.content_hash
                or source["snapshot"]["content"][value.start:value.end] != value.quote):
            raise ValueError("register observation must cite the current work's exact original text")
        proof = [row["id"] for row in source["evidence"]
                 if row["start_offset"] <= value.start and row["end_offset"] >= value.end]
        if not proof:
            raise ValueError("register observation must fit an existing evidence span")
        content = TemporalRecordContent(record_type="state" if value.mode == "state" else "fact",
            branch_id=branch_id, subject_id=value.subject, predicate="writing.register",
            state_slot=value.key if value.mode == "state" else None,
            value=value.model_dump(mode="json"), evidence_basis="narration", proposed_assessment="supported",
            review_note="按所引原文登记；最后观察与完整当前状态分开，不据缺失记录推断零库存。")
        key = register_identity(work_id, branch_id, value)
        existing = self.repository.get_knowledge_item_by_idempotency_key(key)
        if existing is not None:
            if existing["content"] != content.model_dump(mode="json"):
                raise ValueError("the observation already exists with different content; edit its exact version")
            return existing
        return self.create_knowledge_item(space="writing", work_id=work_id, kind="temporal_record",
            status="reviewed", content=content.model_dump(mode="json"), evidence_span_ids=proof[:1], idempotency_key=key)

    @consistent_query_read
    def story_state_register(self, *, work_id, branch_id, source_pins, manifest_revision, manifest_hash):
        from .story_register import project_register
        from .story_register_published import published_register_records
        page = self.repository.list_temporal_records(
            work_id, branch_id=branch_id, limit=500, include_rejected=False,
            register_source_pins=source_pins,
        )
        result = project_register(page["records"], source_pins=source_pins, work_id=work_id, branch_id=branch_id,
            manifest_revision=manifest_revision, manifest_hash=manifest_hash, truncated=page.get("has_more", False))
        published = self.repository.list_published_register_candidates(
            work_id=work_id, branch_id=branch_id, source_pins=source_pins)
        parsed = published_register_records(published["records"], source_pins=source_pins,
            branch_id=branch_id, work_id=work_id)
        result["records"].extend(parsed)
        result["truncated"] = result["truncated"] or published["has_more"]
        result["parsed_observation_coverage"] = {
            "source_count": len({item["source_id"] for row in parsed for item in row["history"]}),
            "record_count": len(parsed), "complete": False,
        }
        return result

    def create_temporal_record(
        self,
        *,
        work_id: str,
        content: TemporalRecordContent,
        evidence_span_ids: list[str],
    ) -> dict[str, Any]:
        """Save one evidence-backed temporal record as a reviewable draft."""
        validate_scope("writing", work_id)
        validated = TemporalRecordContent.model_validate(content)
        if not evidence_span_ids:
            raise ValueError("temporal records require at least one evidence span")
        return self.repository.create_temporal_record(
            work_id=work_id,
            content=validated,
            evidence_span_ids=evidence_span_ids,
        )

    def create_story_entity(
        self,
        *,
        work_id: str,
        content: StoryEntityContent | dict[str, Any],
        evidence_span_ids: list[str],
    ) -> dict[str, Any]:
        """Create one typed identity, instance, name, or relation candidate."""
        validate_scope("writing", work_id)
        evidence_ids = list(dict.fromkeys(evidence_span_ids))
        if len(evidence_ids) > 50:
            raise ValueError("story entities may cite at most 50 evidence spans")
        validated = TypeAdapter(StoryEntityContent).validate_python(content)
        linked_candidates = getattr(validated, "linked_learning_candidates", [])
        if not evidence_ids and not linked_candidates:
            raise ValueError("story entities require original evidence or linked learning candidates")
        self._validate_story_linked_candidate_names(validated)
        self._validate_story_entity_links(work_id, validated)
        created = self.repository.create_knowledge_item(
            space="writing",
            work_id=work_id,
            kind=f"story_{validated.record_type}",
            status="draft",
            content=validated.model_dump(mode="json"),
            evidence_span_ids=evidence_ids,
        )
        detail = self.repository.get_knowledge_item(created["id"])
        if detail is None:
            raise RuntimeError("created story entity could not be read back")
        return detail

    def list_story_entities(self, work_id: str) -> list[dict[str, Any]]:
        return self.repository.list_story_entities(work_id)

    @staticmethod
    def _validate_story_linked_candidate_names(content: BaseModel) -> None:
        references = getattr(content, "linked_learning_candidates", [])
        if not references:
            return
        if isinstance(content, StoryIdentityContent):
            expected_name = content.canonical_name
        elif isinstance(content, StoryNameBindingContent):
            expected_name = content.name
        else:
            raise ValueError("only stable identities and name bindings can link learning candidates")

        def normalize(value: str) -> str:
            return unicodedata.normalize("NFKC", value).strip().lower()

        expected = normalize(expected_name)
        if any(normalize(reference.entity_name) != expected for reference in references):
            raise ValueError("linked candidate names must match the identity or bound name exactly")

    def list_work_linked_identity_candidates(
        self, work_id: str, query: str
    ) -> dict[str, Any]:
        validate_scope("writing", work_id)
        normalized_query = query.strip()
        if not normalized_query:
            raise ValueError("a character name search is required")
        return self.repository.list_work_linked_identity_candidates(
            work_id, normalized_query, limit=200
        )

