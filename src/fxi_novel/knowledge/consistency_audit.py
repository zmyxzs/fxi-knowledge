"""Deterministic consistency audit over published knowledge candidates."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any

_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}

_SINGLE_VALUED = (
    "等级", "力量", "敏捷", "精神", "精神力", "修为", "境界", "年龄",
    "性别", "生命值", "气血", "职业", "身份", "位置", "状态", "品级",
)


def _cn_to_number(text: str) -> float | None:
    total = 0
    current = 0
    seen = False
    for character in text:
        if character in _CN_DIGITS:
            current = _CN_DIGITS[character]
            seen = True
        elif character in _CN_UNITS:
            unit = _CN_UNITS[character]
            if not seen:
                current = 1
            total += (current or 1) * unit
            current = 0
            seen = True
        else:
            return None
    if not seen:
        return None
    return float(total + current)


def _numeric_value(text: str) -> float | None:
    cleaned = re.sub(r"\s+", "", text)
    match = re.search(r"-?\d+(?:\.\d+)?", cleaned)
    if match:
        return float(match.group())
    prefix = re.match(r"[零〇一二两三四五六七八九十百千]+", cleaned)
    if prefix:
        return _cn_to_number(prefix.group())
    return None


def _normalize_value(text: str) -> str:
    cleaned = re.sub(r"\s+", "", text)
    number = _numeric_value(cleaned)
    if number is not None:
        return f"num:{number:g}"
    return cleaned


def _is_single_valued(predicate: str) -> bool:
    return any(token in predicate for token in _SINGLE_VALUED)


def load_published_records(db: Any, work_id: str) -> list[dict]:
    with db.connection() as connection:
        rows = connection.execute(
            """
            SELECT version.content_json, source.title AS source_title
            FROM knowledge_item AS item
            JOIN knowledge_version AS version
              ON version.id = item.published_graph_version_id
            JOIN knowledge_parse_receipt AS receipt
              ON receipt.id = item.origin_parse_receipt_id
            JOIN source AS source ON source.id = receipt.source_id
            WHERE item.kind = 'knowledge_graph_candidate'
              AND item.published_graph_version_id IS NOT NULL
              AND item.status NOT IN ('deleted', 'rejected', 'superseded')
              AND source.work_id = ?
            """,
            (work_id,),
        ).fetchall()
    records = []
    for row in rows:
        try:
            content = json.loads(str(row["content_json"]))
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(content, dict):
            continue
        content["_source_title"] = row["source_title"]
        records.append(content)
    return records


def audit_records(records: list[dict]) -> dict:
    groups: dict[str, dict] = {}
    facts_by_key: dict[tuple[str, str], list[dict]] = defaultdict(list)
    entity_categories: dict[str, set[str]] = defaultdict(set)
    dangling: list[dict] = []

    for content in records:
        role = content.get("candidate_role")
        group_id = content.get("identity_group_id")
        category = content.get("category")
        if role == "entity":
            if group_id and category:
                entity_categories[group_id].add(str(category))
            continue
        if role != "fact" or not group_id:
            continue
        subject = str(
            content.get("identity_display_name") or content.get("subject")
            or content.get("entity_name") or ""
        )
        predicate = str(content.get("predicate") or "")
        if not predicate:
            continue
        groups.setdefault(group_id, {"subject": subject, "items": []})
        groups[group_id]["items"].append(content)
        if category in {"attribute", "ability", "rule", "temporal_state"}:
            facts_by_key[(group_id, predicate)].append(content)
        target_group = content.get("target_identity_group_id")
        if target_group and target_group not in entity_categories:
            dangling.append({
                "source": content.get("_source_title"),
                "subject": subject,
                "predicate": predicate,
                "target_group": target_group,
            })

    conflicts: list[dict] = []
    changes: list[dict] = []
    needs_review: list[dict] = []
    for (group_id, predicate), items in facts_by_key.items():
        if not _is_single_valued(predicate):
            continue
        by_chapter: dict[str, set[str]] = defaultdict(set)
        raw_values: list[str] = []
        for item in items:
            value = str(item.get("value") or "").strip()
            if not value:
                continue
            raw_values.append(value)
            by_chapter[str(item.get("_source_title") or "")].add(_normalize_value(value))
        values = set().union(*by_chapter.values()) if by_chapter else set()
        if len(values) < 2:
            continue
        times = {
            str(item.get("story_time_expression") or "").strip()
            for item in items
            if str(item.get("story_time_expression") or "").strip()
        }
        subject = groups.get(group_id, {}).get("subject") or group_id[:8]
        entry = {
            "subject": subject,
            "predicate": predicate,
            "values": sorted(values),
            "raw_values": sorted(set(raw_values)),
            "sources": sorted(by_chapter),
            "story_times": sorted(times),
        }
        same_chapter = {
            chapter: sorted(chapter_values)
            for chapter, chapter_values in by_chapter.items()
            if len(chapter_values) > 1
        }
        numeric = [_numeric_value(value) for value in raw_values]
        if same_chapter:
            entry["same_chapter_values"] = same_chapter
            conflicts.append(entry)
        elif times and len(times) >= 2:
            changes.append(entry)
        elif all(value is not None for value in numeric):
            changes.append(entry)
        elif all(len(value) <= 12 for value in raw_values):
            needs_review.append(entry)

    category_mismatches = [
        {"group": group_id, "categories": sorted(categories)}
        for group_id, categories in entity_categories.items()
        if len(categories) > 1
    ]

    return {
        "records": len(records),
        "conflicts": conflicts,
        "state_changes": changes,
        "needs_review": needs_review,
        "category_mismatches": category_mismatches,
        "dangling_targets": dangling,
    }


def audit_work(db: Any, work_id: str) -> dict:
    return audit_records(load_published_records(db, work_id))
