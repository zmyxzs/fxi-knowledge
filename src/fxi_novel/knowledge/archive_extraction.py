"""Extract archive state updates from a chapter with an LLM.

The model returns one JSON object; the server applies it deterministically:
entity upserts, per-chapter snapshots and ledger arithmetic.
"""

from __future__ import annotations

import json
import re
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
      "ledger_delta": {"currency": {"<资源名>": -400}, "items_added": [], "items_removed": []},
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
                "messages": _merge_system(build_messages(text, entities, chapter_index)),
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
                "messages": _merge_system(build_deconstruct_messages(text, chapter_index)),
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
                "messages": _merge_system(build_audit_messages(text, entities, redlines, chapter_index)),
                "temperature": 0,
            },
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
    return parse_audit_output(content)


def _merge_system(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    """Fold system prompts into the first user message for providers that ignore system."""
    system = [m["content"] for m in messages if m["role"] == "system"]
    others = [m for m in messages if m["role"] != "system"]
    if not system or not others:
        return messages
    return [
        {"role": "user", "content": "\n\n".join(system) + "\n\n" + others[0]["content"]},
        *others[1:],
    ]


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
            relations = incoming.get("relations")
            if isinstance(relations, list):
                merged = {
                    str(relation.get("target")): relation
                    for relation in (cognition.get("relations") or [])
                    if isinstance(relation, dict) and relation.get("target")
                }
                for relation in relations:
                    if isinstance(relation, dict) and relation.get("target"):
                        merged[str(relation["target"])] = relation
                incoming = {**incoming, "relations": list(merged.values())}
            cognition.update(incoming)
        lifecycle = entry.get("lifecycle_status") or (
            previous["lifecycle_status"] if previous else "active"
        )
        snapshot = archive.write_snapshot(
            db, work_id, entity["canonical_name"], chapter_index,
            lifecycle_status=str(lifecycle),
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
        payload = dict(record.get("payload") or {})
        if str(record["category"]) == "timer" and "created_chapter" not in payload:
            payload["created_chapter"] = chapter_index
        records.append(archive.add_story_record(
            db, work_id, str(record["category"]), payload,
            chapter_index=chapter_index,
        ))
    return {
        "chapter_index": chapter_index,
        "entities": applied_entities,
        "story_records": records,
        "summary": output.get("chapter_summary"),
    }
DEFAULT_ENTITY_TYPES = ("character", "location", "item", "organization", "concept")
DEFAULT_STORY_CATEGORIES = (
    "power_and_laws", "map", "faction", "active_storyline", "foreshadow",
    "completed_storylines", "trope_skeleton", "style_anchor", "timer", "redline",
)

MEMORY_PROMPT = """你是小说记忆抽取器。只输出一个 JSON 对象，不要解释、不要 Markdown。
格式：
{"entities":[{"name":"标准名","type":"__ENTITY_TYPES__","aliases":[]}],
 "facts":[{"entity":"实体名","field":"属性","value":"值","evidence":"原文短句"}],
 "state_changes":[{"entity":"实体名","kind":"resource|item|debt|state","field":"字段或资源名","direction":"increase|decrease|set|add|remove","amount":12,"quantity":1,"item":"物品名","creditor":"债主或对象","from":"旧值","to":"新值","evidence":"原文短句"}],
 "scenes":[{"summary":"一段完整因果","participants":["实体名"],"evidence":"原文短句"}],
 "threads":[{"name":"线程名","transition":"new|advanced|blocked|resolved","evidence":"原文短句"}],
 "records":[{"category":"__STORY_CATEGORIES__","payload":{}}]}
规则：只写原文明确事实；evidence 必须逐字出现；state_changes 只写本章明确变化；资源变化用 kind=resource，给出 direction（increase/decrease/set）和 amount（纯数字）；物品变化用 kind=item，给出 item、direction（add/remove/set）和 quantity；欠款用 kind=debt，给出 creditor 和 amount；普通状态用 kind=state；threads 只写跨章仍有影响的问题；records.category 必须是给定分类之一；没有就空数组。"""


def build_memory_messages(
    text: str,
    chapter_index: int,
    *,
    entity_types: list[str] | None = None,
    story_categories: list[str] | None = None,
) -> list[dict[str, str]]:
    types = "|".join(entity_types or DEFAULT_ENTITY_TYPES)
    categories = ", ".join(story_categories or DEFAULT_STORY_CATEGORIES)
    prompt = (
        MEMORY_PROMPT
        .replace("__ENTITY_TYPES__", types)
        .replace("__STORY_CATEGORIES__", categories)
    )
    return [
        {"role": "system", "content": prompt},
        {"role": "user", "content": json.dumps(
            {"chapter_index": chapter_index, "text": text[:12000]},
            ensure_ascii=False,
        )},
    ]


async def extract_memory_with_openai(
    text: str,
    chapter_index: int,
    *,
    base_url: str,
    api_key: str,
    model: str,
    entity_types: list[str] | None = None,
    story_categories: list[str] | None = None,
    timeout: float = 120.0,
) -> dict:
    url = base_url.rstrip("/") + "/chat/completions"
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": _merge_system(build_memory_messages(
                    text, chapter_index,
                    entity_types=entity_types,
                    story_categories=story_categories,
                )),
                "temperature": 0,
                "response_format": {"type": "json_object"},
            },
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
    return parse_output(content)


