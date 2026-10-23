"""Concurrent, checkpointed semantic checks for independent parse-batch sources."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from weakref import WeakKeyDictionary

from .graph_parser_batch_stage_contracts import verified_manifest_order_key
from .graph_parser_stage_audit_refs import parse_stage_source_pin
from .graph_parser_support import logger
from .repository_support import ProseLifecycleEventSupersededError


_INVOCATION_TOKENS: WeakKeyDictionary[Any, dict[str, object]] = WeakKeyDictionary()


def _set_invocation_token(parser: Any, batch_id: str) -> object:
    token = object()
    _INVOCATION_TOKENS.setdefault(parser, {})[batch_id] = token
    return token


def _is_current_invocation(parser: Any, batch_id: str, token: object) -> bool:
    return (
        _INVOCATION_TOKENS.get(parser, {}).get(batch_id) is token
        and parser._parse_batch_is_running(batch_id)
    )


def _clear_invocation_token(parser: Any, batch_id: str, token: object) -> None:
    tokens = _INVOCATION_TOKENS.get(parser)
    if tokens is None or tokens.get(batch_id) is not token:
        return
    tokens.pop(batch_id, None)
    if not tokens:
        _INVOCATION_TOKENS.pop(parser, None)


def _parse_stage_source_pins_for_unit(
    item: dict[str, Any], section_ids: list[str] | None,
) -> list[dict[str, Any]]:
    """Resolve a chapter unit to the exact extraction receipt that covered it."""
    if not section_ids or not isinstance(item.get("chapter_progress"), dict):
        return [parse_stage_source_pin(item)]
    selected_sections = list(item.get("section_ids") or [])
    completed = item["chapter_progress"].get("completed")
    if not isinstance(completed, list):
        raise ValueError("chapter parse progress has no completed receipt ranges")
    pins: dict[tuple[str, int], dict[str, Any]] = {}
    for section_id in section_ids:
        try:
            index = selected_sections.index(section_id)
        except ValueError as exc:
            raise ValueError("check section is outside its selected source manifest") from exc
        matches = [
            row for row in completed
            if isinstance(row, dict)
            and row.get("status") in {"completed", "reused"}
            and isinstance(row.get("start"), int)
            and isinstance(row.get("end"), int)
            and int(row["start"]) <= index < int(row["end"])
            and isinstance(row.get("receipt_id"), str)
            and row.get("receipt_id")
            and isinstance(row.get("generation"), int)
            and not isinstance(row.get("generation"), bool)
            and int(row["generation"]) > 0
        ]
        receipt_keys = {
            (str(row["receipt_id"]), int(row["generation"])) for row in matches
        }
        if len(receipt_keys) != 1:
            raise ValueError("chapter does not have one exact completed extraction receipt")
        receipt_id, generation = next(iter(receipt_keys))
        pins[(receipt_id, generation)] = parse_stage_source_pin({
            **item, "receipt_id": receipt_id, "generation": generation,
        })
    return [pins[key] for key in sorted(pins)]


async def check_parse_batch_sources(
    parser: Any,
    batch: dict[str, Any],
    items: list[dict[str, Any]],
) -> None:
    """Check extraction-ready sources concurrently, preserving per-source checkpoints.

    The owner's ``review_sources`` method retains responsibility for its shared
    global audit semaphore. This layer only bounds concurrent source checks by
    the batch's persisted ``max_concurrency`` value.
    """
    if not items:
        return

    batch_id = str(batch["batch_id"])
    token = _set_invocation_token(parser, batch_id)
    try:
        try:
            positions, scope_error = await parser._current_verified_batch_positions(batch, items)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            positions = {}
            scope_error = f"批次来源清单核验失败（{type(exc).__name__}）；已停止语义检查。"

        manifest_pin = batch.get("requested_manifest_pin")
        ordered = sorted(
            items,
            key=lambda item: verified_manifest_order_key(
                item, positions.get(str(item["source_id"])), manifest_pin,
            ),
        )
        max_concurrency = batch.get("max_concurrency")
        if (
            isinstance(max_concurrency, bool)
            or not isinstance(max_concurrency, int)
            or max_concurrency < 1
        ):
            raise ValueError("parse batch max_concurrency must be a positive integer")
        semaphore = asyncio.Semaphore(max_concurrency)

        async def check_one(item: dict[str, Any]) -> None:
            async with semaphore:
                if not _is_current_invocation(parser, batch_id, token):
                    return
                if item["stages"]["check"]["status"] == "completed":
                    return
                try:
                    parser._record_parse_stage(batch, item, "check", "running")
                    if not _is_current_invocation(parser, batch_id, token):
                        return
                    if scope_error:
                        raise ValueError(scope_error)

                    selected = list(item["section_ids"])
                    units = [[section_id] for section_id in selected] if selected else [None]
                    check_result = item["stages"]["check"]["result"]
                    completed_units = list(check_result.get("units", []))
                    failed_units = list(check_result.get("failed_units", []))

                    def failed_section_ids() -> list[str]:
                        return list(dict.fromkeys(
                            str(section_id)
                            for row in failed_units
                            if isinstance(row, dict)
                            for section_id in (row.get("section_ids") or [])
                        ))

                    def checkpoint_failed_unit(
                        unit_section_ids: list[str] | None,
                        diagnostic: str,
                        *,
                        review_result: dict[str, Any] | None = None,
                    ) -> None:
                        nonlocal check_result
                        failed_units[:] = [
                            row for row in failed_units
                            if not isinstance(row, dict)
                            or row.get("section_ids") != unit_section_ids
                        ]
                        failure = {
                            "section_ids": unit_section_ids,
                            "status": "failed",
                            "error": "check_failed",
                            "diagnostic": diagnostic[:600],
                        }
                        if review_result is not None:
                            failure["review"] = review_result
                        failed_units.append(failure)
                        check_result = {
                            **check_result,
                            "units": completed_units,
                            "failed_units": failed_units,
                            "failed_section_ids": failed_section_ids(),
                        }
                        parser._record_parse_stage(
                            batch, item, "check", "running", result=check_result,
                        )

                    for unit in units:
                        if not _is_current_invocation(parser, batch_id, token):
                            return
                        if any(
                            row.get("section_ids") == unit and row.get("status") == "completed"
                            and row.get("selection") is None
                            for row in completed_units
                        ):
                            continue

                        failed_units[:] = [
                            row for row in failed_units
                            if not isinstance(row, dict) or row.get("section_ids") != unit
                        ]
                        check_result = {
                            **check_result,
                            "units": completed_units,
                            "failed_units": failed_units,
                            "failed_section_ids": failed_section_ids(),
                        }
                        parser._record_parse_stage(
                            batch, item, "check", "running", result=check_result,
                        )

                        section_key = json.dumps(unit or [], separators=(",", ":"))
                        checkpoint_errors: list[Exception] = []

                        def checkpoint_plan(source_id, plan):
                            nonlocal check_result
                            if not _is_current_invocation(parser, batch_id, token):
                                raise ProseLifecycleEventSupersededError("semantic check invocation was superseded")
                            plans = dict(check_result.get("semantic_unit_plans", {}))
                            plans[section_key] = {source_id: plan}
                            check_result = {
                                **check_result,
                                "semantic_unit_plans": plans,
                                "units": completed_units,
                                "failed_units": failed_units,
                                "failed_section_ids": failed_section_ids(),
                            }
                            try:
                                parser._record_parse_stage(
                                    batch, item, "check", "running", result=check_result,
                                )
                            except Exception as exc:
                                checkpoint_errors.append(exc)
                                raise

                        def checkpoint_unit(selection, result):
                            nonlocal check_result
                            if not _is_current_invocation(parser, batch_id, token):
                                raise ProseLifecycleEventSupersededError("semantic check invocation was superseded")
                            completed_units[:] = [value for value in completed_units if value.get("selection") != selection]
                            completed_units.append({"section_ids": unit, "selection": selection,
                                                    "status": "completed", "review": result})
                            check_result = {
                                **check_result,
                                "units": completed_units,
                                "failed_units": failed_units,
                                "failed_section_ids": failed_section_ids(),
                            }
                            try:
                                parser._record_parse_stage(
                                    batch, item, "check", "running", result=check_result,
                                )
                            except Exception as exc:
                                checkpoint_errors.append(exc)
                                raise

                        try:
                            review = await parser.review_sources(
                                space=batch["space"],
                                work_id=batch.get("requested_work_id") or batch["work_id"],
                                folder_id=batch["folder_id"],
                                source_ids=[item["source_id"]],
                                branch_id=batch["branch_id"],
                                section_ids=unit,
                                expected_snapshot_ids={item["source_id"]: item["snapshot_id"]},
                                parse_stage_audit_context={
                                    "batch_id": batch_id,
                                    "source_snapshot_pins": _parse_stage_source_pins_for_unit(item, unit),
                                    "manifest_pin": batch.get("requested_manifest_pin"),
                                },
                                _planned_units=check_result.get("semantic_unit_plans", {}).get(section_key),
                                _completed_units=completed_units,
                                _checkpoint_plan=checkpoint_plan, _checkpoint_unit=checkpoint_unit,
                            )
                        except asyncio.CancelledError:
                            raise
                        except ProseLifecycleEventSupersededError:
                            raise
                        except Exception as exc:
                            if checkpoint_errors:
                                raise
                            if not _is_current_invocation(parser, batch_id, token):
                                return
                            logger.exception(
                                "knowledge parse batch %s source unit check failed source=%s sections=%s",
                                batch_id, item["source_id"], unit,
                            )
                            checkpoint_failed_unit(
                                unit,
                                f"语义检查单元失败（{type(exc).__name__}）：{str(exc)}",
                            )
                            continue
                        # A terminal cancel or a newer invocation can arrive while
                        # the audit call is outstanding. Its late result is inert.
                        if not _is_current_invocation(parser, batch_id, token):
                            return
                        if review["failed_sources"]:
                            diagnostic = next((
                                row.get("diagnostic") for row in review.get("sources", [])
                                if row.get("status") == "failed"
                            ), "语义检查失败。")
                            checkpoint_failed_unit(unit, str(diagnostic), review_result=review)
                            continue

                        if not any(row.get("section_ids") == unit and row.get("selection") is not None
                                   for row in completed_units):
                            # Existing adapters which don't checkpoint bounded
                            # units retain their original whole-section receipt.
                            completed_units.append({"section_ids": unit, "status": "completed", "review": review})
                        check_result = {
                            **check_result,
                            "units": completed_units,
                            "failed_units": failed_units,
                            "failed_section_ids": failed_section_ids(),
                            "unresolved_count": sum(
                                row["review"].get("uncertain_candidates", 0)
                                + row["review"].get("conflicted_candidates", 0)
                                + row["review"].get("protected_candidates", 0)
                                for row in completed_units
                            ),
                        }
                        if not _is_current_invocation(parser, batch_id, token):
                            return
                        parser._record_parse_stage(
                            batch, item, "check", "running", result=check_result,
                        )

                    if not _is_current_invocation(parser, batch_id, token):
                        return
                    if failed_units:
                        diagnostics = list(dict.fromkeys(
                            str(row.get("diagnostic") or "语义检查单元失败。")
                            for row in failed_units if isinstance(row, dict)
                        ))
                        parser._record_parse_stage(
                            batch,
                            item,
                            "check",
                            "failed",
                            result=check_result,
                            error="check_failed",
                            diagnostic="；".join(diagnostics)[:600],
                        )
                    else:
                        parser._record_parse_stage(
                            batch, item, "check", "completed", result=check_result,
                        )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if not _is_current_invocation(parser, batch_id, token):
                        return
                    superseded = isinstance(exc, ProseLifecycleEventSupersededError)
                    if superseded:
                        logger.info(
                            "knowledge parse batch %s source check was superseded", batch_id,
                        )
                    else:
                        logger.exception(
                            "knowledge parse batch %s source check failed", batch_id,
                        )
                    try:
                        parser._record_parse_stage(
                            batch,
                            item,
                            "check",
                            "failed",
                            error="superseded" if superseded else "check_failed",
                            diagnostic=f"check 阶段未完成：{str(exc)[:600]}",
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        # Cancellation, supersession, or an optimistic revision
                        # race can invalidate the failure checkpoint as well.
                        if _is_current_invocation(parser, batch_id, token):
                            logger.exception(
                                "knowledge parse batch %s source check failure checkpoint was rejected",
                                batch_id,
                            )

        await asyncio.gather(*(check_one(item) for item in ordered))
    finally:
        _clear_invocation_token(parser, batch_id, token)


__all__ = ["check_parse_batch_sources"]
