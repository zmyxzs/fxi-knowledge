"""Knowledge-owner access and writes for qualified portable writing methods."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Iterable, Literal
from pydantic import ValidationError

from .repository import KnowledgeItemVersionConflictError
from .writing_methods import (
    ModelGeneratedWritingMethodTransferProbe,
    ModelGeneratedWritingMethodTransferProbeDraft,
    PersonalWritingMethodQualification,
    PinnedWritingMethodTransferEvidence,
    StoredWritingStyleMethod,
    StoredWritingStyleMethodPayload,
    personal_method_study_scope,
    writing_method_content_hash,
    writing_method_evidence_hash,
    writing_method_evidence_pins,
    writing_method_is_personally_reusable,
    writing_method_transfer_probe_draft_hash,
    writing_method_transfer_probe_hash,
    writing_method_qualification_is_current,
)


PersonalWritingStage = Literal["planning", "outline", "prose", "revision"]
PersonalWritingMethodFeedbackReader = Callable[[Sequence[Mapping[str, str]]], dict[str, Any]]


class KnowledgeWritingMethodServiceMixin:
    """Bounded method-only capability over existing source-study records."""

    def resolve_planning_reading_requirements(self, requirements, **scope):
        from .planning_required_reading import resolve_required_reading
        with self.repository.database.read_snapshot():
            return resolve_required_reading(self, requirements, **scope)

    async def search_planning_cases(self, space: str, work_id: str | None, query: str, **kwargs: Any) -> dict[str, Any]:
        """Read original planning case headers without requiring synthesized methods."""
        from .planning_cases import search_planning_cases_hybrid
        return await search_planning_cases_hybrid(self, space, work_id, query, **kwargs)

    def read_planning_cases(self, space: str, work_id: str | None, case_ids: list[str], **kwargs: Any) -> dict[str, Any]:
        """Expand exact cases through the same current scope and source owners."""
        from .planning_cases import read_planning_cases
        with self.repository.database.read_snapshot():
            return read_planning_cases(self, space, work_id, case_ids, **kwargs)

    def pin_writing_style_excerpt(
        self, source_id: str, snapshot_id: str, *, content_hash: str,
        start_offset: int, text: str,
    ) -> dict[str, Any]:
        """Keep the original project's complete exemplars in existing evidence storage."""
        return self.repository.pin_source_excerpt(
            source_id, snapshot_id, content_hash=content_hash,
            start_offset=start_offset, text=text,
        )

    def set_personal_writing_method_feedback_reader(
        self, reader: PersonalWritingMethodFeedbackReader,
    ) -> None:
        """Attach the read-only prose-owner port used by the method detail view."""
        if not callable(reader):
            raise TypeError("personal writing-method feedback reader must be callable")
        self._personal_writing_method_feedback_reader = reader

    def read_personal_writing_method_feedback(
        self,
        method_pins: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Read cross-work feedback only for exact, still-qualified method pins."""
        if not isinstance(method_pins, Sequence) or isinstance(method_pins, (str, bytes)):
            raise ValueError("method feedback requires a sequence of exact method pins")
        if not 1 <= len(method_pins) <= 3:
            raise ValueError("method feedback reads are limited to 1–3 methods")
        method_ids: list[str] = []
        expected_versions: dict[str, int] = {}
        expected_hashes: dict[str, str] = {}
        for pin in method_pins:
            if not isinstance(pin, Mapping):
                raise ValueError("method feedback pins must be objects")
            method_id = pin.get("method_id")
            version = pin.get("learning_item_version")
            method_hash = pin.get("method_sha256")
            if (
                not isinstance(method_id, str) or not method_id.strip()
                or type(version) is not int or version < 1
                or not isinstance(method_hash, str) or len(method_hash) != 64
                or any(character not in "0123456789abcdef" for character in method_hash)
                or method_id in expected_versions
            ):
                raise ValueError("method feedback pins must contain unique exact method identities")
            method_ids.append(method_id)
            expected_versions[method_id] = version
            expected_hashes[method_id] = method_hash

        reader = getattr(self, "_personal_writing_method_feedback_reader", None)
        if not callable(reader):
            raise RuntimeError("personal writing-method feedback reader is not configured")

        current = self.read_personal_writing_methods(
            method_ids, expected_versions=expected_versions,
        )
        current_by_id = {str(method["method_id"]): method for method in current["methods"]}
        if any(
            current_by_id[method_id].get("method_sha256") != expected_hashes[method_id]
            for method_id in method_ids
        ):
            raise KnowledgeItemVersionConflictError(
                "a selected personal writing method changed before feedback was read"
            )
        result = reader([
            {"method_id": method_id, "method_sha256": expected_hashes[method_id]}
            for method_id in method_ids
        ])
        if not isinstance(result, dict) or result.get("status") != "completed":
            raise RuntimeError("personal writing-method feedback reader returned an invalid result")
        returned = result.get("methods")
        if (
            not isinstance(returned, list)
            or len(returned) != len(method_ids)
            or {row.get("method_id") for row in returned if isinstance(row, Mapping)} != set(method_ids)
            or any(
                not isinstance(row, Mapping)
                or row.get("method_sha256") != expected_hashes.get(str(row.get("method_id")))
                for row in returned
            )
        ):
            raise RuntimeError("personal writing-method feedback does not match the requested method pins")
        return result

    def create_knowledge_item(self, **kwargs: Any) -> dict[str, Any]:
        """Prevent callers from minting model-review qualifications in a new card."""
        content = kwargs.get("content")
        if (
            isinstance(content, dict)
            and content.get("analysis_type") == "style"
            and content.get("learning_role") == "craft_method_synthesis"
        ):
            try:
                payload = StoredWritingStyleMethodPayload.model_validate(
                    content.get("craft_method_payload")
                )
            except ValidationError as exc:
                raise ValueError(f"invalid stored style-method payload: {exc}") from exc
            if any(method.reuse_qualification is not None for method in payload.methods):
                raise ValueError(
                    "a writing method cannot be qualified when its study card is created"
                )
        return super().create_knowledge_item(**kwargs)

    def search_personal_writing_methods(
        self,
        query: str,
        *,
        stage: PersonalWritingStage,
        limit: int = 6,
    ) -> dict[str, Any]:
        normalized = query.strip()
        if not normalized or len(normalized) > 500:
            raise ValueError("personal writing-method query must contain 1–500 characters")
        if stage not in {"planning", "outline", "prose", "revision"}:
            raise ValueError("personal writing-method stage is invalid")
        if not 1 <= limit <= 6:
            raise ValueError("personal writing-method results are limited to 6 candidates")

        rows = self.repository.search_personal_writing_methods(normalized, candidate_limit=24)
        candidates: list[dict[str, Any]] = []
        for row in rows:
            item = self.get_knowledge_item(row["item_id"])
            if (
                item is None
                or not personal_method_study_scope(item)
                or item.get("status") not in {"draft", "reviewed"}
                or str(item.get("version_id")) != row["version_id"]
                or int(item.get("version", -1)) != row["version"]
                or item.get("kind") != "source_study"
                or not isinstance(item.get("content"), dict)
                or item["content"].get("analysis_type") != "style"
                or item["content"].get("learning_role") != "craft_method_synthesis"
            ):
                continue
            try:
                payload = StoredWritingStyleMethodPayload.model_validate(
                    item["content"].get("craft_method_payload")
                )
            except ValidationError:
                continue
            method = next((entry for entry in payload.methods if entry.method_id == row["method_id"]), None)
            if method is None or not writing_method_is_personally_reusable(method):
                continue
            basis = self.style_study_basis_status(item["id"], expected_version=item["version"])
            if (basis == "changed"
                    or not self._method_source_pins_are_readable(item, method)):
                continue
            candidates.append(
                {**self._personal_method_candidate(
                    method=method,
                    item=item,
                    score=row["score"],
                ), "study_basis_status": basis}
            )
            if len(candidates) >= limit:
                break
        return {
            "status": "completed",
            "stage": stage,
            "query": normalized,
            "candidates": candidates,
            "candidate_count": len(candidates),
        }

    @staticmethod
    def _personal_method_candidate(
        *, method: StoredWritingStyleMethod,
        item: dict[str, Any],
        score: float,
    ) -> dict[str, Any]:
        qualification = method.reuse_qualification
        assert qualification is not None
        qualification_value = {
            **qualification.model_dump(mode="json"),
            "induction_status": {
                "pass": "passed", "fail": "failed", "insufficient": "insufficient"
            }[qualification.induction.decision],
            "transfer_status": {
                "pass": "passed", "fail": "failed", "insufficient": "insufficient"
            }[qualification.transfer.decision],
        }
        pins_by_identity = {
            (pin["evidence_id"], pin["start_offset"], pin["end_offset"]): pin
            for pin in writing_method_evidence_pins(method)
        }
        coverage_by_identity = {
            (row.evidence_id, row.start_offset, row.end_offset): row
            for reference_group in (
                method.coverage.support,
                method.coverage.counterexamples,
                method.coverage.context,
            )
            for row in reference_group
        }
        source_refs = {
            (str(row.get("source_id")), str(row.get("snapshot_id"))): row
            for row in item.get("content", {}).get("source_refs", [])
            if isinstance(row, dict)
        }
        read_examples: list[dict[str, Any]] = []
        for example in method.examples:
            pin = pins_by_identity[(example.evidence_id, example.start_offset, example.end_offset)]
            coverage = coverage_by_identity[(example.evidence_id, example.start_offset, example.end_offset)]
            source_ref = source_refs.get((pin["source_id"], pin["snapshot_id"]), {})
            read_examples.append({
                "evidence_id": example.evidence_id,
                "role": example.role,
                "material_kind": example.material_kind,
                "voice": example.voice,
                "explanation": example.explanation,
                # Control characters are JSON-escaped as six code points, giving
                # a safe bound without loading text for all six candidates.
                "text": "\u0000" * (example.end_offset - example.start_offset),
                "source_id": pin["source_id"],
                "snapshot_id": pin["snapshot_id"],
                "content_hash": pin["content_hash"],
                "start_offset": pin["start_offset"],
                "end_offset": pin["end_offset"],
                "source_title": source_ref.get("title"),
                "study_item_ids": list(coverage.study_item_ids),
                "input_role": coverage.input_role,
            })
        full_method = {
            **method.model_dump(mode="json", exclude={"reuse_qualification"}),
            "method_sha256": writing_method_content_hash(method),
            "learning_item_id": str(item["id"]),
            "learning_item_version": int(item["version"]),
            "learning_item_version_id": str(item["version_id"]),
            "reuse_scope": "personal",
            "origin_scope": {"space": item.get("space"), "work_id": item.get("work_id")},
            "qualification_status": qualification.status,
            "qualification": qualification_value,
            "examples": read_examples,
        }
        read_payload = {"status": "completed", "methods": [full_method], "method_count": 1}
        return {
                    "method_id": method.method_id,
                    "learning_item_id": str(item["id"]),
                    "learning_item_version": int(item["version"]),
                    "method_sha256": writing_method_content_hash(method),
                    "title": method.title,
                    "aspect": method.aspect,
                    "technique": method.technique,
                    "use_when": method.use_when,
                    "avoid_when": method.avoid_when,
                    "scale": method.scale,
                    "reuse_scope": "personal",
                    "origin_scope": {"space": item.get("space"), "work_id": item.get("work_id")},
                    "qualification_status": "qualified",
                    "qualification": {
                        "status": "qualified",
                        "reuse_scope": "personal",
                        "induction_status": "passed",
                        "transfer_status": "passed",
                    },
                    "evidence_ids": list(method.evidence_ids),
                    "read_characters_upper_bound": len(json.dumps(
                        read_payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
                    )),
                    "score": score,
        }

    def read_personal_writing_methods(
        self,
        method_ids: list[str],
        *,
        expected_versions: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        normalized_ids = list(dict.fromkeys(method_ids))
        if not normalized_ids or len(normalized_ids) > 3:
            raise ValueError("personal writing-method reads require 1–3 exact method IDs")
        if any(not isinstance(value, str) or not value.strip() for value in normalized_ids):
            raise ValueError("personal writing-method IDs must be non-empty strings")
        expected_versions = expected_versions or {}
        if set(expected_versions) - set(normalized_ids):
            raise ValueError("expected item versions may only bind selected method IDs")

        found: dict[str, tuple[dict[str, Any], StoredWritingStyleMethod, str]] = {}
        for row in self.repository.get_personal_writing_method_records(normalized_ids):
            if row["method"]["method_id"] not in normalized_ids:
                continue
            item = self.get_knowledge_item(row["item_id"])
            if (
                item is None
                or not personal_method_study_scope(item)
                or item.get("status") not in {"draft", "reviewed"}
                or str(item.get("version_id")) != row["version_id"]
                or int(item.get("version", -1)) != row["version"]
                or item.get("kind") != "source_study"
                or not isinstance(item.get("content"), dict)
            ):
                continue
            if (
                item["content"].get("analysis_type") != "style"
                or item["content"].get("learning_role") != "craft_method_synthesis"
            ):
                continue
            try:
                payload = StoredWritingStyleMethodPayload.model_validate(
                    item["content"].get("craft_method_payload")
                )
            except ValidationError:
                continue
            method_id = str(row["method"]["method_id"])
            method = next((entry for entry in payload.methods if entry.method_id == method_id), None)
            if method is None or not writing_method_is_personally_reusable(method):
                continue
            if expected_versions.get(method.method_id) not in (None, int(item["version"])):
                raise KnowledgeItemVersionConflictError(
                    "a selected personal writing method changed after search"
                )
            basis = self.style_study_basis_status(item["id"], expected_version=item["version"])
            if (basis == "changed"
                    or not self._method_source_pins_are_readable(item, method)):
                continue
            found[method.method_id] = (item, method, basis)
        if set(found) != set(normalized_ids):
            raise ValueError("one or more selected methods are no longer qualified, current, or readable")

        methods: list[dict[str, Any]] = []
        for method_id in normalized_ids:
            item, method, basis = found[method_id]
            examples = self._read_method_examples(item, method)
            qualification = method.reuse_qualification
            assert qualification is not None
            methods.append(
                {
                    **method.model_dump(mode="json", exclude={"reuse_qualification"}),
                    "method_sha256": writing_method_content_hash(method),
                    "learning_item_id": str(item["id"]),
                    "learning_item_version": int(item["version"]),
                    "learning_item_version_id": str(item["version_id"]),
                    "study_basis_status": basis,
                    "reuse_scope": qualification.reuse_scope,
                    "origin_scope": {"space": item.get("space"), "work_id": item.get("work_id")},
                    "qualification_status": qualification.status,
                    "qualification": {
                        **qualification.model_dump(mode="json"),
                        "induction_status": {
                            "pass": "passed", "fail": "failed", "insufficient": "insufficient"
                        }[qualification.induction.decision],
                        "transfer_status": {
                            "pass": "passed", "fail": "failed", "insufficient": "insufficient"
                        }[qualification.transfer.decision],
                    },
                    "examples": examples,
                }
            )
        result = {"status": "completed", "methods": methods, "method_count": len(methods)}
        result["read_characters"] = len(
            json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        )
        return result

    def planning_method_catalog(
        self, space: str, work_id: str | None, *, source_ids: list[str] | None = None,
        folder_ids: list[str] | None = None, include_linked_scopes: bool = True,
    ) -> dict[str, Any]:
        """List current planning identities in the same explicit knowledge scope."""
        studies = {}
        for owner_space, owner, registered_ids, folders in self._retrieval._search_scopes(
            space, work_id, source_ids, folder_ids, include_linked_scopes,
        ):
            for row in self.repository.list_current_planning_studies(
                owner_space, owner, source_ids=registered_ids, folder_ids=folders,
            ):
                studies[row["item_id"]] = row
        # Independent questions studied against the same chapters remain distinct.
        # Current-version selection is per item; method IDs deduplicate exact repeats.
        return {"studies": sorted(studies.values(),
                key=lambda row: (row["updated_at"], row["item_id"]), reverse=True)}

    def planning_method_study_basis_is_current(self, item_id: str, *, expected_version: int) -> bool:
        """Read current-use eligibility without modifying method or study history."""
        return self.repository.planning_method_study_basis_is_current(item_id, expected_version)

    def style_study_basis_status(self, item_id: str, *, expected_version: int) -> str:
        return self.repository.style_study_basis_status(item_id, expected_version)

    def read_scoped_style_examples(
        self, item_id: str, *, expected_version: int, profile_id: str,
        work_id: str, example_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Read source-only examples through the knowledge owner, never as methods."""
        from .style_examples import read_style_examples
        with self.repository.database.read_snapshot():
            item = self.get_knowledge_item(item_id)
            content = (item or {}).get("content") or {}
            if (item is None or item.get("version") != expected_version
                    or item.get("status") not in {"draft", "reviewed"}
                    or item.get("kind") != "source_study" or content.get("analysis_type") != "style"
                    or content.get("learning_role") not in {"craft_method_synthesis", "craft_observations"}):
                raise KnowledgeItemVersionConflictError("the selected style examples changed or are unavailable")
            row = read_style_examples(self, item, profile_id=profile_id, work_id=work_id, example_ids=example_ids)
            directory = self.work_style_profile_catalog(work_id)
            selected = next((profile for profile in directory["profiles"]
                             if profile["item_id"] == item_id and profile["version"] == expected_version
                             and profile["profile"]["profile_id"] == profile_id), None)
            if selected is not None:
                row["style_profile_identity"] = {
                    "owner": "KnowledgeService.work_style_profile_catalog", "work_id": work_id,
                    "learning_item_id": item_id, "learning_item_version": expected_version,
                    "profile_id": profile_id, "source_ids": list(selected["profile"]["source_ids"]),
                    "resolved_source_names": list(selected["resolved_source_names"]),
                    "current_author_profile_count": sum(bool(profile["author_profile"]) for profile in directory["profiles"]),
                }
            return {"status": "completed", "references": [row]}

    async def search_work_style_examples(
        self, work_id: str, query: str = "", *,
        source_ids: list[str] | None = None, folder_ids: list[str] | None = None,
        profile_ids: list[str] | None = None, limit: int = 10, cursor: str | None = None,
    ) -> dict[str, Any]:
        """Locate registered style passages without loading their complete prose."""
        from .style_example_candidates import search_work_style_examples
        return await search_work_style_examples(
            self, work_id, query, source_ids=source_ids, folder_ids=folder_ids,
            profile_ids=profile_ids, limit=limit, cursor=cursor,
        )

    def search_work_prose_sources(self, work_id: str, query: str, *,
                                  learning_work_ids=None, source_ids=None) -> dict[str, Any]:
        from .style_example_candidates import search_work_prose_sources
        return search_work_prose_sources(self, work_id, query,
            learning_work_ids=learning_work_ids, source_ids=source_ids)

    def read_work_prose_sources(self, work_id: str, candidates, *, scope, source_ids=None) -> dict[str, Any]:
        from .style_example_candidates import read_work_prose_sources
        return read_work_prose_sources(self, work_id, candidates, scope=scope, source_ids=source_ids)

    def validate_work_prose_source_candidates(self, work_id: str, candidates, *, scope, source_ids=None) -> None:
        from .style_example_candidates import validate_work_prose_source_candidates
        validate_work_prose_source_candidates(self, work_id, candidates, scope=scope, source_ids=source_ids)

    def work_style_profile_catalog(
        self, work_id: str, *, author_quote: str | None = None,
        source_ids: list[str] | None = None, folder_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Resolve current registered study identities; examples are read on selection."""
        names = list(dict.fromkeys(re.findall(r"《([^》]+)》", author_quote or "")))
        if author_quote is not None and (not names or len(names) > 3):
            raise ValueError("a named style read requires 1–3 explicit source titles")
        selected: dict[str | tuple[str, ...], dict[str, Any]] = {}
        scopes = self._retrieval._search_scopes("writing", work_id, source_ids, folder_ids, True)
        candidates = {}
        folder_titles_by_source: dict[str, set[str]] = {}
        project_titles_by_source: dict[str, str] = {}
        active_project_titles: dict[str, dict[str, str]] = {}
        for space, owner, registered_ids, folders in scopes:
            if space != "learning" and (space, owner) != ("writing", work_id):
                continue
            rows = self.repository.list_current_style_profiles(
                space, owner, source_ids=registered_ids, folder_ids=folders,
            )
            from .style_examples import example_bindings
            rows = [row for row in rows if row["profile_kind"] != "examples"
                    or example_bindings(self.get_knowledge_item(row["item_id"]))]
            for row in rows:
                candidates[row["item_id"]] = row
            if any(row["author_profile"] for row in rows):
                # The profile query already checks every source against this
                # registered scope and its current snapshot. Resolve only those
                # IDs through the owner's current directory, never a study's
                # cached folder reference or the model's author alias.
                profile_source_ids = list(dict.fromkeys(
                    source_id for row in rows if row["author_profile"]
                    for source_id in row["profile"]["source_ids"]
                ))
                if profile_source_ids:
                    paths = {folder["id"]: folder["path"]
                             for folder in self.repository.list_folders(space, owner)}
                    source_summaries = []
                    for offset in range(0, len(profile_source_ids), 2000):
                        source_summaries.extend(self.repository.get_source_summaries(
                            space, owner, source_ids=profile_source_ids[offset:offset + 2000],
                        ))
                    if space not in active_project_titles:
                        active_project_titles[space] = {
                            str(project["id"]): project["title"]
                            for project in self.repository.list_projects(space)
                        }
                    projects = active_project_titles[space]
                    for source in source_summaries:
                        source_id = source["id"]
                        path = paths.get(source.get("folder_id"), "")
                        folder_titles_by_source[source_id] = {
                            component[1:-1] if re.fullmatch(r"《[^》]+》", component) else component
                            for component in path.split("/") if component
                        }
                        # A project title is a current source identity only when
                        # the source itself belongs to that active project. A
                        # logical learning scope or folder link cannot supply it.
                        source_work_id = source.get("work_id")
                        project_title = projects.get(str(source_work_id)) if source_work_id else None
                        if project_title:
                            normalized = (project_title[1:-1]
                                          if re.fullmatch(r"《[^》]+》", project_title) else project_title)
                            project_titles_by_source[source_id] = normalized
        for row in sorted(candidates.values(), key=lambda row: (
            row["study_basis_status"] == "current", row["updated_at"], row["item_id"]), reverse=True):
            source_titles = [folder_titles_by_source.get(source_id, set()) | {
                title for ref in row["source_refs"] if ref.get("source_id") == source_id
                for title in re.findall(r"《([^》]+)》", str(ref.get("title") or ""))
            } | ({project_titles_by_source[source_id]} if source_id in project_titles_by_source else set())
                for source_id in row["profile"]["source_ids"]]
            titles = set.intersection(*source_titles) if source_titles else set()
            row = {**row, "resolved_source_names": sorted(titles)}
            if names:
                if not row["author_profile"]:
                    continue
                # A chapter-only corpus belongs to a named book only when every
                # covered source proves that identity; one directory cannot name
                # a profile that mixes chapters from different books.
                for name in names:
                    if name in titles:
                        selected.setdefault(name, row)
            else:
                # Ordinary operations and author-style observations are distinct
                # capabilities even when they cover the same source corpus.
                identity = ("author_style" if row["author_profile"] else "style_methods",
                            *sorted(row["profile"]["source_ids"]))
                selected.setdefault(identity, row)
        profiles = {row["item_id"]: row for row in selected.values()}
        return {"status": "completed" if profiles else "unavailable",
                "profiles": list(profiles.values()), "requested_sources": names,
                "matched_sources": list(selected) if names else []}

    def read_work_author_style_methods(
        self, work_id: str, *, author_quote: str,
    ) -> dict[str, Any]:
        """Read the named current profile through the same identity resolver."""
        catalog = self.work_style_profile_catalog(work_id, author_quote=author_quote)
        rows = []
        for item in catalog["profiles"]:
            if item["profile_kind"] != "methods":
                continue
            rows.extend(self.read_scoped_writing_style_methods(
                item["item_id"], expected_version=item["version"],
                profile_id=item["profile"]["profile_id"], work_id=work_id,
            )["methods"])
        return {"status": "completed" if rows else "unavailable", "methods": rows,
                "requested_sources": catalog["requested_sources"],
                "matched_sources": catalog["matched_sources"]}

    def _work_method_source_ids(self, work_id: str, source_ids: set[str]) -> set[str]:
        """The same direct registered-source boundary for both method families."""
        permitted = set()
        for space, owner, registered_ids, folders in self._retrieval._search_scopes(
            "writing", work_id, None, None, True,
        ):
            if space != "learning" and (space, owner) != ("writing", work_id):
                continue
            selected_ids = list(source_ids if registered_ids is None else source_ids.intersection(registered_ids))
            if selected_ids:
                for folder in (folders or [None]):
                    permitted.update(row["id"] for row in self.repository.list_sources(
                        space, owner, folder, source_ids=selected_ids,
                    ))
        return permitted

    def read_scoped_writing_style_methods(
        self, item_id: str, *, expected_version: int, profile_id: str,
        method_ids: list[str] | None = None,
        work_id: str | None = None, example_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Read an expressly selected study profile with exact source examples.

        This read never grants personal qualification or retrieves work facts.
        Callers supply the item/profile pin authorized for their style task.
        """
        item = self.get_knowledge_item(item_id)
        if (item is None or item.get("version") != expected_version
                or item.get("status") not in {"draft", "reviewed"}
                or not self._is_style_method_item(item)):
            raise KnowledgeItemVersionConflictError("the selected style study changed or is unavailable")
        basis = self.style_study_basis_status(item_id, expected_version=expected_version)
        if basis == "changed":
            raise KnowledgeItemVersionConflictError("the style method's original study was revised or withdrawn")
        payload = StoredWritingStyleMethodPayload.model_validate(item["content"]["craft_method_payload"])
        if payload.profile.profile_id != profile_id:
            raise ValueError("the selected style profile does not belong to this study")
        if work_id is not None:
            source_ids = set(payload.profile.source_ids)
            permitted = self._work_method_source_ids(work_id, source_ids)
            if not source_ids.issubset(permitted):
                raise ValueError("the selected style sources are outside this work's registered references")
        ids = list(dict.fromkeys(method_ids if method_ids is not None else payload.profile.method_ids))
        if not ids or (method_ids is not None and (len(ids) > 24 or len(ids) != len(method_ids))):
            raise ValueError("a scoped style read must contain distinct methods from one exact profile")
        by_id = {method.method_id: method for method in payload.methods}
        if set(ids) - set(by_id):
            raise ValueError("the selected style method is outside the pinned profile")
        selected_examples = None if example_ids is None else set(example_ids)
        if selected_examples is not None:
            available = {example.evidence_id for identity in ids for example in by_id[identity].examples}
            if not selected_examples or len(selected_examples) != len(example_ids) or selected_examples - available:
                raise ValueError("selected style examples are outside the pinned methods")
            if any(not selected_examples.intersection(example.evidence_id for example in by_id[identity].examples)
                   for identity in ids):
                raise ValueError("each scoped style method needs a complete selected source example")
        rows = []
        profile_identity = None
        if work_id is not None and item["content"].get("author_style_fingerprint"):
            directory = self.work_style_profile_catalog(work_id)
            current_profile = next((row for row in directory["profiles"]
                                    if row["item_id"] == item_id and row["version"] == expected_version
                                    and row["profile"]["profile_id"] == profile_id), None)
            if current_profile is not None:
                profile_identity = {
                    "owner": "KnowledgeService.work_style_profile_catalog",
                    "work_id": work_id, "learning_item_id": item_id,
                    "learning_item_version": expected_version, "profile_id": profile_id,
                    "source_ids": list(payload.profile.source_ids),
                    "resolved_source_names": list(current_profile["resolved_source_names"]),
                    "current_author_profile_count": sum(bool(row["author_profile"])
                                                        for row in directory["profiles"]),
                }
        for identity in ids:
            method = by_id[identity]
            if not self._method_source_pins_are_readable(item, method):
                raise KnowledgeItemVersionConflictError("a selected style source changed or was withdrawn")
            examples = self._read_method_examples(item, method, example_ids=selected_examples)
            fingerprint = item["content"].get("author_style_fingerprint") or {}
            for example in examples:
                golden = next((row for row in fingerprint.get("golden_voice_slices", [])
                               if str(row.get("quote") or "").strip() == example["text"]), None)
                if golden is not None:
                    example.update({"is_golden": True, "title": golden["title"],
                                    "cadence_notes": golden.get("cadence_notes", "")})
            rows.append({
                **method.model_dump(mode="json", exclude={"reuse_qualification"}),
                "record_type": "writing_style_method",
                "method_sha256": writing_method_content_hash(method),
                "learning_item_id": item_id, "learning_item_version": expected_version,
                "candidate_status": item["status"],
                "learning_item_version_id": item["version_id"],
                "origin_scope": {"space": item.get("space"), "work_id": item.get("work_id")},
                "examples": examples,
                "style_profile_summary": fingerprint.get("summary", ""),
                "study_basis_status": basis,
                "style_profile_kind": "author_fingerprint" if fingerprint else "method_observations",
                "style_observations": ([
                    {key: deepcopy(operation[key]) for key in (
                        "aspect", "scale", "title", "observation", "technique", "use_when", "avoid_when",
                        "preserve", "observed_contexts", "effect", "limitations", "scope_assessment",
                        "source_specific_elements",
                    ) if key in operation}
                    for operation in fingerprint.get("operations", [])
                ] if fingerprint.get("operations") else [
                    {**{key: deepcopy(rule.get(key, [])) for key in (
                        "applicable_scenes", "forbidden_scenes", "applicable_phases", "exemptions",
                        "retrieval_tags", "evidence_refs", "anti_patterns",
                    )}, "dimension": dimension, "title": rule.get("rule_name") or rule.get("hook_type")
                         or rule.get("cadence_type") or rule.get("tactic") or rule.get("tension_tier"),
                     "technique": rule.get("mechanism") or rule.get("mental_model")
                         or rule.get("subtext_principle") or rule.get("verb_principle")}
                    for dimension in ("macro_opening", "cliffhanger_hooks", "voice_perception",
                                      "scene_cadence", "dialogue_subtext", "verb_engine")
                    for rule in fingerprint.get(dimension, [])
                ]) if not rows else [],
                "bone_verbs": list(fingerprint.get("bone_verbs", [])),
            })
        if example_ids is not None:
            selected = set(example_ids)
            available = {example["evidence_id"] for row in rows for example in row["examples"]}
            if not selected or len(selected) != len(example_ids) or selected - available:
                raise ValueError("selected style examples are outside the pinned methods")
            for row in rows:
                row["examples"] = [example for example in row["examples"]
                                   if example["evidence_id"] in selected]
                if not row["examples"]:
                    raise ValueError("each scoped style method needs a complete selected source example")
        if work_id is not None:
            for row in rows:
                row["reuse_scope"] = "source_bound"
                row["selected_example_ids"] = [example["evidence_id"] for example in row["examples"]]
                row["scoped_style_read"] = {
                    "work_id": work_id, "learning_item_id": item_id,
                    "learning_item_version": expected_version, "profile_id": profile_id,
                    "method_sha256": row["method_sha256"], "source_ids": list(payload.profile.source_ids),
                }
                row["scoped_method_read"] = deepcopy(row["scoped_style_read"])
                if profile_identity is not None:
                    row["style_profile_identity"] = deepcopy(profile_identity)
        return {"status": "completed", "methods": rows}

    def read_scoped_writing_methods(
        self, item_id: str, *, expected_version: int, method_ids: list[str],
        work_id: str, example_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Read an existing source-bound method for this work, without qualification.

        The item's actual payload selects the method family. Neither an adapter
        record type nor a readiness flag can grant source access.
        """
        from fxi_novel.learning.payloads import StoredPlanningMethodPayload

        if not isinstance(work_id, str) or not work_id.strip():
            raise ValueError("a scoped method read requires the current writing work")
        item = self.get_knowledge_item(item_id)
        if (item is None or item.get("version") != expected_version
                or item.get("status") not in {"draft", "reviewed"}):
            raise KnowledgeItemVersionConflictError("the selected source study changed or is unavailable")
        if self._is_style_method_item(item):
            profile = item["content"]["craft_method_payload"]["profile"]
            return self.read_scoped_writing_style_methods(
                item_id, expected_version=expected_version, profile_id=profile["profile_id"],
                method_ids=method_ids, work_id=work_id, example_ids=example_ids,
            )
        content = item.get("content") or {}
        if (item.get("kind") != "source_study" or content.get("analysis_type") != "planning"
                or content.get("learning_role") != "planning_method_synthesis"):
            raise ValueError("the selected study has no source-bound writing methods")
        if not self.planning_method_study_basis_is_current(item_id, expected_version=expected_version):
            raise KnowledgeItemVersionConflictError("the planning method's original study was revised or withdrawn")
        payload = StoredPlanningMethodPayload.model_validate(content.get("method_payload"))
        if (not method_ids or len(method_ids) > 3 or len(set(method_ids)) != len(method_ids)
                or set(method_ids) - {row.method_id for row in payload.methods}):
            raise ValueError("select distinct planning methods from the exact study")
        by_id = {row.method_id: row for row in payload.methods}
        evidence = {row["id"]: row for row in item.get("evidence", [])}
        source_refs = {(row.get("source_id"), row.get("snapshot_id")): row
                       for row in content.get("source_refs", []) if isinstance(row, Mapping)}
        required_sources = {evidence[evidence_id]["source_id"] for identity in method_ids
                            for evidence_id in by_id[identity].evidence_ids if evidence_id in evidence}
        permitted_sources = self._work_method_source_ids(work_id, required_sources)
        rows = []
        for identity in method_ids:
            method = by_id[identity]
            examples = []
            for evidence_id in method.evidence_ids:
                row = evidence.get(evidence_id)
                if row is None:
                    raise ValueError("the selected planning method has no complete persisted evidence")
                source_id, snapshot_id = row.get("source_id"), row.get("snapshot_id")
                source_ref = source_refs.get((source_id, snapshot_id))
                source = self.get_source(source_id, content_offset=0, content_limit=1, include_evidence=False)
                snapshot = (source or {}).get("snapshot") or {}
                if (source is None or source_ref is None
                        or (source.get("space"), source.get("work_id")) != (item.get("space"), item.get("work_id"))
                        or snapshot.get("id") != snapshot_id
                        or snapshot.get("content_hash") != source_ref.get("content_hash")):
                    raise KnowledgeItemVersionConflictError("a selected planning source changed or was withdrawn")
                if source_id not in permitted_sources:
                    raise ValueError("the selected planning sources are outside this work's registered references")
                start, end, quote = row.get("start_offset"), row.get("end_offset"), row.get("quote")
                if (type(start) is not int or type(end) is not int or start < 0 or end <= start
                        or not isinstance(quote, str) or not quote
                        or hashlib.sha256(quote.encode("utf-8")).hexdigest() != row.get("quote_hash")):
                    raise ValueError("the selected planning evidence has an invalid exact range or hash")
                exact = self.get_source(source_id, snapshot_id, content_offset=start,
                                        content_limit=end - start, include_evidence=False)
                if (exact is None or exact["snapshot"]["content"] != quote
                        or exact["snapshot"]["content_hash"] != source_ref["content_hash"]):
                    raise ValueError("the selected planning evidence does not match its source range")
                examples.append({"evidence_id": evidence_id, "role": "support", "text": quote,
                                 "source_id": source_id, "source_title": source.get("title"),
                                 "source_space": source.get("space"), "source_work_id": source.get("work_id"),
                                 "snapshot_id": snapshot_id, "content_hash": snapshot["content_hash"],
                                 "snapshot_content_hash": snapshot["content_hash"], "quote_hash": row["quote_hash"],
                                 "start_offset": start, "end_offset": end, "offset_unit": "unicode_codepoint"})
            rows.append({**method.model_dump(mode="json"), "record_type": "planning_method",
                         "reuse_scope": "source_bound", "candidate_status": item["status"],
                         "method_sha256": hashlib.sha256(json.dumps(method.model_dump(mode="json"),
                             ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest(),
                         "learning_item_id": item_id, "learning_item_version": expected_version,
                         "learning_item_version_id": item["version_id"],
                         "origin_scope": {"space": item.get("space"), "work_id": item.get("work_id")},
                         "examples": examples})
        if example_ids is not None:
            available = {example["evidence_id"] for row in rows for example in row["examples"]}
            if not example_ids or len(set(example_ids)) != len(example_ids) or set(example_ids) - available:
                raise ValueError("selected planning examples are outside the pinned methods")
            for row in rows:
                row["examples"] = [example for example in row["examples"] if example["evidence_id"] in example_ids]
                if not row["examples"]:
                    raise ValueError("each planning method needs complete selected evidence")
        for row in rows:
            row["selected_example_ids"] = [example["evidence_id"] for example in row["examples"]]
            row["scoped_method_read"] = {
                "work_id": work_id, "learning_item_id": item_id, "learning_item_version": expected_version,
                "method_sha256": row["method_sha256"],
                "source_ids": list(dict.fromkeys(example["source_id"] for example in row["examples"])),
            }
        return {"status": "completed", "methods": rows}

    def read_writing_method_review_material(
        self,
        item_id: str,
        *,
        expected_version: str,
        method_ids: list[str],
    ) -> dict[str, Any]:
        """Read only the exact source pins needed to review up to three methods."""
        normalized_ids = list(dict.fromkeys(method_ids))
        if not normalized_ids or len(normalized_ids) > 3:
            raise ValueError("method review reads are limited to 1–3 methods")
        item = self.get_knowledge_item(item_id)
        if (
            item is None
            or not personal_method_study_scope(item)
            or str(item.get("version_id")) != expected_version
            or item.get("status") not in {"draft", "reviewed"}
            or not self._is_style_method_item(item)
        ):
            raise KnowledgeItemVersionConflictError("method review item changed or is unavailable")
        payload = StoredWritingStyleMethodPayload.model_validate(
            item["content"].get("craft_method_payload")
        )
        by_id = {method.method_id: method for method in payload.methods}
        if set(normalized_ids) - set(by_id):
            raise ValueError("method review requested an ID outside the selected card")
        output: list[dict[str, Any]] = []
        derived_studies = {
            str(row.get("item_id")): row
            for row in item["content"].get("derived_from_studies", [])
            if isinstance(row, dict) and row.get("item_id") is not None
        }
        for method_id in normalized_ids:
            method = by_id[method_id]
            if method.scope_assessment == "source_bound":
                continue
            if not self._method_source_pins_are_readable(item, method):
                continue
            evidence = self._read_method_examples(item, method)
            origin_study_ids = sorted({
                study_id
                for row in evidence
                for study_id in row.get("study_item_ids", [])
            })
            origin_studies = []
            for study_id in origin_study_ids:
                study = derived_studies.get(study_id)
                if study is None:
                    origin_studies.append({"study_item_id": study_id, "metadata_available": False})
                    continue
                reading_order = study.get("reading_order", {})
                origin_studies.append({
                    "study_item_id": study_id,
                    "version": study.get("version"),
                    "title": study.get("title"),
                    "selected_range": reading_order.get("selected_range")
                    if isinstance(reading_order, dict) else None,
                    "batch_range": reading_order.get("batch_range")
                    if isinstance(reading_order, dict) else None,
                    "metadata_available": True,
                })
            # These source pins are the very samples that produced this method.
            # Different chapter/source IDs do not establish an independent work or
            # authoring context. No separately owned transfer-sample input exists
            # in this contract, so portability must be checked with a labeled probe.
            transfer_candidates: list[dict[str, Any]] = []
            output.append(
                {
                    "method": method.model_dump(mode="json", exclude={"reuse_qualification"}),
                    "method_sha256": writing_method_content_hash(method),
                    "evidence_sha256": writing_method_evidence_hash(method),
                    "evidence": evidence,
                    "transfer_evidence_candidates": transfer_candidates,
                    "transfer_source_affiliation": {
                        "study_item_ids": origin_study_ids,
                        "studies": origin_studies,
                        "source_ids": sorted({row["source_id"] for row in evidence}),
                        "independent_scope_verified": False,
                        "verification_limit": (
                            "All listed source pins belong to the same method-study evidence set. "
                            "Distinct chapter/source IDs alone do not prove an independent work."
                        ),
                    },
                    "existing_qualification": (
                        method.reuse_qualification.model_dump(mode="json")
                        if method.reuse_qualification is not None else None
                    ),
                }
            )
        if len(output) != len(normalized_ids):
            raise ValueError("one or more review methods have no active, exact source evidence")
        return {
            "item_id": item_id,
            "item_version": int(item["version"]),
            "item_version_id": str(item["version_id"]),
            "space": str(item["space"]),
            "work_id": item.get("work_id"),
            "methods": output,
        }

    def search_unreviewed_personal_writing_method_items(
        self, query: str, *, limit: int = 6,
    ) -> dict[str, Any]:
        """Bound auto-review discovery to the author's unprojected learning library."""
        if not 1 <= limit <= 6:
            raise ValueError("unreviewed personal method discovery is limited to 6 items")
        rows = self.repository.search_unreviewed_personal_writing_method_items(
            query, top_k=limit
        )
        items: list[dict[str, Any]] = []
        for row in rows:
            current = self.get_knowledge_item(row["item_id"])
            if (
                current is None
                or current.get("space") != "learning"
                or current.get("work_id") is not None
                or current.get("status") != "draft"
                or str(current.get("version_id")) != row["version_id"]
                or int(current.get("version", -1)) != row["version"]
                or not self._is_style_method_item(current)
            ):
                continue
            items.append(
                {
                    "item_id": row["item_id"],
                    "version": row["version"],
                    "version_id": row["version_id"],
                    "title": row["title"],
                    "method_ids": list(row["method_ids"]),
                    "source_scope": deepcopy(current["content"].get("source_scope", {})),
                }
            )
        return {"status": "completed", "items": items, "item_count": len(items)}

    def search_personal_writing_method_study_materials(
        self, query: str, *, limit: int = 3,
    ) -> dict[str, Any]:
        """Find study-note inputs only in the author's unscoped learning library."""
        normalized = query.strip()
        if not normalized or len(normalized) > 500:
            raise ValueError("personal learning-material query must contain 1–500 characters")
        if not 1 <= limit <= 3:
            raise ValueError("personal learning-material results are limited to 3 items")
        rows = self.repository.search_knowledge_items(
            normalized,
            "learning",
            None,
            top_k=limit,
            methods_only=False,
        )
        items: list[dict[str, Any]] = []
        for row in rows:
            item_id = str(row.get("item_id", ""))
            current = self.get_knowledge_item(item_id) if item_id else None
            if (
                current is None
                or current.get("space") != "learning"
                or current.get("work_id") is not None
                or current.get("kind") != "source_study"
                or current.get("status") not in {"draft", "reviewed"}
                or not isinstance(current.get("content"), dict)
            ):
                continue
            content = current["content"]
            source_refs = content.get("source_refs", [])
            if not isinstance(source_refs, list) or not source_refs or len(source_refs) > 12:
                continue
            pinned_sources = [
                {
                    "source_id": str(source["source_id"]),
                    "snapshot_id": str(source["snapshot_id"]),
                    "content_hash": str(source["content_hash"]),
                }
                for source in source_refs
                if isinstance(source, dict)
                and all(source.get(key) for key in ("source_id", "snapshot_id", "content_hash"))
            ]
            if len(pinned_sources) != len(source_refs):
                continue
            pins_are_readable = True
            for pin in pinned_sources:
                try:
                    source = self.get_source(
                        pin["source_id"], pin["snapshot_id"],
                        content_offset=0, content_limit=1, include_evidence=False,
                    )
                except (KeyError, ValueError):
                    pins_are_readable = False
                    break
                if (
                    source is None
                    or (source.get("space"), source.get("work_id")) != ("learning", None)
                    or source.get("snapshot", {}).get("id") != pin["snapshot_id"]
                    or source.get("snapshot", {}).get("content_hash") != pin["content_hash"]
                ):
                    pins_are_readable = False
                    break
            if not pins_are_readable:
                continue
            items.append(
                {
                    "item_id": item_id,
                    "version": int(current["version"]),
                    "version_id": str(current["version_id"]),
                    "title": str(content.get("title", "学习资料")),
                    "analysis_type": str(content.get("analysis_type", "")),
                    "learning_role": str(content.get("learning_role", "")),
                    "status": str(current["status"]),
                    "source_scope": deepcopy(content.get("source_scope", {})),
                    "source_refs": pinned_sources,
                }
            )
            if len(items) >= limit:
                break
        return {
            "status": "completed",
            "scope": {"space": "learning", "work_id": None},
            "query": normalized,
            "items": items,
            "item_count": len(items),
        }

    def record_personal_writing_method_reviews(
        self,
        item_id: str,
        *,
        expected_version: str,
        qualifications: dict[str, PersonalWritingMethodQualification | dict[str, Any]],
    ) -> dict[str, Any]:
        """Append only review output bound to the precise current method/evidence."""
        if not expected_version:
            raise ValueError("a current knowledge-item version is required for method review")
        if not qualifications or len(qualifications) > 3:
            raise ValueError("one review write is limited to 1–3 methods")
        item = self.get_knowledge_item(item_id)
        if (
            item is None
            or not personal_method_study_scope(item)
            or item.get("kind") != "source_study"
            or item.get("status") not in {"draft", "reviewed"}
            or not isinstance(item.get("content"), dict)
            or item["content"].get("analysis_type") != "style"
            or item["content"].get("learning_role") != "craft_method_synthesis"
        ):
            raise ValueError("method reviews require an active style-method study item")
        if str(item.get("version_id")) != expected_version:
            raise KnowledgeItemVersionConflictError("the style-method item changed during review")
        payload = StoredWritingStyleMethodPayload.model_validate(
            item["content"].get("craft_method_payload")
        )
        by_id = {method.method_id: method for method in payload.methods}
        if set(qualifications) - set(by_id):
            raise ValueError("method review output references a method outside this card")

        updated_methods: list[dict[str, Any]] = []
        provider_attempt_ids: set[str] = set()
        for method in payload.methods:
            row = method.model_dump(mode="json")
            raw_qualification = qualifications.get(method.method_id)
            if raw_qualification is not None:
                qualification = (
                    raw_qualification
                    if isinstance(raw_qualification, PersonalWritingMethodQualification)
                    else PersonalWritingMethodQualification.model_validate(raw_qualification)
                )
                for phase_receipt in (qualification.induction, qualification.transfer):
                    for provider_attempt in phase_receipt.provider_attempts:
                        if provider_attempt.workflow_node_id != method.method_id:
                            raise ValueError(
                                "provider attempts must belong to the exact reviewed method"
                            )
                        if provider_attempt.attempt_id in provider_attempt_ids:
                            raise ValueError("provider attempt IDs must be unique within a review write")
                        provider_attempt_ids.add(provider_attempt.attempt_id)
                if qualification.method_sha256 != writing_method_content_hash(method):
                    raise ValueError("review output is not bound to the current method content")
                if qualification.evidence_sha256 != writing_method_evidence_hash(method):
                    raise ValueError("review output is not bound to the current evidence identities")
                if not self._method_source_pins_are_readable(item, method):
                    raise ValueError("a pinned method source was withdrawn before review could be saved")
                incoming_draft = qualification.induction.research_probe_draft
                recovery = incoming_draft.recovered_from if incoming_draft is not None else None
                existing_qualification = method.reuse_qualification
                if recovery is not None:
                    if incoming_draft is None:
                        raise ValueError("recovery provenance requires its exact fixed draft")
                    if recovery.status == "started":
                        if (
                            qualification.status != "failed"
                            or qualification.failure_reason != "probe_recovery_transfer_not_attempted"
                            or qualification.induction.decision != "pass"
                            or qualification.transfer.decision != "insufficient"
                            or qualification.transfer.provider_attempts
                            or existing_qualification is None
                            or existing_qualification.status != "insufficient"
                            or existing_qualification.reuse_scope != "work"
                            or existing_qualification.method_sha256 != qualification.method_sha256
                            or existing_qualification.evidence_sha256 != qualification.evidence_sha256
                            or existing_qualification.induction.decision != "insufficient"
                            or existing_qualification.transfer.decision != "insufficient"
                            or existing_qualification.induction.prompt_sha256 != recovery.prompt_sha256
                            or existing_qualification.induction.response_sha256 != recovery.response_sha256
                            or existing_qualification.induction.rationale != recovery.previous_induction_rationale
                            or existing_qualification.transfer.rationale != recovery.previous_transfer_rationale
                            or existing_qualification.induction.research_probe is not None
                            or existing_qualification.induction.research_probe_draft is not None
                            or len(existing_qualification.induction.provider_attempts) != 1
                            or existing_qualification.induction.provider_attempts[0].attempt_id
                            != recovery.provider_attempt_id
                            or existing_qualification.induction.provider_attempts[0].stage
                            != "learning.writing_method_induction"
                            or existing_qualification.induction.provider_attempts[0].workflow_node_id
                            != method.method_id
                            or existing_qualification.induction.provider_attempts[0].status != "completed"
                            or existing_qualification.transfer.provider_attempts
                            or existing_qualification.technical_retry_previous_attempt is not None
                            or existing_qualification.invalidation_reason is not None
                        ):
                            raise ValueError(
                                "recovery start is not bound to the exact prior insufficient invalid-probe receipt"
                            )
                    else:
                        prior_draft = (
                            existing_qualification.induction.research_probe_draft
                            if existing_qualification is not None else None
                        )
                        prior_recovery = (
                            prior_draft.recovered_from
                            if prior_draft is not None else None
                        )
                        incoming_origin = recovery.model_dump(mode="json", exclude={"status"})
                        prior_origin = (
                            prior_recovery.model_dump(mode="json", exclude={"status"})
                            if prior_recovery is not None else None
                        )
                        if (
                            existing_qualification is None
                            or existing_qualification.status != "failed"
                            or existing_qualification.failure_reason != "probe_recovery_transfer_not_attempted"
                            or prior_draft is None
                            or prior_recovery is None
                            or prior_recovery.status != "started"
                            or prior_origin != incoming_origin
                            or prior_draft.model_copy(update={
                                "recovered_from": prior_draft.recovered_from.model_copy(
                                    update={"status": "completed"}
                                )
                            }) != incoming_draft
                            or (
                                qualification.status == "failed"
                                and qualification.failure_reason == "probe_recovery_transfer_not_attempted"
                            )
                            or len(qualification.transfer.provider_attempts) > 1
                        ):
                            raise ValueError(
                                "recovered probe may only be finalized once from its persisted start marker"
                            )

                    transfer_material = self.read_writing_method_review_material(
                        item_id,
                        expected_version=expected_version,
                        method_ids=[method.method_id],
                    )
                    material_row = next(
                        (candidate for candidate in transfer_material["methods"]
                         if candidate["method"]["method_id"] == method.method_id),
                        None,
                    )
                    if (material_row or {}).get("transfer_source_affiliation") != incoming_draft.source_affiliation:
                        raise ValueError(
                            "recovery draft source affiliation changed before it could be saved"
                        )
                if qualification.status == "qualified" and (
                    qualification.reuse_scope != "personal"
                    or method.scope_assessment == "source_bound"
                    or qualification.induction.decision != "pass"
                    or qualification.transfer.decision != "pass"
                ):
                    raise ValueError(
                        "personal qualification needs passing induction and transfer reviews"
                    )
                method_pins = writing_method_evidence_pins(method)
                known_ids = {pin["evidence_id"] for pin in method_pins}
                if (
                    not qualification.induction.cited_evidence_ids
                    or not set(qualification.induction.cited_evidence_ids).issubset(known_ids)
                ):
                    raise ValueError("induction output cites evidence outside the method's pinned examples")
                transfer_citation_ids = known_ids | {
                    row.evidence_id
                    for row in qualification.transfer.transfer_evidence
                    if isinstance(row, PinnedWritingMethodTransferEvidence)
                }
                if (
                    not qualification.transfer.cited_evidence_ids
                    or not set(qualification.transfer.cited_evidence_ids).issubset(transfer_citation_ids)
                ):
                    raise ValueError("transfer output cites evidence outside its exact pinned inputs")

                generated_probes = [
                    row for row in qualification.transfer.transfer_evidence
                    if isinstance(row, ModelGeneratedWritingMethodTransferProbe)
                ]
                if generated_probes:
                    fixed_draft = qualification.induction.research_probe_draft
                    if fixed_draft is not None:
                        draft_hash = writing_method_transfer_probe_draft_hash(
                            fixed_draft,
                            method_sha256=qualification.method_sha256,
                            evidence_sha256=qualification.evidence_sha256,
                            research_probe_reason=qualification.induction.research_probe_reason or "",
                        )
                        transfer_material = self.read_writing_method_review_material(
                            item_id,
                            expected_version=expected_version,
                            method_ids=[method.method_id],
                        )
                        material_row = next(
                            (row for row in transfer_material["methods"]
                             if row["method"]["method_id"] == method.method_id),
                            None,
                        )
                        current_affiliation = (material_row or {}).get("transfer_source_affiliation")
                        assessed_probe = generated_probes[0] if len(generated_probes) == 1 else None
                        if (
                            current_affiliation != fixed_draft.source_affiliation
                            or qualification.induction.research_probe is not None
                            or qualification.induction.research_probe_sha256 != draft_hash
                            or qualification.transfer.research_probe_sha256 != draft_hash
                            or assessed_probe is None
                            or assessed_probe.probe_text != fixed_draft.probe_text
                            or assessed_probe.protected_requirements != fixed_draft.protected_requirements
                            or assessed_probe.limitation != fixed_draft.limitation
                        ):
                            raise ValueError(
                                "transfer review must independently assess the exact current probe draft and source identity"
                            )
                    else:
                        # A saved legacy full-object qualification may be carried
                        # forward unchanged; the current review path never creates it.
                        fixed_probe = qualification.induction.research_probe
                        if (
                            method.reuse_qualification != qualification
                            or fixed_probe is None
                            or len(generated_probes) != 1
                            or generated_probes[0] != fixed_probe
                            or qualification.induction.research_probe_sha256
                            != writing_method_transfer_probe_hash(fixed_probe)
                            or qualification.induction.research_probe_sha256
                            != qualification.transfer.research_probe_sha256
                        ):
                            raise ValueError(
                                "legacy transfer receipt must retain its original exact probe hash"
                            )

                pinned_evidence = [
                    row for row in qualification.transfer.transfer_evidence
                    if isinstance(row, PinnedWritingMethodTransferEvidence)
                ]
                if pinned_evidence:
                    transfer_material = self.read_writing_method_review_material(
                        item_id,
                        expected_version=expected_version,
                        method_ids=[method.method_id],
                    )
                    material_row = next(
                        (row for row in transfer_material["methods"] if row["method"]["method_id"] == method.method_id),
                        None,
                    )
                    allowed_candidates = {
                        row["evidence_id"]: row
                        for row in (material_row or {}).get("transfer_evidence_candidates", [])
                    }
                    for transfer_evidence in pinned_evidence:
                        candidate = allowed_candidates.get(transfer_evidence.evidence_id)
                        if candidate is None or any(
                            getattr(transfer_evidence, field) != candidate[field]
                            for field in (
                                "source_id", "snapshot_id", "content_hash", "start_offset", "end_offset"
                            )
                        ):
                            raise ValueError(
                                "pinned transfer evidence must match an independently scoped sample from the knowledge owner"
                            )
                for transfer_evidence in qualification.transfer.transfer_evidence:
                    if not isinstance(
                        transfer_evidence,
                        (PinnedWritingMethodTransferEvidence, ModelGeneratedWritingMethodTransferProbe),
                    ):
                        raise ValueError("transfer evidence has an unsupported source kind")
                qualified_method = method.model_copy(update={"reuse_qualification": qualification})
                if not writing_method_qualification_is_current(qualified_method):
                    raise ValueError("review receipts do not match the current method and evidence pins")
                row["reuse_qualification"] = qualification.model_dump(mode="json")
            updated_methods.append(row)

        updated_content = deepcopy(item["content"])
        updated_content["craft_method_payload"] = StoredWritingStyleMethodPayload.model_validate(
            {
                **payload.model_dump(mode="json"),
                "methods": updated_methods,
                "schema_version": 3,
            }
        ).model_dump(mode="json")
        evidence_ids = list(item.get("evidence_span_ids") or [])
        # Call the existing KnowledgeService write owner directly. Generic content
        # updates below cannot create or change a qualification receipt.
        super().append_knowledge_version(
            item_id,
            status=str(item["status"]),
            content=updated_content,
            evidence_span_ids=evidence_ids,
            expected_version=expected_version,
        )
        return self.get_knowledge_item(item_id) or item

    def revoke_writing_methods(
        self,
        item_id: str,
        *,
        expected_version: str,
        method_ids: list[str],
        status: Literal["disabled", "withdrawn"],
        reason: str,
    ) -> dict[str, Any]:
        """Disable or withdraw exact method IDs without deleting their evidence or card."""
        normalized_ids = list(dict.fromkeys(method_ids))
        normalized_reason = reason.strip()
        if not expected_version:
            raise ValueError("method lifecycle changes require the current item version")
        if not normalized_ids or len(normalized_ids) > 3:
            raise ValueError("method lifecycle changes are limited to 1–3 exact IDs")
        if any(not method_id.strip() for method_id in normalized_ids):
            raise ValueError("method IDs must be non-empty strings")
        if not normalized_reason or len(normalized_reason) > 500:
            raise ValueError("a method lifecycle reason must contain 1–500 characters")
        item = self.get_knowledge_item(item_id)
        if (
            item is None
            or not personal_method_study_scope(item)
            or item.get("status") not in {"draft", "reviewed"}
            or not self._is_style_method_item(item)
        ):
            raise ValueError("method lifecycle changes require an active style-method study item")
        if str(item.get("version_id")) != expected_version:
            raise KnowledgeItemVersionConflictError("the style-method item changed before lifecycle update")
        payload = StoredWritingStyleMethodPayload.model_validate(
            item["content"].get("craft_method_payload")
        )
        by_id = {method.method_id: method for method in payload.methods}
        if set(normalized_ids) - set(by_id):
            raise ValueError("method lifecycle update references an ID outside this card")
        updates: dict[str, PersonalWritingMethodQualification] = {}
        already_applied = True
        for method_id in normalized_ids:
            method = by_id[method_id]
            qualification = method.reuse_qualification
            if qualification is None:
                raise ValueError("only a reviewed method can be disabled or withdrawn")
            if qualification.status == status and qualification.invalidation_reason == normalized_reason:
                updates[method_id] = qualification
                continue
            if qualification.status != "qualified":
                raise ValueError("only a currently qualified method can change lifecycle status")
            already_applied = False
            updates[method_id] = qualification.model_copy(update={
                "status": status,
                "invalidation_reason": normalized_reason,
            })
        if already_applied:
            return item

        updated_methods = [
            method.model_copy(update={
                "reuse_qualification": updates.get(method.method_id, method.reuse_qualification),
            })
            .model_dump(mode="json")
            for method in payload.methods
        ]
        updated_content = deepcopy(item["content"])
        updated_content["craft_method_payload"] = StoredWritingStyleMethodPayload.model_validate(
            {
                **payload.model_dump(mode="json"),
                "methods": updated_methods,
                "schema_version": 3,
            }
        ).model_dump(mode="json")
        super().append_knowledge_version(
            item_id,
            status=str(item["status"]),
            content=updated_content,
            evidence_span_ids=list(item.get("evidence_span_ids") or []),
            expected_version=expected_version,
        )
        return self.get_knowledge_item(item_id) or item

    def append_knowledge_version(
        self,
        item_id: str,
        *,
        status: str,
        content: Any,
        evidence_span_ids: Iterable[str],
        expected_version: str,
    ) -> dict[str, Any]:
        current = self.get_knowledge_item(item_id)
        if current is not None and self._is_style_method_item(current):
            old_payload = StoredWritingStyleMethodPayload.model_validate(
                current["content"].get("craft_method_payload")
            )
            incoming_content = deepcopy(content) if isinstance(content, dict) else None
            incoming = incoming_content.get("craft_method_payload") if incoming_content else None
            if not isinstance(incoming, dict) or not isinstance(incoming.get("methods"), list):
                raise ValueError("style-method content needs its typed method payload")
            old_by_id = {method.method_id: method for method in old_payload.methods}
            normalized_methods: list[dict[str, Any]] = []
            for raw_method in incoming["methods"]:
                method = StoredWritingStyleMethod.model_validate(raw_method)
                old_method = old_by_id.get(method.method_id)
                old_qualification = (
                    old_method.reuse_qualification.model_dump(mode="json")
                    if old_method is not None and old_method.reuse_qualification is not None
                    else None
                )
                new_qualification = (
                    method.reuse_qualification.model_dump(mode="json")
                    if method.reuse_qualification is not None else None
                )
                method_changed = (
                    old_method is not None
                    and writing_method_content_hash(old_method) != writing_method_content_hash(method)
                )
                if method_changed and old_qualification is not None and new_qualification == old_qualification:
                    invalidated = method.reuse_qualification.model_copy(update={
                        "status": "invalidated",
                        "invalidation_reason": "method content changed; repeat evidence review before reuse",
                    })
                    method = method.model_copy(update={"reuse_qualification": invalidated})
                    new_qualification = invalidated.model_dump(mode="json")
                elif new_qualification is not None and new_qualification != old_qualification:
                    raise ValueError("method qualifications can only be written through LearningService review")
                normalized_methods.append(method.model_dump(mode="json"))
            incoming["methods"] = normalized_methods
            new_payload = StoredWritingStyleMethodPayload.model_validate(incoming)
            incoming_content["craft_method_payload"] = new_payload.model_dump(mode="json")
            content = incoming_content
        return super().append_knowledge_version(
            item_id,
            status=status,
            content=content,
            evidence_span_ids=evidence_span_ids,
            expected_version=expected_version,
        )

    def _is_style_method_item(self, item: dict[str, Any]) -> bool:
        content = item.get("content")
        return bool(
            item.get("kind") == "source_study"
            and isinstance(content, dict)
            and content.get("analysis_type") == "style"
            and content.get("learning_role") == "craft_method_synthesis"
        )

    def _method_source_pins_are_readable(self, item: dict[str, Any], method: StoredWritingStyleMethod) -> bool:
        source_refs = item.get("content", {}).get("source_refs", [])
        source_ref_hashes = {
            (str(row.get("source_id")), str(row.get("snapshot_id"))): str(row.get("content_hash"))
            for row in source_refs if isinstance(row, dict)
        }
        checked: set[tuple[str, str]] = set()
        for pin in writing_method_evidence_pins(method):
            identity = (pin["source_id"], pin["snapshot_id"])
            if source_ref_hashes.get(identity) != pin["content_hash"]:
                return False
            if identity in checked:
                continue
            checked.add(identity)
            try:
                source = self.get_source(
                    pin["source_id"], pin["snapshot_id"],
                    content_offset=0,
                    content_limit=1,
                    include_evidence=False,
                )
            except (KeyError, ValueError):
                return False
            if (
                source is None
                or (source.get("space"), source.get("work_id"))
                    != (item.get("space"), item.get("work_id"))
                or source.get("snapshot", {}).get("id") != pin["snapshot_id"]
                or source.get("snapshot", {}).get("content_hash") != pin["content_hash"]
            ):
                return False
        return True

    def _read_method_examples(
        self,
        item: dict[str, Any],
        method: StoredWritingStyleMethod,
        *, example_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        source_refs = {
            (str(row.get("source_id")), str(row.get("snapshot_id"))): row
            for row in item.get("content", {}).get("source_refs", [])
            if isinstance(row, dict)
        }
        pins_by_identity = {
            (row["evidence_id"], row["start_offset"], row["end_offset"]): row
            for row in writing_method_evidence_pins(method)
        }
        study_ids_by_pin = {
            (row.evidence_id, row.start_offset, row.end_offset): list(row.study_item_ids)
            for reference_group in (
                method.coverage.support,
                method.coverage.counterexamples,
                method.coverage.context,
            )
            for row in reference_group
        }
        output: list[dict[str, Any]] = []
        for example in method.examples:
            if example_ids is not None and example.evidence_id not in example_ids:
                continue
            pin = pins_by_identity.get(
                (example.evidence_id, example.start_offset, example.end_offset)
            )
            if pin is None:
                raise ValueError("method example has no exact persisted evidence identity")
            source = self.get_source(
                pin["source_id"], pin["snapshot_id"],
                content_offset=pin["start_offset"],
                content_limit=pin["end_offset"] - pin["start_offset"],
                include_evidence=False,
            )
            if (
                source is None
                or source.get("snapshot", {}).get("content_hash") != pin["content_hash"]
                or len(str(source.get("snapshot", {}).get("content", "")))
                    != pin["end_offset"] - pin["start_offset"]
                or (source.get("space"), source.get("work_id"))
                    != (item.get("space"), item.get("work_id"))
            ):
                raise ValueError("a selected method's source evidence is no longer readable")
            source_ref = source_refs.get((str(pin["source_id"]), str(pin["snapshot_id"])), {})
            output.append(
                {
                    "evidence_id": example.evidence_id,
                    "role": example.role,
                    "material_kind": example.material_kind,
                    "voice": example.voice,
                    "explanation": example.explanation,
                    "text": str(source["snapshot"]["content"]),
                    "source_id": pin["source_id"],
                    "source_title": source_ref.get("title"),
                    "snapshot_id": pin["snapshot_id"],
                    "content_hash": pin["content_hash"],
                    "start_offset": pin["start_offset"],
                    "end_offset": pin["end_offset"],
                    "study_item_ids": study_ids_by_pin.get(
                        (example.evidence_id, example.start_offset, example.end_offset), []
                    ),
                    "input_role": pin.get("input_role"),
                }
            )
        return output
