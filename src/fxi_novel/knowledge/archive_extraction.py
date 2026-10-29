"""Extract archive state updates from a chapter with an LLM.

The model returns one JSON object; the server applies it deterministically:
entity upserts, per-chapter snapshots and ledger arithmetic.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

SYSTEM_PROMPT = """你是小说档案抽取器。根据本章正文和已有实体表，输出本章结束时的状态变更。
只输出 JSON，结构如下：
{
  "entities": [
    {
      "name": "标准名",
      "aliases": ["本章出现的其他称呼"],
      "lifecycle_status": "active|dead|destroyed|exited",
      "dynamic_state": {"current_location": "...", "current_realm": "...", "physical_and_mental": "..."},
      "ledger_delta": {"currency": {"铜钱": -400}, "items_added": [], "items_removed": []},
      "cognition_and_relations": {"core_motivation": "...", "known_secrets": [], "cognitive_blind_spots": [], "relations": []},
      "hard_rules": {}
    }
  ],
  "story_records": [{"category": "active_storyline", "payload": {}}],
  "chapter_summary": "一句话"
}
规则：
- name 必须使用已有实体表中的标准名；本章新称呼写入 aliases。
- 只写本章发生变化的字段；没有变化就省略。
- ledger_delta 只写增减量，不要写最终余额；货币用整数。
- 死亡/损毁必须有明确结果描写，不能把“差点死了”当成死亡。
- hard_rules 只允许新实体填写；已有实体留空。
- story_records 可写 active_storyline / foreshadow / completed_storylines 等。"""


def build_messages(text: str, entities: list[dict], chapter_index: int) -> list[dict[str, str]]:
    context = {
        "chapter_index": chapter_index,
        "known_entities": [
            {
                "name": entity.get("canonical_name"),
                "aliases": entity.get("aliases", []),
                "entity_type": entity.get("entity_type"),
            }
            for entity in entities[:200]
        ],
        "chapter_text": text[:12000],
    }
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
    ]


def parse_output(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned[cleaned.find("{"): cleaned.rfind("}") + 1]
    data = json.loads(cleaned)
    if not isinstance(data, dict):
        raise ValueError("archive output must be an object")
    entities = data.get("entities")
    if entities is not None and not isinstance(entities, list):
        raise ValueError("entities must be a list")
    records = data.get("story_records")
    if records is not None and not isinstance(records, list):
        raise ValueError("story_records must be a list")
    return data


async def extract_with_openai(
    text: str,
    entities: list[dict],
    chapter_index: int,
    *,
    base_url: str,
    api_key: str,
    model: str,
    timeout: float = 120.0,
) -> dict:
    url = base_url.rstrip("/") + "/chat/completions"
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": build_messages(text, entities, chapter_index),
                "temperature": 0,
            },
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
    return parse_output(content)


DECONSTRUCT_PROMPT = """你是爆款小说拆书器。把本章还原成去名化的剧情骨架和文风锚点。
只输出 JSON：
{
  "trope_skeleton": {
    "steps": ["【剧情1】...", "【剧情2】..."],
    "conflict_tags": ["打脸", "扮猪吃虎"]
  },
  "style_anchor": {
    "scene_tags": ["题材", "场景功能"],
    "sanitized_text": "200~350字最具张力的原文片段，人名替换为[主角]/[反派A]/[配角B]，删除AI式微表情套话"
  }
}
要求：
- steps 6~8 步，覆盖本章完整事件链，只保留因果与冲突，不写章节名。
- 所有人名、地名、门派名替换为占位符，不要保留真名。
- style_anchor 必须逐字来自本章，只做去名化与套话删除，不改写情节。"""


def build_deconstruct_messages(text: str, chapter_index: int) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": DECONSTRUCT_PROMPT},
        {"role": "user", "content": json.dumps(
            {"chapter_index": chapter_index, "chapter_text": text[:12000]},
            ensure_ascii=False,
        )},
    ]


def parse_deconstruct_output(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned[cleaned.find("{"): cleaned.rfind("}") + 1]
    data = json.loads(cleaned)
    if not isinstance(data, dict):
        raise ValueError("deconstruct output must be an object")
    skeleton = data.get("trope_skeleton")
    if not isinstance(skeleton, dict) or not isinstance(skeleton.get("steps"), list):
        raise ValueError("trope_skeleton.steps is required")
    anchor = data.get("style_anchor")
    if not isinstance(anchor, dict) or not str(anchor.get("sanitized_text") or "").strip():
        raise ValueError("style_anchor.sanitized_text is required")
    return data


async def extract_deconstruct_with_openai(
    text: str,
    chapter_index: int,
    *,
    base_url: str,
    api_key: str,
    model: str,
    timeout: float = 120.0,
) -> dict:
    url = base_url.rstrip("/") + "/chat/completions"
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": build_deconstruct_messages(text, chapter_index),
                "temperature": 0,
            },
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
    return parse_deconstruct_output(content)


def apply_deconstruct_output(
    db: Any, work_id: str, chapter_index: int, output: dict,
) -> dict:
    from . import archive

    skeleton = output["trope_skeleton"]
    anchor = output["style_anchor"]
    records = [
        archive.add_story_record(
            db, work_id, "trope_skeleton",
            {
                "steps": [str(step) for step in skeleton.get("steps") or []],
                "conflict_tags": [str(tag) for tag in skeleton.get("conflict_tags") or []],
            },
            chapter_index=chapter_index,
        ),
        archive.add_story_record(
            db, work_id, "style_anchor",
            {
                "scene_tags": [str(tag) for tag in anchor.get("scene_tags") or []],
                "sanitized_text": str(anchor.get("sanitized_text") or ""),
            },
            chapter_index=chapter_index,
        ),
    ]
    return {
        "chapter_index": chapter_index,
        "trope_steps": len(skeleton.get("steps") or []),
        "style_anchor_chars": len(str(anchor.get("sanitized_text") or "")),
        "story_records": records,
    }


AUDIT_PROMPT = """你是小说设定审校。根据正文、实体硬设定、当前状态和红线，找出违规项。
只输出 JSON：
{"violations": [{"type": "HARD_RULE_CONFLICT|REDLINE_BREACH|DEAD_CHARACTER_ACTIVE|LEDGER_ERROR", "entity": "可选", "detail": "具体问题"}]}
规则：
- 硬设定（hard_rules）不得被正文改写；身份、外貌、性格底色、底线、口癖、功法机制冲突都要报。
- 红线（redlines）命中即报 REDLINE_BREACH。
- 已死亡/已损毁实体不得以活人方式行动。
- 账本支出不得超过当前余额。
- 没有问题返回 {"violations": []}。"""


def build_audit_messages(
    text: str, entities: list[dict], redlines: list[str], chapter_index: int,
) -> list[dict[str, str]]:
    context = {
        "chapter_index": chapter_index,
        "redlines": redlines,
        "entities": [
            {
                "name": entity.get("canonical_name"),
                "aliases": entity.get("aliases", []),
                "hard_rules": entity.get("hard_rules", {}),
                "dynamic_state": entity.get("dynamic_state", {}),
                "lifecycle_status": entity.get("lifecycle_status"),
            }
            for entity in entities[:80]
        ],
        "chapter_text": text[:12000],
    }
    return [
        {"role": "system", "content": AUDIT_PROMPT},
        {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
    ]


def parse_audit_output(text: str) -> list[dict]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned[cleaned.find("{"): cleaned.rfind("}") + 1]
    data = json.loads(cleaned)
    violations = data.get("violations") if isinstance(data, dict) else None
    if not isinstance(violations, list):
        raise ValueError("violations must be a list")
    return [
        {
            "type": str(item.get("type") or "HARD_RULE_CONFLICT"),
            "entity": item.get("entity"),
            "detail": str(item.get("detail") or ""),
        }
        for item in violations if isinstance(item, dict)
    ]


async def audit_with_openai(
    text: str,
    entities: list[dict],
    redlines: list[str],
    chapter_index: int,
    *,
    base_url: str,
    api_key: str,
    model: str,
    timeout: float = 120.0,
) -> list[dict]:
    url = base_url.rstrip("/") + "/chat/completions"
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": build_audit_messages(text, entities, redlines, chapter_index),
                "temperature": 0,
            },
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
    return parse_audit_output(content)


def _merge_ledger(previous: dict, delta: dict) -> dict:
    ledger = json.loads(json.dumps(previous or {}))
    currency = ledger.setdefault("currency", {})
    for name, amount in (delta.get("currency") or {}).items():
        try:
            value = int(amount)
        except (TypeError, ValueError):
            continue
        currency[name] = int(currency.get(name, 0)) + value
    items = ledger.setdefault("items", [])
    for item in delta.get("items_added") or []:
        if isinstance(item, str) and item not in items:
            items.append(item)
    for item in delta.get("items_removed") or []:
        if item in items:
            items.remove(item)
    return ledger


def apply_output(db: Any, work_id: str, chapter_index: int, output: dict) -> dict:
    from . import archive

    applied_entities = []
    for entry in output.get("entities") or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        existing = archive.get_entity(db, work_id, name)
        entity = archive.upsert_entity(
            db, work_id, name,
            aliases=[str(item) for item in entry.get("aliases") or []],
            entity_type=entry.get("entity_type"),
            role_tier=entry.get("role_tier"),
            hard_rules=entry.get("hard_rules") or None,
            overwrite_hard_rules=False,
        )
        previous = archive.get_snapshot(db, work_id, name, at_chapter=chapter_index)
        dynamic = dict(previous["dynamic_state"]) if previous else {}
        dynamic.update(entry.get("dynamic_state") or {})
        delta = entry.get("ledger_delta")
        if isinstance(delta, dict):
            dynamic["inventory_ledger"] = _merge_ledger(
                dynamic.get("inventory_ledger") or {}, delta,
            )
        cognition = dict(previous["cognition"]) if previous else {}
        incoming = entry.get("cognition_and_relations")
        if isinstance(incoming, dict):
            cognition.update(incoming)
        snapshot = archive.write_snapshot(
            db, work_id, entity["canonical_name"], chapter_index,
            lifecycle_status=str(entry.get("lifecycle_status") or "active"),
            dynamic_state=dynamic,
            cognition=cognition,
        )
        applied_entities.append({
            "name": entity["canonical_name"],
            "new": existing is None,
            "snapshot": snapshot,
        })

    records = []
    for record in output.get("story_records") or []:
        if not isinstance(record, dict) or not record.get("category"):
            continue
        records.append(archive.add_story_record(
            db, work_id, str(record["category"]), record.get("payload") or {},
            chapter_index=chapter_index,
        ))
    return {
        "chapter_index": chapter_index,
        "entities": applied_entities,
        "story_records": records,
        "summary": output.get("chapter_summary"),
    }
