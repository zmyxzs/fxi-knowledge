"""Materialize parse-batch status without coupling it to creation/lifecycle writes."""

from .repository_support import *
from .parse_candidate_qualification import qualified_candidate_sql
from .repository_parse_publication import published_graph_candidate_filter
from .repository_parse_queue import attach_parse_queue_states
from .repository_parse_availability import read_batch_claim_availability
from .candidate_publication_sql import reviewed_claim_table_exists
from .graph_parser_batch_stage_contracts import parse_item_has_extraction_failures
from .parse_completion import (
    base_completion, completion_goal, deferred_stages, execution_goal,
    item_execution_status, original_goal_complete, required_stages,
)


class KnowledgeParseBatchReadRepositoryMixin:
    def verify_knowledge_parse_resolution_authorization(
        self, *, batch_id: str, source_id: str, authorization: dict[str, Any],
    ) -> dict[str, Any]:
        from .repository_parse_resolution_proofs import verify_resolution_authorization

        return verify_resolution_authorization(
            self, batch_id=batch_id, source_id=source_id, authorization=authorization,
        )

    def list_published_event_identity_coverage(
        self, candidate_pins: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        """Read event-identity coverage only from the exact published parse resolution.

        The parse stage is the sole owner of event reconciliation coverage. Current item,
        version, source snapshot, checked version, and post-resolution version must all
        agree before the audited payload is returned to query consumers.
        """
        pins: dict[str, dict[str, Any]] = {}
        for pin in candidate_pins:
            item_id = str(pin.get("item_id") or "")
            version_id = str(pin.get("version_id") or "")
            if not item_id or not version_id:
                raise ValueError("event identity reads require exact item and version pins")
            if item_id in pins:
                raise ValueError("event identity candidate pins must be unique")
            source_pins = pin.get("source_pins")
            if not isinstance(source_pins, list):
                source_pins = []
            pins[item_id] = {
                "version_id": version_id,
                "source_pins": [
                    {
                        "source_id": str(value.get("source_id") or ""),
                        "snapshot_id": str(value.get("snapshot_id") or ""),
                        "content_hash": str(value.get("content_hash") or ""),
                    }
                    for value in source_pins if isinstance(value, dict)
                ],
            }
        if not pins:
            return {}

        published = published_graph_candidate_filter("item")
        resolution = """CASE WHEN json_extract(batch_item.stages_json, '$.publish.status') = 'completed'
            THEN json_extract(batch_item.stages_json, '$.resolution')
            ELSE json_extract(batch_item.result_json, '$.publication_checkpoint.resolution') END"""
        resolution_revision = """CASE WHEN json_extract(batch_item.stages_json, '$.publish.status') = 'completed'
            THEN batch_item.stage_revision
            ELSE json_extract(batch_item.result_json, '$.publication_checkpoint.stage_revision') END"""
        resolution_owner_batch_id = """CASE WHEN json_extract(batch_item.stages_json, '$.publish.status') = 'completed'
            THEN batch_item.batch_id
            WHEN json_type(batch_item.result_json, '$.publication_checkpoint.publication_batch_id') = 'text'
             AND length(trim(json_extract(batch_item.result_json,
                    '$.publication_checkpoint.publication_batch_id'))) > 0
            THEN json_extract(batch_item.result_json, '$.publication_checkpoint.publication_batch_id')
            ELSE receipt.parse_batch_id END"""

        pin_json = json.dumps(
            [
                {
                    "item_id": item_id,
                    "version_id": value["version_id"],
                    "source_pins": value["source_pins"],
                }
                for item_id, value in pins.items()
            ],
            separators=(",", ":"),
        )
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""
                WITH requested AS (
                    SELECT json_extract(pin.value, '$.item_id') AS item_id,
                           json_extract(pin.value, '$.version_id') AS version_id,
                           json_extract(pin.value, '$.source_pins') AS source_pins
                    FROM json_each(?) AS pin
                )
                SELECT requested.item_id AS requested_item_id,
                       requested.version_id AS requested_version_id,
                       item.id AS item_id, item.status AS item_status,
                       item.origin_parse_receipt_id AS receipt_id,
                       version.id AS current_version_id,
                       receipt.parse_batch_id, receipt.source_id,
                       receipt.snapshot_id, receipt.generation,
                       snapshot.content_hash,
                       batch_item.stages_json, {resolution_revision} AS stage_revision,
                       {resolution_owner_batch_id} AS resolution_owner_batch_id,
                       batch_item.result_json,
                       json_extract({resolution}, '$.status')
                           AS resolution_status,
                       json_extract({resolution}, '$.input_hash')
                           AS resolution_input_hash,
                       json_extract({resolution}, '$.result')
                           AS resolution_result_json
                FROM requested
                JOIN knowledge_item AS item ON item.id = requested.item_id
                JOIN knowledge_version AS version
                  ON version.id = item.published_graph_version_id
                 AND version.item_id = item.id
                JOIN knowledge_parse_receipt AS receipt
                  ON receipt.id = item.origin_parse_receipt_id
                JOIN source_current_snapshot AS current
                  ON current.source_id = receipt.source_id
                 AND current.snapshot_id = receipt.snapshot_id
                JOIN source_snapshot AS snapshot
                  ON snapshot.id = current.snapshot_id
                 AND snapshot.source_id = current.source_id
                JOIN knowledge_parse_batch_item AS batch_item
                  ON batch_item.batch_id = receipt.parse_batch_id
                 AND batch_item.source_id = receipt.source_id
                 AND batch_item.snapshot_id = receipt.snapshot_id
                JOIN knowledge_parse_batch AS batch
                  ON batch.id = batch_item.batch_id
                WHERE item.kind = 'knowledge_graph_candidate'
                  AND item.status NOT IN ('deleted', 'rejected')
                  AND item.space = receipt.space AND item.work_id IS receipt.work_id
                  AND {published}
                  AND batch.space = receipt.space AND batch.work_id IS receipt.work_id
                  AND batch.branch_id IS receipt.branch_id
                  AND json_extract(batch_item.result_json, '$.receipt_id') = receipt.id
                  AND CAST(json_extract(batch_item.result_json, '$.generation') AS INTEGER)
                      = receipt.generation
                  AND EXISTS (
                      SELECT 1 FROM json_each(requested.source_pins) AS pin
                      WHERE json_extract(pin.value, '$.source_id') = receipt.source_id
                        AND json_extract(pin.value, '$.snapshot_id') = receipt.snapshot_id
                        AND json_extract(pin.value, '$.content_hash') = snapshot.content_hash
                  )
                """,
                (pin_json,),
            ).fetchall()

        by_requested_id: dict[str, Any] = {
            str(row["requested_item_id"]): row for row in rows
        }
        output: dict[str, dict[str, Any]] = {}
        for item_id, requested in pins.items():
            row = by_requested_id.get(item_id)
            if row is None:
                output[item_id] = {
                    "available": False,
                    "reason": "no_current_published_resolution_pin",
                    "item_id": item_id,
                    "published_version_id": requested["version_id"],
                }
                continue
            if (
                str(row["requested_version_id"]) != requested["version_id"]
                or str(row["current_version_id"]) != requested["version_id"]
            ):
                output[item_id] = {
                    "available": False,
                    "reason": "published_candidate_version_changed",
                    "item_id": item_id,
                    "published_version_id": str(row["current_version_id"]),
                }
                continue
            if row["resolution_status"] != "completed" or not row["resolution_input_hash"]:
                output[item_id] = {
                    "available": False,
                    "reason": "current_resolution_stage_incomplete",
                    "item_id": item_id,
                    "published_version_id": str(row["current_version_id"]),
                    "resolution_batch_id": row["resolution_owner_batch_id"],
                }
                continue
            try:
                resolution = json.loads(str(row["resolution_result_json"] or "{}"))
            except (TypeError, json.JSONDecodeError):
                resolution = {}
            authorization = resolution.get("authorization")
            event_reconciliation = resolution.get("event_reconciliation")
            if not isinstance(authorization, dict) or not isinstance(event_reconciliation, dict):
                output[item_id] = {
                    "available": False,
                    "reason": "resolution_receipt_has_no_event_identity_result",
                    "item_id": item_id,
                    "published_version_id": str(row["current_version_id"]),
                    "resolution_batch_id": row["resolution_owner_batch_id"],
                }
                continue
            final_versions = authorization.get("final_candidate_versions")
            if (
                not isinstance(final_versions, dict)
                or final_versions.get(item_id) != str(row["current_version_id"])
            ):
                output[item_id] = {
                    "available": False,
                    "reason": "resolution_authorization_does_not_bind_published_version",
                    "item_id": item_id,
                    "published_version_id": str(row["current_version_id"]),
                    "resolution_batch_id": row["resolution_owner_batch_id"],
                }
                continue
            coverage_map = event_reconciliation.get("event_identity_coverage")
            coverage = None
            if isinstance(coverage_map, dict):
                coverage = coverage_map.get(item_id)
            elif isinstance(coverage_map, list):
                coverage = next(
                    (value for value in coverage_map
                     if isinstance(value, dict) and value.get("item_id") == item_id),
                    None,
                )
            if not isinstance(coverage, dict):
                output[item_id] = {
                    "available": False,
                    "reason": "event_identity_coverage_missing_for_target",
                    "item_id": item_id,
                    "published_version_id": str(row["current_version_id"]),
                    "resolution_batch_id": row["parse_batch_id"],
                }
                continue
            if (
                coverage.get("item_id") != item_id
                or not isinstance(coverage.get("checked_version_id"), str)
                or not coverage.get("checked_version_id")
                or coverage.get("resolution_batch_id") != str(row["resolution_owner_batch_id"])
                or coverage.get("resolution_stage") != "event_resolution"
                or coverage.get("resolution_stage_status") != "completed"
                or coverage.get("source_id") != str(row["source_id"])
                or coverage.get("snapshot_id") != str(row["snapshot_id"])
                or coverage.get("content_hash") != str(row["content_hash"])
            ):
                output[item_id] = {
                    "available": False,
                    "reason": "event_identity_target_pins_do_not_match_current_source",
                    "item_id": item_id,
                    "published_version_id": str(row["current_version_id"]),
                    "resolution_batch_id": row["resolution_owner_batch_id"],
                }
                continue
            output[item_id] = {
                "available": True,
                "item_id": item_id,
                "checked_version_id": str(coverage["checked_version_id"]),
                "published_version_id": str(row["current_version_id"]),
                "source_id": str(row["source_id"]),
                "snapshot_id": str(row["snapshot_id"]),
                "content_hash": str(row["content_hash"]),
                "resolution_batch_id": str(row["resolution_owner_batch_id"]),
                "resolution_stage_input_hash": str(row["resolution_input_hash"]),
                "resolution_stage_revision": int(row["stage_revision"]),
                "resolution_attempt_ids": list(authorization.get("attempt_ids") or []),
                "checked_candidate_versions": dict(
                    authorization.get("checked_candidate_versions") or {}
                ),
                "final_candidate_versions": dict(
                    authorization.get("final_candidate_versions") or {}
                ),
                "resolution_contract_version": coverage.get("resolution_contract_version"),
                "resolution_contract_fingerprint": coverage.get("resolution_contract_fingerprint"),
                "candidate_universe_source_snapshot_pins": list(
                    event_reconciliation.get("candidate_universe_source_snapshot_pins") or []
                ),
                "resolution_stage": coverage.get("resolution_stage"),
                "resolution_stage_status": coverage.get("resolution_stage_status"),
                "resolution_prompt_hash": coverage.get("resolution_prompt_hash"),
                "candidate_retrieval_status": coverage.get("candidate_retrieval_status"),
                "canonicalization_status": coverage.get("canonicalization_status"),
                "coverage": coverage,
                "event_reconciliation": event_reconciliation,
            }
        return output

    def get_knowledge_parse_batch(self, batch_id: str) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            batch = connection.execute(
                """
                SELECT id, space, work_id, requested_work_id, requested_manifest_pin_json,
                       folder_id, branch_id, max_concurrency,
                       force_reparse, status, total_sources, summary_json, diagnostic,
                       created_at, updated_at, client_batch_id, completion_goal, grouping_policy
                FROM knowledge_parse_batch WHERE id = ?
                """,
                (batch_id,),
            ).fetchone()
            if batch is None:
                return None
            rows = connection.execute(
                """
                SELECT item.ordinal, item.source_id, item.source_title, item.folder_id,
                       item.snapshot_id, item.status, item.result_json, item.diagnostic,
                       item.section_ids_json, item.stages_json, item.stage_revision,
                       item.execution_goal, item.base_publication_json,
                       receipt.id AS current_receipt_id,
                       receipt.generation AS current_receipt_generation,
                       receipt.review_status AS current_receipt_review_status,
                       receipt.review_diagnostic_json AS current_review_diagnostic_json,
                       receipt.coverage_total AS current_receipt_coverage_total,
                       receipt.coverage_completed AS current_receipt_coverage_completed,
                       receipt.candidate_count AS current_receipt_candidate_count
                FROM knowledge_parse_batch_item AS item
                JOIN knowledge_parse_batch AS batch ON batch.id = item.batch_id
                LEFT JOIN knowledge_parse_receipt AS receipt
                  ON receipt.id = json_extract(item.result_json, '$.receipt_id')
                 AND receipt.parse_batch_id = item.batch_id
                 AND receipt.source_id = item.source_id
                 AND receipt.snapshot_id = item.snapshot_id
                 AND receipt.space = batch.space
                 AND receipt.work_id IS batch.work_id
                 AND receipt.branch_id IS batch.branch_id
                 AND receipt.generation = CAST(
                     json_extract(item.result_json, '$.generation') AS INTEGER
                 )
                WHERE item.batch_id = ? ORDER BY item.ordinal
                """,
                (batch_id,),
            ).fetchall()
            claim_availability = read_batch_claim_availability(connection, batch_id)
            readable_candidate_filter = published_graph_candidate_filter(
                "item", include_reviewed_claims=reviewed_claim_table_exists(connection),
            )
            logical_card_count = int(connection.execute(
                f"""
                SELECT COUNT(DISTINCT current_candidate.card_key)
                FROM knowledge_parse_batch_item AS selected
                JOIN knowledge_parse_batch AS batch ON batch.id = selected.batch_id
                JOIN knowledge_parse_receipt AS receipt ON receipt.source_id = selected.source_id
                  AND receipt.snapshot_id = selected.snapshot_id AND receipt.status = 'completed'
                  AND receipt.branch_id IS batch.branch_id
                JOIN knowledge_item AS item ON item.origin_parse_receipt_id = receipt.id
                JOIN knowledge_card_candidate AS current_candidate
                  ON current_candidate.candidate_item_id = item.id
                 AND current_candidate.kind = 'knowledge_graph_candidate'
                JOIN knowledge_version AS version
                  ON version.id = current_candidate.candidate_version_id
                JOIN source_current_snapshot AS current ON current.source_id = receipt.source_id
                  AND current.snapshot_id = selected.snapshot_id
                JOIN source ON source.id = selected.source_id AND source.deleted_at IS NULL
                  AND source.space = batch.space AND source.work_id IS batch.work_id
                WHERE selected.batch_id = ? AND item.status NOT IN ('deleted', 'rejected')
                  AND json_extract(selected.result_json, '$.receipt_id') = receipt.id
                  AND {qualified_candidate_sql('receipt', 'item')}
                  AND {readable_candidate_filter}
                  AND (
                    json_array_length(selected.section_ids_json) = 0 OR EXISTS (
                        SELECT 1 FROM json_each(selected.section_ids_json) AS requested
                        JOIN source_section AS section ON section.id = requested.value
                        WHERE CAST(json_extract(version.content_json, '$.evidence_quote_start') AS INTEGER) >= section.start_offset
                          AND CAST(json_extract(version.content_json, '$.evidence_quote_end') AS INTEGER) <= section.end_offset
                    )
                  )
                """,
                (batch_id,),
            ).fetchone()[0])
            queue = attach_parse_queue_states(connection, [{
                "batch_id": batch_id, "status": str(batch["status"]),
            }])[0].get("queue")
        sources: list[dict[str, Any]] = []
        for row in rows:
            result = json.loads(str(row["result_json"])) if row["result_json"] else {}
            receipt_diagnostic = {}
            has_current_receipt = row["current_receipt_id"] is not None
            if has_current_receipt:
                try:
                    receipt_diagnostic = json.loads(
                        str(row["current_review_diagnostic_json"] or "{}")
                    )
                except (TypeError, json.JSONDecodeError):
                    receipt_diagnostic = {}
                receipt_failed_groups = receipt_diagnostic.get("failed_group_refs", [])
                if not isinstance(receipt_failed_groups, list) or not receipt_failed_groups:
                    receipt_failed_groups = result.get("failed_group_refs", [])
                if not isinstance(receipt_failed_groups, list):
                    receipt_failed_groups = []
                receipt_id = str(row["current_receipt_id"])
                review_status = str(row["current_receipt_review_status"])
                if review_status == "pending":
                    review_status = str(result.get("review_status") or review_status)
                failed_group_refs = [str(value) for value in receipt_failed_groups]
                generation = int(row["current_receipt_generation"])
                coverage_total = int(row["current_receipt_coverage_total"])
                coverage_completed = int(row["current_receipt_coverage_completed"])
            else:
                receipt_id = None
                review_status = "pending"
                failed_group_refs = [
                    str(value) for value in (result.get("failed_group_refs") or [])
                ]
                generation = int(result.get("generation", 0))
                coverage_total = int(result.get("coverage_total", 0))
                coverage_completed = int(result.get("coverage_completed", 0))
            recorded_stages = json.loads(str(row["stages_json"]))
            stages = {
                name: KnowledgeParseStageContent.model_validate(recorded_stages.get(name, {}))
                    .model_dump(mode="json")
                for name in KNOWLEDGE_PARSE_DOWNSTREAM_STAGES
            }
            extraction_status = str(row["status"])
            goal_item = {
                "extraction_status": extraction_status, "stages": stages,
                "execution_goal": str(row["execution_goal"]),
                "base_publication_json": row["base_publication_json"],
                "source_id": str(row["source_id"]), "snapshot_id": str(row["snapshot_id"]),
                "receipt_id": receipt_id, "generation": generation,
                "extraction_complete": review_status == "passed" and coverage_completed == coverage_total,
            }
            # Historical goal completion follows the receipt pinned in the
            # immutable item result. A later force_reparse=False batch can
            # reuse that receipt and move its current parse_batch_id; this
            # makes it unavailable as current publication evidence here, but
            # does not erase the fact that this batch once completed its goal.
            historical_goal_item = {
                **goal_item,
                "receipt_id": result.get("receipt_id"),
                "generation": result.get("generation"),
            }
            stage_order = required_stages(dict(batch), goal_item)
            unfinished = next(
                (name for name in stage_order
                 if stages[name]["status"] != "completed"), None
            )
            failed_stage = (
                "extraction" if extraction_status == "failed" else next(
                    (name for name in stage_order
                     if stages[name]["status"] == "failed"), None
                )
            )
            source_status = item_execution_status(dict(batch), goal_item)
            base = base_completion(goal_item)
            base_failed = execution_goal(dict(batch), goal_item) == "reviewed_claims" and (
                stages["check"]["status"] == "completed" and base["status"] == "failed"
            )
            if base_failed:
                failed_stage = "base_publication"
            extraction_complete = review_status == "passed" and coverage_completed == coverage_total
            if source_status in {"completed", "reused"} and not extraction_complete:
                source_status = "partial"
            failure = stages[failed_stage] if failed_stage in stages else base if base_failed else {}
            source_error = failure.get("error") or result.get("error")
            resume_extraction_available = parse_item_has_extraction_failures({
                **goal_item,
                "review_status": review_status,
                "failed_group_refs": failed_group_refs,
                "error": source_error,
            })
            published = stages["publish"]["result"]
            sources.append({
                "execution_goal": execution_goal(dict(batch), goal_item),
                "base_publication": base,
                "base_completion": base,
                "original_goal_completed": original_goal_complete(
                    dict(batch), historical_goal_item,
                ),
                "deferred_stages": deferred_stages(dict(batch), goal_item),
                "availability": claim_availability.get(int(row["ordinal"]), {
                    "published_records": 0, "reviewed_only_records": 0, "pipeline_published_records": 0,
                }),
                "ordinal": int(row["ordinal"]),
                "source_id": str(row["source_id"]),
                "title": str(row["source_title"]),
                "folder_id": row["folder_id"],
                "snapshot_id": str(row["snapshot_id"]),
                "section_ids": json.loads(str(row["section_ids_json"])),
                "status": source_status,
                "extraction_status": extraction_status,
                "extraction_complete": extraction_complete,
                "candidate_qualification": receipt_diagnostic.get("candidate_qualification"),
                "stages": stages,
                "stage_revision": int(row["stage_revision"]),
                "current_stage": (
                    "extraction" if extraction_status not in {"completed", "reused"}
                    else unfinished
                ),
                "failed_stage": failed_stage,
                "unresolved_count": int(published.get("unresolved_count", stages["check"]["result"].get("unresolved_count", 0))),
                "source_record_count": published.get("source_record_count"),
                "logical_card_count": published.get("logical_card_count"),
                "candidate_count": int(row["current_receipt_candidate_count"] if has_current_receipt
                                       else result.get("candidate_count", 0)),
                "attempts": int(result.get("attempts", 0)),
                "receipt_id": receipt_id,
                "review_status": review_status,
                "failed_group_refs": failed_group_refs,
                "resume_extraction_available": resume_extraction_available,
                "generation": generation,
                "coverage_total": coverage_total,
                "coverage_completed": coverage_completed,
                "chapter_total": int(
                    result["chapter_total"]
                    if result.get("chapter_total") is not None
                    else len(json.loads(str(row["section_ids_json"])))
                ),
                "chapter_completed": int(result.get("chapter_completed") or 0),
                "chapter_failed": int(result.get("chapter_failed") or 0),
                "failed_section_ids": list(result.get("failed_section_ids") or []),
                "failed_check_section_ids": list(stages["check"]["result"].get("failed_section_ids", [])),
                "chapter_progress": result.get("chapter_progress"),
                "metrics": result.get("metrics"),
                "error": source_error,
                "diagnostic": failure.get("diagnostic") or result.get("diagnostic") or row["diagnostic"],
            })
        completed = sum(item["status"] in {"completed", "reused"} for item in sources)
        superseded = sum(item["status"] == "failed" and item["error"] == "superseded" for item in sources)
        failed = sum(item["status"] == "failed" and item["error"] != "superseded" for item in sources)
        running = sum(item["status"] == "running" for item in sources)
        queued = sum(item["status"] == "queued" for item in sources)
        identity_resolution = json.loads(str(batch["summary_json"]))
        if not isinstance(identity_resolution, dict) or "status" not in identity_resolution:
            identity_resolution = None
        return {
            "batch_id": str(batch["id"]),
            "completion_goal": completion_goal(dict(batch)),
            "grouping_policy": str(batch["grouping_policy"]),
            "original_goal_completed": all(item["original_goal_completed"] for item in sources),
            "client_batch_id": batch["client_batch_id"],
            "queue": queue,
            "space": str(batch["space"]),
            "work_id": batch["work_id"],
            "requested_work_id": batch["requested_work_id"],
            "requested_manifest_pin": self._decode_manifest_history(batch["requested_manifest_pin_json"]),
            "folder_id": batch["folder_id"],
            "branch_id": batch["branch_id"],
            "max_concurrency": int(batch["max_concurrency"]),
            "force_reparse": bool(batch["force_reparse"]),
            "status": str(batch["status"]),
            "total_sources": int(batch["total_sources"]),
            "completed": completed,
            "partial": sum(item["status"] == "partial" for item in sources),
            "failed": failed,
            "superseded": superseded,
            "running": running,
            "queued": queued,
            "candidate_count": sum(item["candidate_count"] for item in sources),
            "extracted": sum(item["extraction_status"] in {"completed", "reused"} for item in sources),
            "unresolved_count": sum(item["unresolved_count"] for item in sources),
            "source_record_count": sum(item["source_record_count"] or 0 for item in sources),
            "availability": {
                key: sum(item["availability"][key] for item in sources)
                for key in ("published_records", "reviewed_only_records", "pipeline_published_records")
            },
            "logical_card_count": logical_card_count,
            "stage_summary": {
                stage: {state: sum(item["stages"][stage]["status"] == state for item in sources)
                        for state in ("queued", "running", "completed", "failed")}
                for stage in KNOWLEDGE_PARSE_DOWNSTREAM_STAGES
            },
            "chapter_total": sum(item["chapter_total"] for item in sources),
            "chapter_completed": sum(item["chapter_completed"] for item in sources),
            "chapter_failed": sum(item["chapter_failed"] for item in sources),
            "sources": sources,
            "identity_resolution": identity_resolution,
            "diagnostic": batch["diagnostic"],
            "created_at": str(batch["created_at"]),
            "updated_at": str(batch["updated_at"]),
        }
