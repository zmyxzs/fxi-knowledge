"""Author style extraction contracts migrated from Fxi craft.dna.analyze.

Only storage adaptation belongs here; the learned voice and long excerpts stay intact.
"""

from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator

from fxi_novel.knowledge.writing_methods import StyleMethodFields


class StyleExcerptSelection(BaseModel):
    """Select a continuous passage from one supplied sample without reprinting it."""

    model_config = {"extra": "forbid"}
    sample_id: str = Field(min_length=1)
    start_paragraph: int = Field(ge=1)
    end_paragraph: int = Field(ge=1)


class StyleOperationExample(BaseModel):
    """One source passage and the part it plays in a learned operation."""

    model_config = {"extra": "forbid"}
    source_selection: StyleExcerptSelection
    role: Literal["support", "counterexample", "context"]
    material_kind: Literal["prose", "story_interface", "author_aside", "import_residue", "uncertain"]
    explanation: str = Field(min_length=10, max_length=500)


class StyleOperation(StyleMethodFields):
    """The existing knowledge method semantics, before source IDs are resolved."""

    examples: list[StyleOperationExample] = Field(default_factory=list, max_length=12)


class ScopedCraftMetadata(BaseModel):
    """Scope and provenance metadata emitted with every reusable craft rule."""

    model_config = {"extra": "ignore"}
    applicable_scenes: list[str] = Field(default_factory=list)
    forbidden_scenes: list[str] = Field(default_factory=list)
    applicable_phases: list[str] = Field(default_factory=list)
    exemptions: list[str] = Field(default_factory=list)
    retrieval_tags: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    source_selection: StyleExcerptSelection | None = None
    counterexample_selections: list[StyleExcerptSelection] = Field(default_factory=list, max_length=2)
    source_context: str = Field(default="", max_length=300)
    observation: str = Field(default="", max_length=500)
    technique: str = Field(default="", max_length=500)
    preserve: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(default_factory=list, max_length=6)
    scope_assessment: Literal["conditional", "source_bound"] = "conditional"
    effect: str = Field(default="", max_length=500)
    limitations: list[str] = Field(default_factory=list, max_length=5)
    source_specific_elements: list[str] = Field(default_factory=list, max_length=5)

    @field_validator("exemptions", mode="before")
    @classmethod
    def preserve_condition_reference(cls, value):
        # A completed response can express one exception as its condition and
        # source label. Keep both values verbatim in the existing text field;
        # the raw response remains in the extraction receipt.
        if not isinstance(value, list):
            return value
        return [json.dumps(row, ensure_ascii=False) if (
            isinstance(row, dict) and set(row) == {"condition", "evidence_sample"}
            and all(isinstance(part, str) for part in row.values())) else row for row in value]


class PerceptionFilterRule(ScopedCraftMetadata):
    """感知与心智滤镜：视点角色面对刺激时的本能反应底色与心理机制。"""

    model_config = {"extra": "ignore"}
    rule_name: str = Field(description="样本实际支持的视点、内心或叙述声音操作")
    mental_model: str = Field(description="心智模型与叙事声线特征")
    exemplar_quote: str = Field(default="", description="原文引文佐证")
    anti_patterns: list[str] = Field(default_factory=list, description="由当前样本支持的误用或例外")


class SceneDynamicsRule(ScopedCraftMetadata):
    """场景推进：行动、回应和信息如何改变当前情境。"""

    model_config = {"extra": "ignore"}
    cadence_type: str = Field(description="样本实际支持的场景推进方式")
    mechanism: str = Field(description="推进机制与技术拆解")
    exemplar_quote: str = Field(default="", description="原文引文佐证")
    anti_patterns: list[str] = Field(default_factory=list, description="禁忌模式")


class DialogueDynamicsRule(ScopedCraftMetadata):
    """Conditional dialogue choices, including ordinary conversation and subtext."""

    model_config = {"extra": "ignore"}
    tactic: str = Field(description="当前对白的表达或组织操作")
    subtext_principle: str = Field(description="潜台词原则（字面意与真实意图的落差）")
    exemplar_quote: str = Field(default="", description="原文对话片段")
    anti_patterns: list[str] = Field(default_factory=list, description="禁忌模式")


class VerbEngineRule(ScopedCraftMetadata):
    """物理动作推进与动词状态改变：从样本提炼动作表达与节奏层次。"""

    model_config = {"extra": "ignore"}
    tension_tier: str = Field(description="适用张力层级或场景条件")
    verb_principle: str = Field(description="动作驱动原则（如何通过动作改变状态）")
    exemplar_quote: str = Field(default="", description="原文动作推进片段")
    forbidden_habits: list[str] = Field(default_factory=list, description="样本支持的禁止习惯")
    anti_patterns: list[str] = Field(default_factory=list, description="禁忌模式")


class MacroOpeningRule(ScopedCraftMetadata):
    """宏观开篇格局与切入范式：从样本提炼场景建立与叙事动势。"""

    model_config = {"extra": "ignore"}
    rule_name: str = Field(description="开篇范式名称")
    opening_archetype: str = Field(default="", description="开篇原型与戏剧动势")
    mechanism: str = Field(description="生效机理（如何建立现场目标、约束与叙事动势）")
    exemplar_quote: str = Field(default="", description="来源owner读取的连续原文；保留旧逐字引文的恢复能力")
    anti_patterns: list[str] = Field(default_factory=list, description="样本支持的开篇禁忌")


