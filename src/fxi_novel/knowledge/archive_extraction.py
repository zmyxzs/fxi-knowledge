"""Extract archive state updates from a chapter with an LLM.

The model returns one JSON object; the server applies it deterministically:
entity upserts, per-chapter snapshots and ledger arithmetic.
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

import httpx

from .profile_defaults import DEFAULT_ENTITY_TYPES, DEFAULT_STORY_CATEGORIES

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
CHAPTER_MEMORY_MAX_CHARS = 30_000
CHAPTER_MEMORY_SCENE_TYPES = {"judgment", "rumor", "lie", "update", "info_gap", "unknown"}
CHAPTER_MEMORY_STATE_CATEGORIES = {
    "character", "resource", "item", "debt", "relation", "goal",
}
CHAPTER_MEMORY_THREAD_TRANSITIONS = {"new", "advanced", "blocked", "resolved"}

_COGNITION_TYPE_ALIASES = {
    "判断": "judgment", "judgement": "judgment",
    "传闻": "rumor", "谣言": "rumor",
    "谎言": "lie", "谎话": "lie",
    "认知更新": "update", "更新": "update",
    "信息差": "info_gap", "信息缺口": "info_gap",
    "未知": "unknown", "不知道": "unknown",
}
_STATE_CATEGORY_ALIASES = {
    "人物": "character", "角色": "character", "状态": "character",
    "资源": "resource", "钱": "resource", "货币": "resource",
    "道具": "item", "物品": "item",
    "欠款": "debt", "债务": "debt",
    "关系": "relation",
    "目标": "goal",
}
_THREAD_TRANSITION_ALIASES = {
    "新": "new", "新增": "new", "开始": "new",
    "推进": "advanced", "进展": "advanced",
    "受阻": "blocked", "卡住": "blocked",
    "解决": "resolved", "完成": "resolved",
}


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _first(item: dict, keys: tuple[str, ...], default: Any = "") -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return value
    return default


def _normalize_quote(value: str) -> str:
    return "".join(
        char for char in value
        if not (
            char.isspace()
            or unicodedata.category(char)[0] in {"P", "Z", "S", "C"}
        )
    )


def normalize_chapter_memory_output(
    output: dict, chapter_index: int | None = None,
) -> dict:
    """Accept alternate keys and Chinese enum labels without losing data."""
    if not isinstance(output, dict):
        return {}
    data = dict(output)
    if chapter_index is not None and not data.get("chapter_index"):
        data["chapter_index"] = chapter_index
    scenes = _as_list(_first(data, ("scenes", "scene_list", "events", "场景"), []))
    normalized_scenes = []
    for scene in scenes:
        if not isinstance(scene, dict):
            continue
        normalized_scenes.append({
            **scene,
            "name": _first(scene, ("name", "title", "scene", "场景名")),
            "causal_paragraph": _first(
                scene,
                ("causal_paragraph", "causal", "summary", "description", "因果段"),
            ),
            "participants": _as_list(
                _first(scene, ("participants", "actors", "characters", "参与人"), [])
            ),
            "evidence": _first(scene, ("evidence", "quote", "原文")),
        })
    data["scenes"] = normalized_scenes
    cognition = _as_list(
        _first(data, ("cognition", "cognitions", "knowledge", "认知"), [])
    )
    normalized_cognition = []
    for item in cognition:
        if not isinstance(item, dict):
            continue
        raw_type = str(_first(item, ("type", "kind", "类型"), "unknown")).strip()
        normalized_cognition.append({
            **item,
            "holder": _first(item, ("holder", "character", "entity", "人物"), "未知"),
            "type": _COGNITION_TYPE_ALIASES.get(raw_type, raw_type),
            "audience": _as_list(
                _first(item, ("audience", "readers", "受众"), [])
            ),
            "evidence": _first(item, ("evidence", "quote", "原文")),
        })
    data["cognition"] = normalized_cognition
    deltas = _as_list(
        _first(data, ("state_deltas", "state_changes", "deltas", "changes", "状态变化"), [])
    )
    normalized_deltas = []
    for item in deltas:
        if not isinstance(item, dict):
            continue
        raw_category = str(
            _first(item, ("category", "kind", "类型"), "")
        ).strip()
        normalized_deltas.append({
            **item,
            "category": _STATE_CATEGORY_ALIASES.get(raw_category, raw_category),
            "entity": _first(item, ("entity", "character", "name", "人物"), ""),
            "evidence": _first(item, ("evidence", "quote", "原文")),
        })
    data["state_deltas"] = normalized_deltas
    threads = _as_list(
        _first(data, ("unresolved", "threads", "open_threads", "未决"), [])
    )
    normalized_threads = []
    for item in threads:
        if not isinstance(item, dict):
            continue
        raw_transition = str(
            _first(item, ("transition", "status", "状态"), "new")
        ).strip()
        normalized_threads.append({
            **item,
            "thread": _first(item, ("thread", "name", "title", "线程"), ""),
            "transition": _THREAD_TRANSITION_ALIASES.get(
                raw_transition, raw_transition,
            ),
            "next_condition": _first(
                item, ("next_condition", "next", "condition", "下一步"), "",
            ),
            "evidence": _first(item, ("evidence", "quote", "原文")),
        })
    data["unresolved"] = normalized_threads
    return data


def validate_chapter_memory_soft(
    output: dict, text: str, chapter_index: int,
) -> dict:
    """Hard errors block storage; everything else degrades to warnings."""
    hard: list[str] = []
    warnings: list[str] = []
    if not isinstance(output, dict):
        return {"output": {}, "hard": ["输出不是对象"], "warnings": [], "valid": False}
    if int(output.get("chapter_index") or 0) not in {0, int(chapter_index)}:
        warnings.append(
            f"chapter_index 不匹配：输出 {output.get('chapter_index')}，请求 {chapter_index}"
        )
    if len(text) > CHAPTER_MEMORY_MAX_CHARS:
        warnings.append(f"正文超过 {CHAPTER_MEMORY_MAX_CHARS} 字，尾部未参与抽取")

    def check_evidence(item: dict, label: str) -> None:
        quote = str(item.get("evidence") or "").strip()
        if not quote:
            warnings.append(f"{label} 缺少 evidence")
        elif quote in text:
            return
        elif _normalize_quote(quote) in _normalize_quote(text):
            warnings.append(f"{label} 的 evidence 仅归一化后匹配")
        else:
            warnings.append(f"{label} 的 evidence 未逐字出现：{quote[:40]}")

    scenes = output.get("scenes") if isinstance(output.get("scenes"), list) else []
    if not scenes:
        hard.append("scenes 为空")
    if len(scenes) < 3 or len(scenes) > 8:
        warnings.append(f"scenes 数量 {len(scenes)}，建议 3–8")
    for index, scene in enumerate(scenes):
        if not isinstance(scene, dict):
            warnings.append(f"scenes[{index}] 不是对象")
            continue
        if not str(scene.get("name") or "").strip():
            warnings.append(f"scenes[{index}] 缺少 name")
        if len(str(scene.get("causal_paragraph") or "").strip()) < 20:
            warnings.append(f"scenes[{index}] causal_paragraph 过短")
        if not isinstance(scene.get("participants"), list):
            warnings.append(f"scenes[{index}] participants 不是数组")
        check_evidence(scene, f"scenes[{index}]")

    for index, item in enumerate(output.get("cognition") or []):
        if not isinstance(item, dict):
            warnings.append(f"cognition[{index}] 不是对象")
            continue
        if not str(item.get("holder") or "").strip():
            warnings.append(f"cognition[{index}] 缺少 holder")
        if str(item.get("type") or "") not in CHAPTER_MEMORY_SCENE_TYPES:
            warnings.append(f"cognition[{index}] type 非标准")
        if not isinstance(item.get("audience"), list):
            warnings.append(f"cognition[{index}] audience 不是数组")
        check_evidence(item, f"cognition[{index}]")

    for index, item in enumerate(output.get("state_deltas") or []):
        if not isinstance(item, dict):
            warnings.append(f"state_deltas[{index}] 不是对象")
            continue
        category = str(item.get("category") or "")
        if category not in CHAPTER_MEMORY_STATE_CATEGORIES:
            warnings.append(f"state_deltas[{index}] category 非标准")
        if category == "resource" and (
            not str(item.get("field") or "").strip() or item.get("amount") is None
        ):
            warnings.append(f"state_deltas[{index}] resource 缺 field/amount")
        elif category == "item" and (
            not str(item.get("item") or "").strip() or item.get("quantity") is None
        ):
            warnings.append(f"state_deltas[{index}] item 缺 item/quantity")
        elif category == "debt" and (
            not str(item.get("creditor") or "").strip() or item.get("amount") is None
        ):
            warnings.append(f"state_deltas[{index}] debt 缺 creditor/amount")
        if not str(item.get("entity") or "").strip():
            warnings.append(f"state_deltas[{index}] 缺少 entity")
        check_evidence(item, f"state_deltas[{index}]")

    for index, item in enumerate(output.get("unresolved") or []):
        if not isinstance(item, dict):
            warnings.append(f"unresolved[{index}] 不是对象")
            continue
        if not str(item.get("thread") or "").strip():
            warnings.append(f"unresolved[{index}] 缺少 thread")
        if str(item.get("transition") or "") not in CHAPTER_MEMORY_THREAD_TRANSITIONS:
            warnings.append(f"unresolved[{index}] transition 非标准")
        check_evidence(item, f"unresolved[{index}]")
    return {
        "output": output,
        "hard": hard,
        "warnings": warnings,
        "valid": not hard,
    }


def _chapter_memory_prompt() -> str:
    from fxi_novel.project_paths import project_root

    path = project_root() / "prompts" / "learning" / "chapter-memory.md"
    return path.read_text(encoding="utf-8")


def parse_chapter_memory_output(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned[cleaned.find("{"): cleaned.rfind("}") + 1]
    data = json.loads(cleaned)
    if not isinstance(data, dict):
        raise ValueError("chapter memory output must be an object")
    return normalize_chapter_memory_output(data)


def validate_chapter_memory(
    output: dict, text: str, chapter_index: int,
) -> list[str]:
    issues: list[str] = []
    if int(output.get("chapter_index") or 0) != int(chapter_index):
        issues.append(
            f"chapter_index 不匹配：输出 {output.get('chapter_index')}，请求 {chapter_index}"
        )
    if len(text) > CHAPTER_MEMORY_MAX_CHARS:
        issues.append(f"正文超过 {CHAPTER_MEMORY_MAX_CHARS} 字，尾部未参与抽取")

    def evidence(item: dict, label: str) -> None:
        quote = str(item.get("evidence") or "").strip()
        if not quote:
            issues.append(f"{label} 缺少 evidence")
        elif quote not in text:
            issues.append(f"{label} 的 evidence 未在正文逐字出现：{quote[:40]}")

    scenes = output.get("scenes") or []
    if not isinstance(scenes, list) or not 3 <= len(scenes) <= 8:
        issues.append(f"scenes 数量 {len(scenes) if isinstance(scenes, list) else 0}，应为 3–8")
    for index, scene in enumerate(scenes if isinstance(scenes, list) else []):
        if not isinstance(scene, dict):
            issues.append(f"scenes[{index}] 不是对象")
            continue
        if not str(scene.get("name") or "").strip():
            issues.append(f"scenes[{index}] 缺少 name")
        paragraph = str(scene.get("causal_paragraph") or "").strip()
        if len(paragraph) < 40:
            issues.append(f"scenes[{index}] causal_paragraph 过短")
        if not isinstance(scene.get("participants"), list):
            issues.append(f"scenes[{index}] participants 必须是数组")
        evidence(scene, f"scenes[{index}]")

    for index, item in enumerate(output.get("cognition") or []):
        if not isinstance(item, dict):
            issues.append(f"cognition[{index}] 不是对象")
            continue
        if not str(item.get("holder") or "").strip():
            issues.append(f"cognition[{index}] 缺少 holder")
        if str(item.get("type") or "") not in CHAPTER_MEMORY_SCENE_TYPES:
            issues.append(f"cognition[{index}] type 非法")
        if not isinstance(item.get("audience"), list):
            issues.append(f"cognition[{index}] audience 必须是数组")
        evidence(item, f"cognition[{index}]")

    for index, item in enumerate(output.get("state_deltas") or []):
        if not isinstance(item, dict):
            issues.append(f"state_deltas[{index}] 不是对象")
            continue
        category = str(item.get("category") or "")
        if category not in CHAPTER_MEMORY_STATE_CATEGORIES:
            issues.append(f"state_deltas[{index}] category 非法")
        if category == "resource":
            if not str(item.get("field") or "").strip() or item.get("amount") is None:
                issues.append(f"state_deltas[{index}] resource 缺 field/amount")
        elif category == "item":
            if not str(item.get("item") or "").strip() or item.get("quantity") is None:
                issues.append(f"state_deltas[{index}] item 缺 item/quantity")
        elif category == "debt":
            if not str(item.get("creditor") or "").strip() or item.get("amount") is None:
                issues.append(f"state_deltas[{index}] debt 缺 creditor/amount")
        if not str(item.get("entity") or "").strip():
            issues.append(f"state_deltas[{index}] 缺少 entity")
        evidence(item, f"state_deltas[{index}]")

    for index, item in enumerate(output.get("unresolved") or []):
        if not isinstance(item, dict):
            issues.append(f"unresolved[{index}] 不是对象")
            continue
        if not str(item.get("thread") or "").strip():
            issues.append(f"unresolved[{index}] 缺少 thread")
        if str(item.get("transition") or "") not in CHAPTER_MEMORY_THREAD_TRANSITIONS:
            issues.append(f"unresolved[{index}] transition 非法")
        evidence(item, f"unresolved[{index}]")
    return issues


def render_chapter_memory(output: dict) -> str:
    chapter = output.get("chapter_index")
    lines = [f"# 第{chapter}章 剧情记忆"]
    for scene in output.get("scenes") or []:
        if not isinstance(scene, dict):
            continue
        lines.append(
            f"- [{scene.get('name') or '未命名场景'}]：{scene.get('causal_paragraph') or ''}"
        )
    lines.append("")
    lines.append("认知与信息差：")
    cognition = [
        item for item in (output.get("cognition") or []) if isinstance(item, dict)
    ]
    if cognition:
        for item in cognition:
            lines.append(
                f"- {item.get('type')}·{item.get('holder')}：{item.get('content') or ''}"
            )
    else:
        lines.append("- 无")
    lines.append("")
    lines.append("Δ状态：")
    deltas = [
        item for item in (output.get("state_deltas") or []) if isinstance(item, dict)
    ]
    if deltas:
        for item in deltas:
            detail = item.get("to") if item.get("to") is not None else item.get("amount")
            if item.get("item"):
                detail = f"{item.get('item')} {item.get('direction')} {item.get('quantity')}"
            lines.append(
                f"- {item.get('category')}·{item.get('entity')}：{item.get('field') or ''} {detail or ''}".rstrip()
            )
    else:
        lines.append("- 无")
    lines.append("")
    lines.append("未决：")
    unresolved = [
        item for item in (output.get("unresolved") or []) if isinstance(item, dict)
    ]
    if unresolved:
        for item in unresolved:
            lines.append(
                f"- [{item.get('thread')}·{item.get('transition')}] {item.get('next_condition') or ''}"
            )
    else:
        lines.append("- 无")
    return "\n".join(lines)


def _project_chapter_memory(
    db: Any, work_id: str, chapter_index: int, output: dict,
    document_id: str,
) -> dict:
    from . import archive

    with db.transaction() as connection:
        connection.execute(
            """
            DELETE FROM kb_story_record
            WHERE work_id=? AND chapter_index=?
              AND category IN ('scene','cognition','thread','state_delta')
              AND json_extract(payload_json, '$.source_document_id') IS NOT NULL
            """,
            (work_id, chapter_index),
        )
    counts = {"scene": 0, "cognition": 0, "thread": 0, "state_delta": 0}
    for scene in output.get("scenes") or []:
        if not isinstance(scene, dict):
            continue
        archive.add_story_record(
            db, work_id, "scene",
            {**scene, "source_document_id": document_id},
            chapter_index=chapter_index,
        )
        counts["scene"] += 1
    for item in output.get("cognition") or []:
        if not isinstance(item, dict):
            continue
        archive.add_story_record(
            db, work_id, "cognition",
            {**item, "source_document_id": document_id},
            chapter_index=chapter_index,
        )
        counts["cognition"] += 1
    for item in output.get("unresolved") or []:
        if not isinstance(item, dict):
            continue
        archive.add_story_record(
            db, work_id, "thread",
            {**item, "source_document_id": document_id},
            chapter_index=chapter_index,
        )
        counts["thread"] += 1
    state_changes = []
    for delta in output.get("state_deltas") or []:
        if not isinstance(delta, dict):
            continue
        category = str(delta.get("category") or "")
        if category in {"resource", "item", "debt"}:
            state_changes.append({**delta, "kind": category})
        else:
            archive.add_story_record(
                db, work_id, "state_delta",
                {**delta, "source_document_id": document_id},
                chapter_index=chapter_index,
            )
            counts["state_delta"] += 1
    if state_changes:
        apply_memory_output(db, work_id, chapter_index, {
            "state_changes": state_changes,
        })
    return counts


def apply_chapter_memory_output(
    db: Any, work_id: str, chapter_index: int, output: dict, *,
    text: str, prompt_name: str = "chapter-memory",
    prompt_version: str = "4", model: str | None = None,
) -> dict:
    from . import archive

    normalized = normalize_chapter_memory_output(output, chapter_index)
    check = validate_chapter_memory_soft(normalized, text, chapter_index)
    issues = check["hard"]
    warnings = check["warnings"]
    document = archive.save_document(
        db, work_id, "chapter_memory", f"chapter:{chapter_index}", normalized,
        markdown=render_chapter_memory(normalized),
        source_chapters=[chapter_index],
        status="active" if not issues else "draft",
        prompt_name=prompt_name,
        prompt_version=prompt_version,
        model=model,
        schema_version=1,
        projection_status="pending" if not issues else "not_applicable",
        warnings=warnings,
    )
    projection = {"scene": 0, "cognition": 0, "thread": 0, "state_delta": 0}
    projection_error = None
    if not issues:
        try:
            projection = _project_chapter_memory(
                db, work_id, chapter_index, normalized, document["id"],
            )
            archive.set_document_projection(db, document["id"], "done")
        except Exception as exc:  # noqa: BLE001 - retryable projection
            projection_error = f"{type(exc).__name__}: {exc}"
            archive.set_document_projection(
                db, document["id"], "failed", projection_error,
            )
        document = archive.get_document(
            db, work_id, "chapter_memory", f"chapter:{chapter_index}",
            version=document["version"],
        ) or document
    return {
        "chapter_index": chapter_index,
        "valid": not issues,
        "issues": issues,
        "warnings": warnings,
        "document": document,
        "projection": projection,
        "projection_error": projection_error,
        "scenes": len(normalized.get("scenes") or []),
        "cognition": len(normalized.get("cognition") or []),
        "state_deltas": len(normalized.get("state_deltas") or []),
        "unresolved": len(normalized.get("unresolved") or []),
    }


async def extract_chapter_memory_with_openai(
    text: str,
    chapter_index: int,
    *,
    base_url: str,
    api_key: str,
    model: str,
    timeout: float = 180.0,
) -> dict:
    url = base_url.rstrip("/") + "/chat/completions"
    messages = [
        {"role": "system", "content": _chapter_memory_prompt()},
        {"role": "user", "content": json.dumps(
            {"chapter_index": chapter_index, "text": text[:CHAPTER_MEMORY_MAX_CHARS]},
            ensure_ascii=False,
        )},
    ]
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": _merge_system(messages),
                "temperature": 0,
                "response_format": {"type": "json_object"},
            },
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
    return parse_chapter_memory_output(content)
ARCHIVE_UPDATE_MAX_CHAPTER_CHARS = 20_000
ARCHIVE_UPDATE_MAX_PREVIOUS_CHARS = 60_000
ARCHIVE_MODULE_TITLES = (
    "一、题材与元规则",
    "二、力量和规则体系",
    "三、地图与副本档案",
    "四、人物关系网",
    "五、核心角色档案",
    "六、势力关系网",
    "七、当前活跃故事线",
    "八、伏笔与待回收因果档案",
    "九、已完结故事线概要",
)


def _archive_update_prompt() -> str:
    from fxi_novel.project_paths import project_root

    path = project_root() / "prompts" / "learning" / "archive-update.md"
    return path.read_text(encoding="utf-8")


def parse_archive_update_output(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned[cleaned.find("{"): cleaned.rfind("}") + 1]
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        if start < 0:
            raise
        data, _ = json.JSONDecoder().raw_decode(cleaned[start:])
    if not isinstance(data, dict):
        raise ValueError("archive update output must be an object")
    for key in ("draft", "archive"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValueError(f"{key} must be a non-empty string")
    changes = data.get("changes")
    if changes is not None and not isinstance(changes, list):
        raise ValueError("changes must be a list")
    return data


def validate_archive_update(
    output: dict, previous_archive: str,
) -> list[str]:
    issues: list[str] = []
    draft = str(output.get("draft") or "")
    archive = str(output.get("archive") or "")
    if not draft.strip():
        issues.append("draft 为空")
    if not archive.strip():
        issues.append("archive 为空")
        return issues
    changes = output.get("changes")
    if changes is not None and not isinstance(changes, list):
        issues.append("changes 必须是数组")
    return issues


def archive_update_warnings(
    output: dict, previous_archive: str,
) -> list[str]:
    warnings: list[str] = []
    draft = str(output.get("draft") or "")
    archive = str(output.get("archive") or "")
    for phase in ("第一阶段", "第二阶段", "第三阶段"):
        if phase not in draft:
            warnings.append(f"draft 缺少 {phase}")
    normalized = archive.replace(" ", "").replace("\u3000", "")
    missing = [title for title in ARCHIVE_MODULE_TITLES if title not in normalized]
    if missing:
        warnings.append("archive 缺少模块：" + "、".join(missing))
    if previous_archive and len(archive) < len(previous_archive) * 0.5:
        warnings.append("archive 长度不足原档案一半，可能丢失内容")
    if "创作建议" in archive:
        warnings.append("archive 含创作建议")
    return warnings


def filter_archive_update_changes(
    changes: list[dict], state_delta_count: int | None = None,
) -> tuple[list[dict], list[str]]:
    valid: list[dict] = []
    warnings: list[str] = []
    for index, change in enumerate(changes):
        if not isinstance(change, dict):
            warnings.append(f"changes[{index}] 不是对象，已跳过")
            continue
        if change.get("source_delta") is not None:
            try:
                delta_index = int(change["source_delta"])
            except (TypeError, ValueError):
                warnings.append(f"changes[{index}] source_delta 非法，已跳过")
                continue
            if state_delta_count is not None and not (
                0 <= delta_index < state_delta_count
            ):
                warnings.append(
                    f"changes[{index}] source_delta={delta_index} 越界，已跳过"
                )
                continue
            valid.append(change)
            continue
        entry_key = str(change.get("entry_key") or "").strip()
        module = str(change.get("module") or "").strip()
        subject = str(change.get("subject") or "").strip()
        predicate = str(change.get("predicate") or "").strip()
        if entry_key or (module and subject and predicate):
            valid.append(change)
        else:
            warnings.append(
                f"changes[{index}] 缺少 source_delta/entry_key/subject，已跳过"
            )
    return valid, warnings


def apply_archive_update(
    db: Any, work_id: str, output: dict, *,
    source_chapters: list[int], previous_archive: str = "",
    prompt_name: str = "archive-update",
    prompt_version: str = "2", model: str | None = None,
) -> dict:
    from . import archive

    issues = validate_archive_update(output, previous_archive)
    warnings = archive_update_warnings(output, previous_archive)
    changes = output.get("changes") or []
    chapter_index = max(source_chapters or [0])
    memory = archive.get_document(
        db, work_id, "chapter_memory",
        f"chapter:{chapter_index}", status="active",
    )
    state_deltas = (
        (memory.get("content") or {}).get("state_deltas")
        if memory else None
    )
    state_delta_count = len(state_deltas) if isinstance(state_deltas, list) else 0
    valid_changes, change_warnings = filter_archive_update_changes(
        changes, state_delta_count,
    )
    warnings.extend(change_warnings)
    changes_applied = {"applied": 0, "archived": 0, "deleted": 0, "skipped": len(changes) - len(valid_changes)}
    if not issues and valid_changes:
        try:
            changes_applied = archive.apply_archive_changes(
                db, work_id, chapter_index, valid_changes,
                state_deltas=state_deltas if isinstance(state_deltas, list) else None,
            )
            changes_applied["skipped"] = len(changes) - len(valid_changes)
        except ValueError as exc:
            issues.append(f"changes 应用失败：{exc}")
    document = archive.save_document(
        db, work_id, "long_term_archive", "main", output,
        markdown=str(output.get("archive") or ""),
        source_chapters=source_chapters,
        status="active" if not issues else "draft",
        prompt_name=prompt_name,
        prompt_version=prompt_version,
        model=model,
        schema_version=1,
        warnings=warnings,
    )
    return {
        "valid": not issues,
        "issues": issues,
        "warnings": warnings,
        "document": document,
        "changes_applied": changes_applied,
    }


async def extract_archive_update_with_openai(
    previous_archive: str,
    chapters: list[dict],
    *,
    work_title: str = "",
    base_url: str,
    api_key: str,
    model: str,
    timeout: float = 300.0,
) -> dict:
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "work_title": work_title,
        "previous_archive": previous_archive[:ARCHIVE_UPDATE_MAX_PREVIOUS_CHARS],
        "new_chapters": [
            {
                "chapter_index": chapter.get("chapter_index"),
                "text": str(chapter.get("text") or "")[:ARCHIVE_UPDATE_MAX_CHAPTER_CHARS],
                "chapter_memory": chapter.get("chapter_memory"),
            }
            for chapter in chapters
        ],
    }
    messages = [
        {"role": "system", "content": _archive_update_prompt()},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": _merge_system(messages),
                "temperature": 0,
                "response_format": {"type": "json_object"},
            },
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
    return parse_archive_update_output(content)
STORYLINE_SECTIONS = (
    "一、拆解定位",
    "二、线网总览",
    "三、分线说明书",
    "四、纠缠与爆点逻辑",
    "五、爽点类型轮换序列",
    "六、角色线网位",
    "七、功能出口快照",
    "八、待验证假设与未决项清单",
    "九、分析备注",
)
STORYLINE_EXITS = tuple(f"出口{name}" for name in "一二三四五六七八")
STORYLINE_CHAPTER_PATTERN = re.compile(r"第\s*[0-9一二三四五六七八九十百千]+\s*章")


def _storyline_network_prompt() -> str:
    from fxi_novel.project_paths import project_root

    path = project_root() / "prompts" / "learning" / "storyline-network.md"
    return path.read_text(encoding="utf-8")


def parse_storyline_network_output(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned[cleaned.find("{"): cleaned.rfind("}") + 1]
    data = json.loads(cleaned)
    if not isinstance(data, dict):
        raise ValueError("storyline network output must be an object")
    if not isinstance(data.get("network"), str) or not data["network"].strip():
        raise ValueError("network must be a non-empty string")
    assumptions = data.get("assumptions")
    if assumptions is not None and not isinstance(assumptions, list):
        raise ValueError("assumptions must be a list")
    return data


def validate_storyline_network(
    output: dict, previous_network: str = "",
) -> list[str]:
    issues: list[str] = []
    network = str(output.get("network") or "")
    if not network.strip():
        issues.append("network 为空")
        return issues
    normalized = network.replace(" ", "").replace("\u3000", "")
    for section in STORYLINE_SECTIONS:
        if section not in normalized:
            issues.append(f"network 缺少章节：{section}")
    for exit_name in STORYLINE_EXITS:
        if exit_name not in normalized:
            issues.append(f"network 缺少 {exit_name}")
    if STORYLINE_CHAPTER_PATTERN.search(network):
        issues.append("network 出现章节号")
    if previous_network and len(network) < len(previous_network) * 0.5:
        issues.append("network 长度不足上一轮一半，可能丢失内容")
    assumptions = output.get("assumptions") or []
    if not isinstance(assumptions, list):
        issues.append("assumptions 必须是数组")
    return issues


def apply_storyline_network(
    db: Any, work_id: str, output: dict, *,
    scope_key: str, source_chapters: list[int],
    round_number: int = 1, base_scope_key: str | None = None,
    previous_network: str = "",
    prompt_name: str = "storyline-network",
    prompt_version: str = "1", model: str | None = None,
) -> dict:
    from . import archive

    issues = validate_storyline_network(output, previous_network)
    content = {
        **output,
        "round": round_number,
        "base_scope_key": base_scope_key,
    }
    document = archive.save_document(
        db, work_id, "storyline_network", scope_key, content,
        markdown=str(output.get("network") or ""),
        source_chapters=source_chapters,
        status="active" if not issues else "draft",
        prompt_name=prompt_name,
        prompt_version=prompt_version,
        model=model,
        schema_version=1,
    )
    return {
        "valid": not issues,
        "issues": issues,
        "document": document,
        "round": round_number,
    }


async def extract_storyline_network_with_openai(
    chapters: list[dict],
    *,
    base_network: str = "",
    evidence_chapters: list[dict] | None = None,
    work_title: str = "",
    base_url: str,
    api_key: str,
    model: str,
    timeout: float = 300.0,
) -> dict:
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "work_title": work_title,
        "batch_chapters": [
            {
                "chapter_index": chapter.get("chapter_index"),
                "text": str(chapter.get("text") or "")[:12_000],
            }
            for chapter in chapters
        ],
        "base_network": base_network[:60_000],
        "evidence_chapters": [
            {
                "chapter_index": chapter.get("chapter_index"),
                "text": str(chapter.get("text") or "")[:12_000],
            }
            for chapter in (evidence_chapters or [])
        ],
    }
    messages = [
        {"role": "system", "content": _storyline_network_prompt()},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": _merge_system(messages),
                "temperature": 0,
                "response_format": {"type": "json_object"},
            },
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
    return parse_storyline_network_output(content)
