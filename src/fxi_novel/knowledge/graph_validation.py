"""Pure validation helpers for knowledge graph model output."""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import ValidationError

DIALOGUE_QUOTE_PATTERN = re.compile(
    r"“([^”\n]+)”|「([^」\n]+)」|『([^』\n]+)』|\"([^\"\n]+)\""
)


class IncompleteCoverageError(ValueError):
    """The response did not account for every supplied text segment."""


def validation_diagnostic(error: Exception) -> str:
    """Summarize rejection without returning model-generated field values."""
    if isinstance(error, json.JSONDecodeError):
        response = error.doc.lstrip()
        if not response:
            return "模型返回内容为空，无法解析 JSON"
        if response.startswith("```"):
            return "模型返回了 Markdown 代码围栏；接口要求纯 JSON 对象"
        if response[0] not in "[{":
            return f"模型响应不是 JSON 对象起始符（首字符 U+{ord(response[0]):04X}）"
        return f"模型返回的 JSON 语法无效（第 {error.lineno} 行，第 {error.colno} 列）"
    if isinstance(error, ValidationError):
        rows = error.errors(include_input=False, include_url=False)
        parts = [
            f"{'.'.join(str(segment) for segment in row['loc'])}: {row['msg']}"
            for row in rows[:8]
        ]
        if len(rows) > 8:
            parts.append(f"还有 {len(rows) - 8} 项字段错误")
        return "; ".join(parts)[:600] or "模型结果未通过结构校验"
    if isinstance(error, IncompleteCoverageError):
        return "模型没有声明覆盖全部输入段。"
    if isinstance(error, TypeError):
        return "模型输出字段类型不符合要求。"
    if isinstance(error, ValueError):
        dependency = re.fullmatch(r"depends on rejected (quantity Q[1-9][0-9]*|event V[1-9][0-9]*)", str(error))
        if dependency:
            return f"依赖的 {dependency[1]} 未通过校验；请先核对该记录的具体错误，保留有依据的原行。"
        mismatch = re.fullmatch(
            r"asset quantity mismatch: ((?:event_ref|quantity_ref|operation|asset_ref)"
            r"(?:, (?:event_ref|quantity_ref|operation|asset_ref))*)", str(error),
        )
        if mismatch:
            details = {
                "event_ref": "数量的 event_ref 与事件 ref 不一致",
                "quantity_ref": "数量与事件未形成唯一关联（事件侧反向引用由代码生成）",
                "operation": "数量的 operation 与事件 kind 不一致",
                "asset_ref": "数量的 asset_ref 与事件 asset_ref 不一致",
            }
            return "；".join(details[field] for field in mismatch[1].split(", ")) + "。按原文核对，不自动统一资产或操作。"
        safe_diagnostics = {
            "model response must be one isolated JSON code block": "模型返回的 JSON 代码围栏格式不完整。",
            "model response must contain a JSON object": "模型响应不是 JSON 对象。",
            "parser repeated a source-local entity reference": "模型给多个实体重复使用了同一个本地编号。",
            "fact references an unknown source-local entity": "事实引用了实体目录中不存在的主体编号。",
            "fact target references an unknown source-local entity": "关系引用了实体目录中不存在的目标编号。",
            "fact perspective references an unknown source-local entity": "说话者、观察者或听众引用了实体目录中不存在的编号。",
            "dialogue annotation refs must be unique": "对白标注重复引用同一个确定性 D# 片段。",
            "dialogue annotation identifies no deterministic utterance": "对白标注引用了当前原文中不存在的 D# 片段。",
            "dialogue speaker evidence does not match the pinned source snapshot": "说话者依据没有精确对应当前固定原文快照。",
            "dialogue narrative position differs from its pinned source": "对白位置与当前来源清单中的精确坐标不一致。",
            "dialogue overlap span does not cover the exact source utterance": "对白引用范围未覆盖所有对应原文片段。",
            "identified dialogue speaker cites an unknown evidence reference": "已识别说话者引用了当前解析单元中不存在的依据段。",
            "identified dialogue speaker has no source-local entity item": "已识别说话者没有对应的本来源人物实体。",
            "event_entity_ref must identify an event entity": "事件主键引用了非事件类别的实体。",
            "asset_ref must identify an asset or resource entity": "事件资产引用了非物品、能力或资源类别的实体；不会自动改写实体类别。",
            "event and asset entity granularity must agree": "事件中的资产实例/类型标记与实体目录不一致。",
            "typed asset events require an asset_ref": "资产变化事件缺少可定位的资产实体。",
            "participant cited an unknown source-local evidence unit": "未决参与者引用了当前解析单元中不存在的依据段。",
            "participant evidence unit does not match the pinned source snapshot": "参与者依据没有精确对应当前固定原文快照。",
            "participant mention is absent from its exact evidence": "参与者原文提及不在其引用的精确证据中。",
            "source-local typed reference is duplicated": "多个结构化记录重复使用同一个本地编号。",
            "event and quantity links are not reciprocal": "事件与数量记录的相互引用、资产或操作类型不一致。",
            "generic event measurement links are not reciprocal": "通用事件与测量记录未提供一致的双向引用；两条记录均保留为未通过项。",
            "generic event measurement links must be reciprocal and separate from asset quantities": "通用事件测量必须由事件和数量记录显式双向关联，不能混入资产数量字段。",
            "event measurement refs and supplied quantities must match exactly": "事件测量引用与嵌套数量记录未完全对应。",
            "event link references unknown event": "事件关系引用了本次输出中不存在的事件编号。",
            "quantity_ref points to unknown quantity": "事件引用了本次输出中不存在的数量记录。",
            "event_ref points to unknown event": "数量记录引用了本次输出中不存在的事件编号。",
            "condition references unknown event": "条件引用了本次输出中不存在的事件编号。",
            "condition references unknown rule": "条件引用了本次输出中不存在的规则编号。",
            "condition must reference an entity, event, or rule": "条件引用必须指向实体、事件或规则。",
            "typed instance subject requires instance granularity": "具体物品记录关联的实体没有标记为具体实例。",
            "an entity can have only one typed instance payload": "同一实体重复附加了具体实例记录。",
            "candidate cited an unknown source segment": "记录引用了本次输入中不存在的原文段。",
            "candidate quote is absent from the selected parse segments": "证据引文无法在本次选中的原文段中逐字定位；模型改写了引文或引用了未选章节。",
            "candidate quote occurs multiple times in its cited segment": "证据引文太短且在指定段中重复；应扩展为唯一的原文句子。",
            "candidate quote does not match the pinned source snapshot": "证据引文与当前固定的原文快照不一致。",
            "identity output referenced an unknown slot": "身份归并结果引用了本次输入中不存在的 slot。",
            "identity merge slots must be unique across groups": "同一候选 slot 被多个合并组重复引用。",
            "identity support slots must be merge members": "合并证据引用了不属于该合并组的候选。",
            "parser repeated an identical candidate": "模型重复输出了同一条事实候选。",
            "identity output must partition the current chapter candidates and supplied prior groups": "身份归并输出没有完整覆盖本次候选和已提供的先前身份组。",
            "identity groups cannot mix entity categories": "身份归并试图把不同类别的条目合并。",
            "prior groups may only be joined when current chapter evidence supports the merge": "身份归并试图合并先前身份组，但本次候选没有参与。",
            "same_name role must match the earliest chapter name": "同名角色与最早章节中的规范名称不一致。",
            "identity merge support must have source evidence": "身份合并缺少至少两条可定位的原文证据。",
            "each proposition must be an exact, unique source quote": "原子性审校的命题引文必须逐字且在原文段中唯一。",
            "propositions must follow their source order": "原子性审校的命题顺序与原文顺序不一致。",
            "replacement facts must cover every declared proposition exactly once": "复合事实拆分没有逐项覆盖原文命题。",
            "each replacement fact must cite its mapped proposition": "拆分事实的证据引文与对应原文命题不一致。",
            "semantic review cannot rewrite entity cards": "实体候选不可修正；请将 correction 设为 null，并用 assessment 标记排除或未决。",
            "facts linked to an AI-excluded entity must also be excluded": "若实体候选评为 excluded，本次审校中引用该实体的所有事实候选也必须评为 excluded。",
        }
        if str(error) in safe_diagnostics:
            return safe_diagnostics[str(error)]
        return "模型输出的实体关联或原文证据校验未通过。"
    return "模型输出未通过解析校验。"


