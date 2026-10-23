"""Knowledge service methods for sources."""

from __future__ import annotations

import hashlib
import json

from .service_support import *
from .source_edit_conflict import CreativeNoteSaveConflict, SourceEditConflict


class KnowledgeSourceServiceMixin:
    """Sources orchestration on the knowledge domain facade."""

    def read_author_inputs(self, work_id, inputs):
        from .author_input_coverage import project_inputs
        from .creative_note_context import current_creative_note_context
        with self.repository.database.read_snapshot(), self.repository.database.connection() as connection:
            return {"inputs": project_inputs(connection, work_id, inputs),
                    "notes": current_creative_note_context(connection, work_id)}

    def save_author_input_resolution(self, *, work_id, inputs, notes_pin, response, resolution_id):
        from .author_input_coverage import save_resolution
        with self.repository.database.transaction(immediate=True) as connection:
            save_resolution(connection, work_id=work_id, inputs=inputs, notes_pin=notes_pin,
                            response=response, resolution_id=resolution_id)

    def reusable_author_input_resolution(self, work_id, packet):
        import json
        inputs = packet["inputs"]
        revision = inputs[-1].get("coverage_revision") if inputs else None
        if revision is None:
            return None
        with self.repository.database.connection() as connection:
            row = connection.execute("SELECT input_json,response_json FROM author_input_resolution WHERE id=? AND work_id=?",
                                     (revision, work_id)).fetchone()
        if row is None:
            return None
        saved, response = json.loads(row["input_json"]), json.loads(row["response_json"])
        if (saved["notes_pin"]["records"] != packet["notes"]["pin"]["records"]
                or [item["input_id"] for item in saved["inputs"]] != [item["input_id"] for item in inputs]
                or any(item["remaining_text"].strip() != current["content"] for item, current in zip(
                    sorted(response["inputs"], key=lambda item: item["input_id"]),
                    sorted(inputs, key=lambda item: item["input_id"]), strict=True))):
            return None
        return response

    def project_author_history(self, work_id, history):
        """Return current unincorporated requirements; never reimport a handled body."""
        from .author_input_coverage import project_inputs
        with self.repository.database.connection() as connection:
            rows = project_inputs(connection, work_id, history)
        result = []
        for row in rows:
            if not row["content"].strip():
                continue
            source = {**(row.get("source") or {}), "projection_hash": hashlib.sha256(row["content"].encode()).hexdigest(),
                      **({"author_input_id": row["input_id"]} if row.get("handled") else {})}
            result.append({"role": row["role"], "content": row["content"], "source": source})
        return result

    def current_creative_note_context(self, work_id: str) -> dict[str, Any]:
        from .creative_note_context import current_creative_note_context
        with self.repository.database.read_snapshot(), self.repository.database.connection() as connection:
            return current_creative_note_context(connection, work_id)

    def validate_creative_note_context(self, work_id: str, context: dict[str, Any]) -> dict[str, Any]:
        """Verify a sealed reading against exact source history, including retired notes."""
        from fxi_novel.writing.chapter_author_sources import normalize_creative_note_context
        try:
            context = normalize_creative_note_context(context, work_id=work_id)
        except ValueError as exc:
            raise ValueError("saved creative-note context changed after delivery") from exc
        if (not isinstance(context, dict) or not {"pin", "notes"} <= set(context)
                or not isinstance(context["pin"], dict) or not {"work_id", "records"} <= set(context["pin"])
                or context["pin"]["work_id"] != work_id or not isinstance(context["notes"], list)
                or not isinstance(context["pin"]["records"], list)):
            raise ValueError("saved creative-note context is invalid")
        notes, ids = [], []
        with self.repository.database.read_snapshot():
            for index, pin in enumerate(context["pin"]["records"], 1):
                if (not isinstance(pin, dict) or set(pin) != {"source_id", "edit_revision", "snapshot_id", "content_hash"}
                        or type(pin["edit_revision"]) is not int or pin["edit_revision"] < 1):
                    raise ValueError("saved creative-note record pin is invalid")
                try:
                    source = self.get_source(pin["source_id"], record_revision=pin["edit_revision"], include_evidence=False)
                except KeyError:
                    source = self.get_source(pin["source_id"], include_evidence=False)
                    if source is None or source["edit_revision"] != pin["edit_revision"]:
                        raise ValueError("saved creative-note history is unavailable")
                if (not source or source["space"] != "writing" or source["work_id"] != work_id
                        or source["document_metadata"].get("kind") != "creative_note"
                        or source["selected_snapshot_id"] != pin["snapshot_id"]
                        or source["snapshot"]["content_hash"] != pin["content_hash"]):
                    raise ValueError("saved creative-note source no longer matches its exact reading")
                ids.append(pin["source_id"])
                notes.append({"id": f"N{index}", "title": source["title"],
                              "content": source["snapshot"]["content"], "attributes": source["document_metadata"]})
        if ids != sorted(set(ids)) or notes != context["notes"]:
            raise ValueError("saved creative-note context changed after delivery")
        if context.get("author_inputs"):
            with self.repository.database.connection() as connection:
                for row in context["author_inputs"]:
                    saved = connection.execute("SELECT input_json,response_json FROM author_input_resolution WHERE id=? AND work_id=?",
                        (row["resolution_id"], work_id)).fetchone()
                    if saved is None:
                        raise ValueError("saved author input resolution is unavailable")
                    inputs = json.loads(saved["input_json"])["inputs"]
                    decisions = json.loads(saved["response_json"])["inputs"]
                    original = next((item for item in inputs if item["input_id"] == row["id"]), None)
                    decision = next((item for item in decisions if item["input_id"] == row["id"]), None)
                    if (not original or not decision or original["source"] != row["source"]
                            or original["original_hash"] != row["original_hash"]
                            or decision["disposition"] != row["disposition"]
                            or decision["remaining_text"].strip() != row["remaining_text"]):
                        raise ValueError("saved author input resolution changed after delivery")
        return context

    def add_text_source(
        self,
        title: str,
        content: str,
        space: str,
        work_id: str | None = None,
        source_uri: str | None = None,
        folder_id: str | None = None,
        folder_path: str | None = None,
        source_encoding: str = "utf-8",
        document_metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        operation_origin: dict[str, Any] | None = None,
    ) -> SourceImportResult:
        if ((source_uri and source_uri.startswith("prose://"))
                or (document_metadata and "prose_projection" in document_metadata)):
            raise ValueError("prose projection sources are maintained by chapter adoption")
        return self.repository.add_text_source(
            title=title,
            content=content,
            space=space,
            work_id=work_id,
            source_uri=source_uri,
            folder_id=folder_id,
            folder_path=folder_path,
            source_encoding=source_encoding,
            document_metadata=document_metadata,
            idempotency_key=idempotency_key,
            operation_origin=operation_origin,
        )

    def update_text_source(
        self,
        source_id: str,
        *,
        title: str,
        source_uri: str | None,
        content: str,
        document_metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        operation_origin: dict[str, Any] | None = None,
        expected_edit_revision: int | None = None,
    ) -> SourceImportResult:
        """Edit source metadata and append a new immutable snapshot when content changes."""
        source = self.repository.get_source(
            source_id, content_offset=0, content_limit=1, include_evidence=False,
        )
        metadata = source.get("document_metadata") if source is not None else None
        if isinstance(metadata, dict) and "prose_projection" in metadata:
            raise ValueError("正文索引来自已采用的章节版本；请在正文版本中修改并重新采用。")
        if ((source_uri and source_uri.startswith("prose://"))
                or (document_metadata and "prose_projection" in document_metadata)):
            raise ValueError("prose projection sources are maintained by chapter adoption")
        return self.repository.update_source(
            source_id,
            title=title,
            source_uri=source_uri,
            content=content,
            document_metadata=document_metadata,
            idempotency_key=idempotency_key,
            operation_origin=operation_origin,
            expected_edit_revision=expected_edit_revision,
        )

    def save_creative_note_candidate(
        self, *, work_id: str, candidate: dict[str, Any], save_mode: str,
        title: str, content: str, folder_id: str | None,
        idempotency_key: str, operation_origin: dict[str, Any],
    ) -> dict[str, Any]:
        """Save a reviewed candidate against the version read during preparation."""
        kind = candidate.get("kind")
        decision = candidate.get("decision_status")
        if (not isinstance(kind, str) or kind not in {"concept", "character", "setting", "relationship", "item",
                         "plot_decision", "theme", "other"}
                or not isinstance(decision, str) or decision not in {"confirmed", "proposal"}
                or not isinstance(candidate.get("summary"), str)
                or not isinstance(candidate.get("content", ""), str)
                or not isinstance(candidate.get("references"), list)):
            raise ValueError("capture candidate metadata is invalid")
        base = None
        expected = candidate.get("base_edit_revision")
        if save_mode == "update":
            target_id = candidate.get("update_source_id")
            if not isinstance(target_id, str) or not target_id.strip():
                raise ValueError("this capture candidate has no existing source to update")
            if type(expected) is not int or expected < 1:
                raise CreativeNoteSaveConflict(
                    "capture_base_missing", "旧候选未绑定读取版本；请基于当前完整资料重新整理后保存。",
                )
            current = self.get_source(target_id, include_evidence=False)
            if current is None:
                raise KeyError("capture candidate target source not found")
            if current["space"] != "writing" or current["work_id"] != work_id:
                raise CreativeNoteSaveConflict("capture_scope_conflict", "目标资料不属于当前作品。")
            try:
                base = self.get_source(target_id, record_revision=expected, include_evidence=False)
            except KeyError:
                if current["edit_revision"] != expected:
                    raise SourceEditConflict(target_id, expected, current["edit_revision"])
                base = current
            if base is None:
                raise KeyError("capture candidate target source not found")
            if (base["space"] != "writing" or base["work_id"] != work_id
                    or base["document_metadata"].get("kind") != "creative_note"):
                raise CreativeNoteSaveConflict("capture_target_conflict", "候选只能更新本作的创作笔记。")
            if base["document_metadata"].get("decision_status") == "confirmed" and decision != "confirmed":
                raise CreativeNoteSaveConflict(
                    "capture_proposal_conflict", "待讨论改写不能覆盖已定设定；可作为独立提案保存。",
                )
        elif save_mode != "create":
            raise ValueError("invalid capture save mode")
        metadata = {
            **(base["document_metadata"] if base else {}),
            "schema_version": 1, "kind": "creative_note", "creative_type": kind,
            "decision_status": decision,
            # An author may edit the candidate body before saving. Its old model
            # summary no longer describes that text reliably; don't keep it current.
            "summary": candidate["summary"] if content.strip() == candidate.get("content", "").strip() else "",
            "references": candidate["references"],
        }
        if base is not None:
            result = self.update_text_source(
                base["id"], title=title, source_uri=base["source_uri"], content=content,
                document_metadata=metadata, expected_edit_revision=expected,
                idempotency_key=idempotency_key, operation_origin=operation_origin,
            )
        else:
            result = self.add_text_source(
                title=title, content=content, space="writing", work_id=work_id,
                folder_id=folder_id, document_metadata=metadata,
                idempotency_key=idempotency_key, operation_origin=operation_origin,
            )
        return self._creative_note_save_result(result)

    def replace_creative_note(
        self, source_id: str, *, work_id: str, base_edit_revision: int,
        title: str, content: str, idempotency_key: str, operation_origin: dict[str, Any],
    ) -> dict[str, Any]:
        """Replace an existing note without changing its author status or scope."""
        current = self.get_source(source_id, include_evidence=False)
        if current is None:
            raise KeyError("creative note not found")
        if current["space"] != "writing" or current["work_id"] != work_id:
            raise CreativeNoteSaveConflict("note_scope_conflict", "目标笔记不属于当前作品。")
        if type(base_edit_revision) is not int or base_edit_revision < 1:
            raise ValueError("a creative note replacement requires its read revision")
        try:
            base = self.get_source(source_id, record_revision=base_edit_revision, include_evidence=False)
        except KeyError:
            if current["edit_revision"] != base_edit_revision:
                raise SourceEditConflict(source_id, base_edit_revision, current["edit_revision"])
            base = current
        if (base is None or base["space"] != "writing" or base["work_id"] != work_id
                or base["document_metadata"].get("kind") != "creative_note"):
            raise CreativeNoteSaveConflict("note_target_conflict", "只能替换已有创作笔记。")
        metadata = dict(base["document_metadata"])
        result = self.update_text_source(
            source_id, title=title, source_uri=base["source_uri"], content=content,
            document_metadata=metadata, expected_edit_revision=base_edit_revision,
            idempotency_key=idempotency_key, operation_origin=operation_origin,
        )
        return self._creative_note_save_result(result)

    def create_creative_note(
        self, *, work_id: str, title: str, content: str,
        creative_type: str = "other", decision_status: str = "proposal",
        folder_id: str | None = None, idempotency_key: str,
        operation_origin: dict[str, Any],
    ) -> dict[str, Any]:
        """Create one note for an exact assistant operation through the source owner."""
        if (not isinstance(creative_type, str)
                or creative_type not in {"concept", "character", "setting", "relationship", "item",
                                         "plot_decision", "theme", "other"}):
            raise ValueError("creative note type is invalid")
        if not isinstance(decision_status, str) or decision_status not in {"confirmed", "proposal"}:
            raise ValueError("creative note decision status is invalid")
        if (not isinstance(operation_origin, dict) or operation_origin.get("kind") != "assistant_note_edit"
                or any(not isinstance(operation_origin.get(field), str) or not operation_origin[field].strip()
                       for field in ("conversation_id", "assistant_message_id"))):
            raise ValueError("creative note creation origin is invalid")
        result = self.add_text_source(
            title=title, content=content, space="writing", work_id=work_id,
            # Source identity is tied to the durable logical operation, so title
            # reuse cannot overwrite another note or turn a retry into a new note.
            source_uri=f"creative-note://{work_id}/{idempotency_key}", folder_id=folder_id,
            document_metadata={"schema_version": 1, "kind": "creative_note",
                               "creative_type": creative_type, "decision_status": decision_status,
                               "summary": "", "references": []},
            idempotency_key=idempotency_key, operation_origin=operation_origin,
        )
        return self._creative_note_save_result(result)

    def retire_creative_note(
        self, source_id: str, *, work_id: str, base_edit_revision: int,
        idempotency_key: str, operation_origin: dict[str, Any],
    ) -> dict[str, Any]:
        """Soft-delete one read note with an exact, retractable operation receipt."""
        return self.repository.retire_creative_note(
            source_id, work_id=work_id, base_edit_revision=base_edit_revision,
            idempotency_key=idempotency_key, operation_origin=operation_origin,
        )

    def _creative_note_save_result(self, result: SourceImportResult) -> dict[str, Any]:
        saved_record = None
        current_revision = None
        is_current = False
        if result.edit_revision is not None:
            saved = self.get_source(result.source_id, record_revision=result.edit_revision, include_evidence=False)
            if saved is not None:
                saved_record = {
                    "edit_revision": result.edit_revision, "snapshot_id": result.snapshot_id,
                    "content_hash": result.content_hash, "title": saved["title"],
                    "content": saved["snapshot"]["content"], "document_metadata": saved["document_metadata"],
                }
                current_revision = saved["edit_revision"]
                is_current = saved["is_current_record"]
        return {"source_id": result.source_id, "saved_record": saved_record,
                "current_edit_revision": current_revision, "is_current": is_current}

    def rollback_assistant_capture_candidates(
        self,
        *,
        conversation_id: str,
        work_id: str,
        superseded_message_refs: list[dict[str, Any]],
        legacy_idempotency_keys: list[str] | None = None,
    ) -> dict[str, Any]:
        return self.repository.rollback_assistant_capture_candidates(
            conversation_id=conversation_id,
            work_id=work_id,
            superseded_message_refs=superseded_message_refs,
            legacy_idempotency_keys=legacy_idempotency_keys or [],
        )

    def preflight_rollback_assistant_capture_candidates(
        self,
        *,
        conversation_id: str,
        work_id: str,
        superseded_message_refs: list[dict[str, Any]],
        legacy_idempotency_keys: list[str] | None = None,
    ) -> dict[str, Any]:
        return self.repository.preflight_rollback_assistant_capture_candidates(
            conversation_id=conversation_id,
            work_id=work_id,
            superseded_message_refs=superseded_message_refs,
            legacy_idempotency_keys=legacy_idempotency_keys or [],
        )

    def find_source_idempotency_keys(
        self,
        idempotency_keys: list[str],
        *,
        space: str,
        work_id: str | None,
    ) -> set[str]:
        return self.repository.find_source_idempotency_keys(
            idempotency_keys,
            space=space,
            work_id=work_id,
        )

    def rename_source(self, source_id: str, *, title: str) -> dict[str, str] | None:
        return self.repository.rename_source(source_id, title=title)

    def list_projects(self, space: str | None = None) -> list[dict[str, Any]]:
        return self.repository.list_projects(space)

    def get_learning_preference(self, work_id=None, *, revision=None):
        return self.repository.get_learning_preference(work_id, revision=revision)

    def replace_learning_preference(self, *, libraries, expected_revision, work_id=None):
        return self.repository.replace_learning_preference(
            libraries=libraries, expected_revision=expected_revision, work_id=work_id)

    def work_learning_preference_context(self, work_id, *, use):
        return self.repository.work_learning_preference_context(work_id, use=use)

    def is_project_active(self, project_id: str, *, space: str) -> bool:
        return self.repository.is_project_active(project_id, space=space)

    def create_project(
        self, *, space: str, title: str, description: str = ""
    ) -> dict[str, Any]:
        return self.repository.create_project(
            space=space, title=title, description=description
        )

    def update_project(
        self,
        project_id: str,
        *,
        space: str,
        title: str,
        description: str,
    ) -> dict[str, Any] | None:
        return self.repository.update_project(
            project_id, space=space, title=title, description=description
        )

    def delete_project(self, project_id: str, *, space: str) -> bool:
        return self.repository.delete_project(project_id, space=space)

    def list_folders(self, space: str, work_id: str | None) -> list[dict[str, Any]]:
        return self.repository.list_folders(space, work_id)

    def list_folder_page(
        self,
        space: str,
        work_id: str | None,
        *,
        parent_folder_id: str | None,
        limit: int,
        offset: int,
        plan_section_folders: dict[str, str | None] | None = None,
    ) -> dict[str, Any]:
        return self.repository.list_folder_page(
            space,
            work_id,
            parent_folder_id=parent_folder_id,
            limit=limit,
            offset=offset,
            plan_section_folders=plan_section_folders,
        )

    def create_folder(
        self,
        *,
        space: str,
        work_id: str | None,
        parent_folder_id: str | None,
        name: str,
    ) -> dict[str, Any]:
        return self.repository.create_folder(
            space=space,
            work_id=work_id,
            parent_folder_id=parent_folder_id,
            name=name,
        )

    def rename_folder(self, folder_id: str, *, name: str) -> dict[str, Any] | None:
        return self.repository.rename_folder(folder_id, name=name)

    def move_folder(
        self, folder_id: str, *, parent_folder_id: str | None
    ) -> dict[str, Any] | None:
        return self.repository.move_folder(folder_id, parent_folder_id=parent_folder_id)

    def move_sources(self, source_ids: list[str], *, folder_id: str | None) -> int:
        return self.repository.move_sources(source_ids, folder_id=folder_id)

    def delete_folder(self, folder_id: str) -> bool:
        return self.repository.delete_folder(folder_id)

    def list_sources(
        self,
        space: str,
        work_id: str | None,
        folder_id: str | None = None,
        *,
        source_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        return self.repository.list_sources(space, work_id, folder_id, source_ids=source_ids)

    def list_source_summaries_by_ids(
        self, space: str, work_id: str | None, source_ids: list[str]
    ) -> list[dict[str, Any]]:
        selected_ids = list(dict.fromkeys(source_ids))
        if len(selected_ids) > 10_000:
            raise ValueError("a source summary request is limited to 10,000 sources")
        items: list[dict[str, Any]] = []
        for offset in range(0, len(selected_ids), 500):
            items.extend(
                self.repository.list_sources(
                    space,
                    work_id,
                    source_ids=selected_ids[offset : offset + 500],
                )
            )
        order = {source_id: index for index, source_id in enumerate(selected_ids)}
        items.sort(key=lambda item: order[str(item["id"])])
        return items

    def preview_source_ownership_transfer(
        self,
        source_space: str,
        source_work_id: str | None,
        target_work_id: str,
        source_ids: list[str],
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        return self.repository.preview_source_ownership_transfer(
            source_space,
            source_work_id,
            target_work_id,
            source_ids,
            limit=limit,
            offset=offset,
        )

    def transfer_source_ownership(
        self,
        *,
        operation_id: str,
        source_space: str,
        source_work_id: str | None,
        target_work_id: str,
        source_ids: list[str],
        expected_fingerprint: str,
    ) -> dict[str, Any]:
        return self.repository.transfer_source_ownership(
            operation_id=operation_id,
            source_space=source_space,
            source_work_id=source_work_id,
            target_work_id=target_work_id,
            source_ids=source_ids,
            expected_fingerprint=expected_fingerprint,
        )

    def get_source_ownership_transfer(
        self, operation_id: str
    ) -> dict[str, Any] | None:
        return self.repository.get_source_ownership_transfer(operation_id)

    def list_source_page(
        self,
        space: str,
        work_id: str | None,
        *,
        folder_id: str | None,
        direct_folder_only: bool,
        query: str,
        parse_status: str,
        sort: str,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        return self.repository.list_source_page(
            space,
            work_id,
            folder_id=folder_id,
            direct_folder_only=direct_folder_only,
            query=query,
            parse_status=parse_status,
            sort=sort,
            limit=limit,
            offset=offset,
        )

    def count_sources(self, space: str, work_id: str | None) -> int:
        return self.repository.count_sources(space, work_id)

    def list_source_selection_index(
        self,
        space: str,
        work_id: str | None,
        *,
        folder_id: str | None = None,
        limit: int = 10_000,
    ) -> list[dict[str, Any]]:
        return self.repository.list_source_selection_index(
            space,
            work_id,
            folder_id=folder_id,
            limit=limit,
        )

    def get_source_summaries(
        self,
        space: str,
        work_id: str | None,
        *,
        source_ids: list[str] | None = None,
        folder_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        return self.repository.get_source_summaries(
            space,
            work_id,
            source_ids=source_ids,
            folder_ids=folder_ids,
        )

    def list_source_sections_for_snapshots(
        self, source_snapshots: dict[str, str]
    ) -> list[dict[str, Any]]:
        return self.repository.list_source_sections_for_snapshots(source_snapshots)

    def list_source_sections(
        self, source_id: str, section_ids: list[str] | None = None
    ) -> list[dict[str, Any]]:
        return self.repository.list_source_sections(source_id, section_ids)

    def get_source_sections(
        self,
        source_id: str,
        section_ids: list[str],
        *,
        snapshot_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.repository.get_source_sections(
            source_id, section_ids, snapshot_id=snapshot_id
        )

    def list_learning_source_candidates(self) -> list[dict[str, Any]]:
        return self.repository.list_learning_source_candidates()

    def get_source(
        self,
        source_id: str,
        snapshot_id: str | None = None,
        *,
        record_revision: int | None = None,
        content_offset: int | None = None,
        content_limit: int | None = None,
        include_evidence: bool = True,
    ) -> dict[str, Any] | None:
        return self.repository.get_source(
            source_id,
            snapshot_id,
            record_revision=record_revision,
            content_offset=content_offset,
            content_limit=content_limit,
            include_evidence=include_evidence,
        )

    def delete_source(self, source_id: str, *, expected_edit_revision: int | None = None) -> bool:
        return self.repository.delete_source(source_id, expected_edit_revision=expected_edit_revision)

    def list_work_learning_references(self, work_id: str) -> dict[str, Any]:
        return self.repository.list_work_learning_references(work_id)

    def replace_work_learning_references(
        self,
        work_id: str,
        source_ids: list[str],
        scopes: list[dict[str, str | None]] | None = None,
    ) -> dict[str, Any]:
        return self.repository.replace_work_learning_references(work_id, source_ids, scopes)

    def list_work_base_reference(self, work_id: str) -> dict[str, Any] | None:
        return self.repository.list_work_base_reference(work_id)

    def replace_work_base_reference(
        self,
        work_id: str,
        *,
        base_work_id: str | None,
        base_branch_id: str | None,
        source_ids: list[str],
        scope_folder_ids: list[str | None] | None = None,
        space: str = "writing",
    ) -> dict[str, Any] | None:
        branch_id = base_branch_id.strip() if base_branch_id is not None else None
        if space == "writing" and base_work_id is not None:
            if branch_id is None:
                raise ValueError("writing base references require a branch")
            validate_temporal_branch_id(branch_id)
        elif space == "learning" and branch_id is not None:
            raise ValueError("learning base references do not use a branch")
        return self.repository.replace_work_base_reference(
            work_id,
            base_work_id=base_work_id,
            base_branch_id=branch_id,
            space=space,
            source_ids=source_ids,
            scope_folder_ids=scope_folder_ids,
        )

