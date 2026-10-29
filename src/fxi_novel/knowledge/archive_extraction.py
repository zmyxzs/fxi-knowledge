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
