"""LLM adjudication for consistency-audit review candidates."""

from __future__ import annotations

import json
from typing import Any

import httpx

VERDICTS = {"conflict", "change", "different", "insufficient"}

SYSTEM_PROMPT = """你是小说设定审校员。给定同一主体、同一属性在不同章节出现的多个值，判断它们属于哪一类：
- conflict：同一时点/同一状态下互相矛盾，无法用成长、阶段或不同对象解释
- change：随时间或阶段发生的正常变化（等级提升、伤势恢复等）
- different：其实是不同对象、不同技能或不同计量口径
- insufficient：证据不足，无法判断
只输出 JSON：{"verdict":"conflict|change|different|insufficient","reason":"不超过80字"}"""


def build_messages(entry: dict[str, Any]) -> list[dict[str, str]]:
    payload = {
        "subject": entry.get("subject"),
        "predicate": entry.get("predicate"),
        "values": entry.get("raw_values") or entry.get("values"),
        "sources": entry.get("sources"),
        "story_times": entry.get("story_times"),
    }
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def parse_verdict(text: str) -> dict[str, str]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned[cleaned.find("{"): cleaned.rfind("}") + 1]
    data = json.loads(cleaned)
    verdict = str(data.get("verdict") or "").strip()
    if verdict not in VERDICTS:
        raise ValueError(f"invalid verdict: {verdict!r}")
    return {"verdict": verdict, "reason": str(data.get("reason") or "")[:200]}


VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": sorted(VERDICTS)},
        "reason": {"type": "string"},
    },
    "required": ["verdict", "reason"],
    "additionalProperties": False,
}


async def adjudicate_entries(entries: list[dict], gateway: Any) -> list[dict]:
    results = []
    for entry in entries:
        try:
            _, verdict = await gateway.chat_validated(
                build_messages(entry),
                validator=lambda result: parse_verdict(result.text),
                json_mode=True,
                temperature=0,
                response_schema=VERDICT_SCHEMA,
            )
        except Exception as exc:  # noqa: BLE001 - fall back to a review verdict
            verdict = {"verdict": "insufficient", "reason": f"model error: {type(exc).__name__}"}
        results.append({**entry, **verdict})
    return results


async def adjudicate_entries_openai(
    entries: list[dict],
    *,
    base_url: str,
    api_key: str,
    model: str,
    timeout: float = 60.0,
) -> list[dict]:
    """Adjudicate through any OpenAI-compatible chat-completions endpoint."""
    url = base_url.rstrip("/") + "/chat/completions"
    results = []
    async with httpx.AsyncClient(timeout=timeout) as client:
        for entry in entries:
            try:
                response = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={
                        "model": model,
                        "messages": build_messages(entry),
                        "temperature": 0,
                    },
                )
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"]
                verdict = parse_verdict(content)
            except Exception as exc:  # noqa: BLE001 - fall back to a review verdict
                verdict = {"verdict": "insufficient", "reason": f"model error: {type(exc).__name__}"}
            results.append({**entry, **verdict})
    return results
