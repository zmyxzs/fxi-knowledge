"""Persist source parser progress through the existing candidate receipt owner."""

from __future__ import annotations

from typing import Any

from .graph_parser_resume import parse_group_checkpoint, parse_group_input_hash, refresh_group_checkpoint_candidate_ids
from .graph_parser_source_pin_validation import resolve_parse_source_manifest_pins


class ParseProgressWriteError(RuntimeError):
    def __init__(self, *, superseded: bool = False) -> None:
        super().__init__("parse input is superseded" if superseded else "parse progress persistence failed")
        self.superseded = superseded


class SourceParseProgress:
    def __init__(self, parser: Any, source: Any, claim: dict[str, Any], *, space: str, branch_id: str | None) -> None:
        self.parser, self.source, self.claim = parser, source, claim
        self.space, self.branch_id = space, branch_id
        self.candidate_count = int(claim.get("candidate_count", 0))
        self.coverage_completed = int(claim.get("coverage_completed", 0))

    def save(
        self, *, groups: list[tuple[dict[str, Any], ...]],
        checkpoints: dict[str, dict[str, Any]], candidates: list[dict[str, Any]],
        dialogue_utterances: list[dict[str, Any]], provider_model: str | None,
    ) -> None:
        complete_checkpoints = {key: dict(value) for key, value in checkpoints.items()}
        for group in groups:
            ref, pending = parse_group_checkpoint(
                group, source_hash=self.source.input_hash, prompt_hash=self.parser.prompt_hash,
                input_hash=parse_group_input_hash(group, branch_id=self.branch_id),
            )
            complete_checkpoints.setdefault(ref, pending)
        refresh_group_checkpoint_candidate_ids(
            checkpoints=complete_checkpoints, groups=groups,
            input_hash=self.source.input_hash, candidates=candidates,
        )
        manifest_pin, source_pin = resolve_parse_source_manifest_pins(
            source=self.source, segments=self.source.segments, space=self.space,
        )
        covered = sum(row["coverage_count"] for row in complete_checkpoints.values() if row["status"] == "passed")
        self.complete(
            receipt_id=str(self.claim["receipt_id"]), expected_attempt=int(self.claim["attempt_count"]),
            provider_model=provider_model or str(self.claim.get("provider_model") or self.parser.model_gateway.config.chat.model),
            candidates=candidates, dialogue_utterances=dialogue_utterances,
            coverage_completed=covered, review_status="incomplete", checkpoint_only=True,
            failed_group_refs=[key for key, row in complete_checkpoints.items() if row["status"] != "passed"],
            group_checkpoints=complete_checkpoints,
            narrative_manifest_pin=manifest_pin.model_copy(update={"offset": 0}).model_dump(mode="json") if manifest_pin else None,
            narrative_source_pin=source_pin,
        )

    def complete(self, **request: Any) -> int:
        try:
            count = self.parser.knowledge.complete_knowledge_parse(**request)
        except Exception as exc:
            # The write may have committed before its caller lost the result.
            # Read the same attempt before reporting or attempting another save.
            try:
                saved = self.parser.knowledge.get_knowledge_parse_progress(str(self.claim["receipt_id"]))
            except Exception:
                saved = None
            if saved and saved["generation"] == self.claim["generation"]:
                self.candidate_count = int(saved["candidate_count"])
                self.coverage_completed = int(saved["coverage_completed"])
                if (saved["attempt_count"] == request["expected_attempt"]
                        and saved["status"] == "completed" and not request.get("checkpoint_only")
                        and saved["review_status"] == request["review_status"]
                        and self.candidate_count == len(request["candidates"])
                        and saved["review_diagnostic"].get("group_checkpoints") == request["group_checkpoints"]):
                    return self.candidate_count
            raise ParseProgressWriteError() from exc
        if count is None:
            raise ParseProgressWriteError(superseded=True)
        self.candidate_count = count
        self.coverage_completed = int(request["coverage_completed"])
        return count