def dialogue_utterances(segments: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    utterances: list[dict[str, Any]] = []
    by_range: dict[tuple[int, int, str], dict[str, Any]] = {}
    for segment in segments:
        segment_text = str(segment["text"])
        segment_offset = int(segment["start_offset"])
        for match in DIALOGUE_QUOTE_PATTERN.finditer(segment_text):
            quote_group = next(
                index
                for index, value in enumerate(match.groups(), start=1)
                if value is not None
            )
            raw_text = match.group(quote_group)
            utterance = raw_text.strip()
            if not utterance:
                continue
            leading_whitespace = len(raw_text) - len(raw_text.lstrip())
            start = segment_offset + match.start(quote_group) + leading_whitespace
            key = (start, start + len(utterance), utterance)
            representation = {
                "source_segment_ref": str(segment["ref"]),
                "evidence_span_id": str(segment["evidence_span_id"]),
                "segment_start": segment_offset,
                "segment_end": int(segment["end_offset"]),
            }
            existing = by_range.get(key)
            if existing is not None:
                if not any(
                    row["source_segment_ref"] == representation["source_segment_ref"]
                    and row["evidence_span_id"] == representation["evidence_span_id"]
                    for row in existing["representations"]
                ):
                    existing["representations"].append(representation)
                continue
            row = {
                "source_segment_ref": str(segment["ref"]),
                "evidence_span_id": str(segment["evidence_span_id"]),
                "start": start,
                "end": start + len(utterance),
                "text": utterance,
                "representations": [representation],
            }
            by_range[key] = row
            utterances.append(row)
    utterances.sort(key=lambda row: (row["start"], row["end"], row["text"]))
    for index, utterance in enumerate(utterances, start=1):
        utterance["ref"] = f"D{index}"
    return utterances


def resolve_segment_evidence_quote(
    segments: tuple[dict[str, Any], ...],
    cited_segment_ref: str,
    evidence_quote: str,
) -> tuple[dict[str, Any], int]:
    """Resolve an exact quote only against text supplied to this parse attempt."""
    segment_by_ref = {str(row["ref"]): row for row in segments}
    segment = segment_by_ref.get(cited_segment_ref)
    occurrences = str(segment["text"]).count(evidence_quote) if segment is not None else 0
    local_start = str(segment["text"]).find(evidence_quote) if occurrences == 1 else -1

    if occurrences == 0:
        matching_locations: dict[int, list[tuple[dict[str, Any], int]]] = {}
        for row in segments:
            row_text = str(row["text"])
            if row_text.count(evidence_quote) == 1:
                row_start = row_text.find(evidence_quote)
                absolute_start = int(row["start_offset"]) + row_start
                matching_locations.setdefault(absolute_start, []).append((row, row_start))
        if len(matching_locations) == 1:
            # Overlapping evidence spans can contain the same physical quote. Group by
            # absolute source offset, but never search outside the selected parse input.
            matches = next(iter(matching_locations.values()))
            segment, local_start = min(
                matches,
                key=lambda match: (
                    int(match[0]["start_offset"]),
                    int(match[0]["end_offset"]),
                ),
            )
            occurrences = 1

    if segment is None:
        raise ValueError("candidate cited an unknown source segment")
    if occurrences == 0:
        raise ValueError("candidate quote is absent from the selected parse segments")
    if occurrences > 1:
        raise ValueError("candidate quote occurs multiple times in its cited segment")
    return segment, local_start


def model_failure_diagnostic(error: Exception) -> str:
    """Return a useful, bounded reason without persisting response bodies or URLs."""
    cause = error.__cause__
    seen: set[int] = set()
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        response = getattr(cause, "response", None)
        status_code = getattr(response, "status_code", None)
        if isinstance(status_code, int):
            return f"模型网关返回 HTTP {status_code}。"
        cause_name = type(cause).__name__.casefold()
        if "timeout" in cause_name:
            return "模型网关请求超时。"
        if "connect" in cause_name:
            return "无法连接模型网关。"
        cause = cause.__cause__
    return "模型路由不可用或返回无效响应；请检查当前模型路由配置和模型服务日志。"


def load_model_json(text: str) -> dict[str, Any]:
    """Accept raw JSON or one isolated JSON code block, then validate its contents."""
    response = text.strip()
    if response.startswith("```"):
        lines = response.splitlines()
        if (
            len(lines) < 3
            or lines[0].strip().casefold() not in {"```", "```json"}
            or lines[-1].strip() != "```"
        ):
            raise ValueError("model response must be one isolated JSON code block")
        response = "\n".join(lines[1:-1]).strip()
    parsed = json.loads(response)
    if not isinstance(parsed, dict):
        raise ValueError("model response must contain a JSON object")
    return parsed