class GoldenVoiceSlice(BaseModel):
    """A complete source interaction or action passage used as a voice reference."""

    model_config = {"extra": "ignore"}
    slice_type: str = Field(description="场景或功能类型，由样本与项目标签决定")
    title: str = Field(description="高光段落标题")
    quote: str = Field(default="", description="由来源 owner 读取的完整原文；旧已完成响应的逐字引文仍可恢复")
    source_selection: StyleExcerptSelection | None = None
    cadence_notes: str = Field(default="", description="原文的关系、压力、表达目的与句群作用")


class CliffhangerRule(ScopedCraftMetadata):
    """章尾如何结束当前行动、停顿或保留未解信息。"""

    model_config = {"extra": "ignore"}
    hook_type: str = Field(description="断章类型，由样本与项目标签决定")
    mechanism: str = Field(description="断章机制与心理停顿点")
    exemplar_quote: str = Field(default="", description="原文章尾的连续语境")
    anti_patterns: list[str] = Field(default_factory=list, description="由当前样本支持的误用或例外")


class DnaFingerprint(BaseModel):
    """Saved profile and historical six-dimension response read adapter."""

    model_config = {"extra": "ignore"}
    author_alias: str = Field(description="作者/风格代号")
    summary: str = Field(description="本次已读范围的完整风格学习正文；历史研究概览仍原样读取")
    operations: list[StyleOperation] = Field(default_factory=list)
    macro_opening: list[MacroOpeningRule] = Field(default_factory=list, description="宏观开篇范式与起手结构")
    cliffhanger_hooks: list[CliffhangerRule] = Field(default_factory=list, description="章尾断章与悬念推进律")
    voice_perception: list[PerceptionFilterRule] = Field(default_factory=list, description="感知与心智滤镜")
    scene_cadence: list[SceneDynamicsRule] = Field(default_factory=list, description="场景推进与压力律")
    dialogue_subtext: list[DialogueDynamicsRule] = Field(default_factory=list, description="对白表达、互动与潜台词")
    verb_engine: list[VerbEngineRule] = Field(default_factory=list, description="动词推进与张力分层")
    golden_voice_slices: list[GoldenVoiceSlice] = Field(default_factory=list, description="样本中的连续交流或动作范例")
    bone_verbs: list[str] = Field(default_factory=list, description="可选的来源动作词观察，不作为替换词表或配额")
    detected_entities: list[str] = Field(default_factory=list, description="在样本中检测到的具体人名/专有名词（用于实体隔离）")
    source_ref: str = Field(default="", description="输入样本的真实来源引用；无来源时保持为空")


class StyleSynthesisResponse(BaseModel):
    """The single current model response; aspect and scale come from evidence."""

    model_config = {"extra": "forbid"}
    author_alias: str
    summary: str
    operations: list[StyleOperation]
    golden_voice_slices: list[GoldenVoiceSlice] = Field(default_factory=list)


def _is_valid_craft_mechanism(title: str, text: str) -> bool:
    """Check a nonempty observation's structure, without claiming semantic validity."""
    t = (title or "").strip()
    m = (text or "").strip()
    return len(t) >= 2 and len(m) >= 6


def _merge_fingerprints(fps: list[DnaFingerprint], author_alias: str) -> DnaFingerprint:
    """Preserve distinct source conditions while merging exact duplicate observations."""
    summaries = [f.summary for f in fps if f.summary and f.summary != "无有效样本输入"]
    merged = DnaFingerprint(
        author_alias=author_alias,
        summary="；".join(dict.fromkeys(summaries)) if summaries else f"作者【{author_alias}】的来源写法观察",
    )
    seen_operations = set()
    for fingerprint in fps:
        for operation in fingerprint.operations:
            identity = json.dumps(operation.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
            if identity not in seen_operations:
                seen_operations.add(identity)
                merged.operations.append(operation)
    for field, title, technique in (
        ("macro_opening", "rule_name", "mechanism"),
        ("cliffhanger_hooks", "hook_type", "mechanism"),
        ("voice_perception", "rule_name", "mental_model"),
        ("scene_cadence", "cadence_type", "mechanism"),
        ("dialogue_subtext", "tactic", "subtext_principle"),
        ("verb_engine", "tension_tier", "verb_principle"),
    ):
        seen = set()
        for fingerprint in fps:
            for rule in getattr(fingerprint, field):
                identity = json.dumps(rule.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
                if identity not in seen and _is_valid_craft_mechanism(getattr(rule, title), getattr(rule, technique)):
                    seen.add(identity)
                    getattr(merged, field).append(rule)

    # 聚合真实黄金语流长段（确保每段真实保真）
    seen_slices = set()
    for f in fps:
        for s in f.golden_voice_slices:
            identity = (s.quote, json.dumps(s.source_selection.model_dump(mode="json"), sort_keys=True)
                        if s.source_selection is not None else None)
            if s.quote.strip() and identity not in seen_slices:
                seen_slices.add(identity)
                merged.golden_voice_slices.append(s)

    # 聚合骨力动词
    verbs = set(merged.bone_verbs)
    for f in fps:
        verbs.update(f.bone_verbs)
    merged.bone_verbs = sorted(list(verbs))

    all_entities = set()
    for f in fps:
        all_entities.update(f.detected_entities)
    merged.detected_entities = sorted(list(all_entities))
    return merged
