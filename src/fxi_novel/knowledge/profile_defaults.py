"""Single source of defaults for work-profile and memory schemas."""

from __future__ import annotations

DEFAULT_WORK_TYPE = "writing"

DEFAULT_RESOURCE_UNITS: tuple[str, ...] = ()

DEFAULT_ENTITY_TYPES: tuple[str, ...] = (
    "character", "location", "item", "organization", "concept",
)

DEFAULT_STORY_CATEGORIES: tuple[str, ...] = (
    "power_and_laws", "map", "faction", "active_storyline", "foreshadow",
    "completed_storylines", "trope_skeleton", "style_anchor", "timer", "redline",
)

DEFAULT_ARCHIVE_MODULES: tuple[dict[str, str], ...] = (
    {"title": "模块一：题材与元规则", "kind": "meta"},
    {"title": "模块二：力量体系与金手指", "kind": "records", "category": "power_and_laws"},
    {"title": "模块三：地图与区域规则", "kind": "records", "category": "map"},
    {"title": "模块四：出场人物简表", "kind": "characters"},
    {"title": "模块五：主角高维面板", "kind": "protagonist"},
    {"title": "模块六：派系与势力", "kind": "records", "category": "faction"},
    {"title": "模块七：活跃故事线", "kind": "records", "category": "active_storyline"},
    {"title": "模块八：伏笔与认知盲区", "kind": "records", "category": "foreshadow"},
    {"title": "模块九：已完结故事线", "kind": "records", "category": "completed_storylines"},
)

DEFAULT_ALLOW_DEBT = False