def _ledger_number(value: Any) -> int | None:
    text = str(value).strip().replace(",", "")
    match = re.match(r"^(-?\d+(?:\.\d+)?)", text)
    if match:
        return int(float(match.group(1)))
    chinese = re.sub(r"[^零一二三四五六七八九十百千万两]", "", text)
    if not chinese:
        return None
    digits = {
        "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
        "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
    }
    units = {"十": 10, "百": 100, "千": 1000, "万": 10000}
    total = 0
    section = 0
    number = 0
    for char in chinese:
        if char in digits:
            number = digits[char]
        elif char in units:
            unit = units[char]
            if unit == 10000:
                section = (section + number) * unit
                total += section
                section = 0
            else:
                section += (number or 1) * unit
            number = 0
    return total + section + number


def apply_memory_output(
    db: Any, work_id: str, chapter_index: int, output: dict,
    *, allowed_categories: list[str] | None = None,
) -> dict:
    from . import archive

    applied_entities = []
    for entry in output.get("entities") or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        entity = archive.upsert_entity(
            db, work_id, name,
            aliases=[str(item) for item in entry.get("aliases") or []],
            entity_type=entry.get("type"),
        )
        applied_entities.append(entity["canonical_name"])

    changes = []
    for change in output.get("state_changes") or []:
        if not isinstance(change, dict):
            continue
        name = str(change.get("entity") or "").strip()
        if not name:
            continue
        entity = archive.resolve_entity(db, work_id, name)
        if entity is None:
            entity = archive.upsert_entity(db, work_id, name)
        canonical = entity["canonical_name"]
        previous = archive.get_snapshot(
            db, work_id, canonical, at_chapter=chapter_index,
        )
        dynamic = dict(previous["dynamic_state"]) if previous else {}
        field = str(change.get("field") or "").strip()
        value = change.get("to")
        kind = str(change.get("kind") or "").strip()
        item = str(change.get("item") or "").strip()
        if field and value is not None:
            dynamic[field] = value
        if kind == "resource" and field:
            amount = _ledger_number(change.get("amount"))
            direction = str(change.get("direction") or "").strip()
            resources = dict(dynamic.get("resources") or {})
            ledger = dict(dynamic.get("inventory_ledger") or {})
            currency = dict(ledger.get("currency") or {})
            current = resources.get(field, currency.get(field))
            final: int | None = None
            if amount is not None and direction in {"increase", "decrease", "set"}:
                if direction == "set":
                    final = amount
                elif isinstance(current, int):
                    final = (
                        current + amount
                        if direction == "increase"
                        else current - amount
                    )
                else:
                    deltas = dict(dynamic.get("resource_deltas") or {})
                    deltas[field] = amount if direction == "increase" else -amount
                    dynamic["resource_deltas"] = deltas
            else:
                final = _ledger_number(value)
            if final is not None:
                resources[field] = final
                dynamic["resources"] = resources
                currency[field] = final
                ledger["currency"] = currency
                dynamic["inventory_ledger"] = ledger
                if value is None:
                    dynamic[field] = str(final)
        elif kind == "debt":
            amount = _ledger_number(change.get("amount"))
            direction = str(change.get("direction") or "").strip()
            key = str(change.get("creditor") or field or "unknown")
            debts = dict(dynamic.get("debts") or {})
            current = debts.get(key)
            final: int | None = None
            if amount is not None and direction in {"increase", "decrease", "set"}:
                if direction == "set":
                    final = amount
                elif direction == "increase":
                    final = (current if isinstance(current, int) else 0) + amount
                else:
                    final = max(0, (current if isinstance(current, int) else 0) - amount)
            if final is not None:
                debts[key] = final
                dynamic["debts"] = debts
                if not field:
                    dynamic["debt"] = final
        elif kind == "item" and item:
            quantity = _ledger_number(change.get("quantity"))
            if quantity is None:
                quantity = _ledger_number(change.get("amount"))
            if quantity is None:
                quantity = 1
            direction = str(change.get("direction") or "").strip()
            inventory = dict(dynamic.get("inventory") or {})
            current = inventory.get(item, 0)
            if direction == "add":
                inventory[item] = current + quantity
            elif direction == "remove":
                inventory[item] = max(0, current - quantity)
            elif direction == "set":
                inventory[item] = quantity
            dynamic["inventory"] = inventory
        archive.write_snapshot(
            db, work_id, canonical, chapter_index,
            lifecycle_status=str(
                change.get("lifecycle_status")
                or (previous["lifecycle_status"] if previous else "active")
            ),
            dynamic_state=dynamic,
        )
        changes.append({
            "entity": canonical, "kind": kind, "field": field,
            "direction": str(change.get("direction") or ""),
            "amount": change.get("amount"), "item": item,
            "quantity": change.get("quantity"), "to": value,
        })

    facts = [
        archive.add_story_record(db, work_id, "fact", fact, chapter_index=chapter_index)
        for fact in (output.get("facts") or []) if isinstance(fact, dict)
    ]
    scenes = [
        archive.add_story_record(db, work_id, "scene", scene, chapter_index=chapter_index)
        for scene in (output.get("scenes") or []) if isinstance(scene, dict)
    ]
    threads = [
        archive.add_story_record(db, work_id, "thread", thread, chapter_index=chapter_index)
        for thread in (output.get("threads") or []) if isinstance(thread, dict)
    ]
    allowed = {str(category) for category in (allowed_categories or [])}
    records = []
    for record in output.get("records") or []:
        if not isinstance(record, dict):
            continue
        category = str(record.get("category") or "").strip()
        if not category or (allowed and category not in allowed):
            continue
        records.append(archive.add_story_record(
            db, work_id, category, record.get("payload") or {},
            chapter_index=chapter_index,
        ))
    return {
        "chapter_index": chapter_index,
        "entities": applied_entities,
        "state_changes": changes,
        "facts": len(facts),
        "scenes": len(scenes),
        "threads": len(threads),
        "records": len(records),
    }
