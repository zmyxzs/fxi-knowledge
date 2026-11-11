# 四份规格覆盖审阅

输入：

- `提取细纲.txt`：章节级剧情记忆编码
- `细纲转线网1.txt` / `细纲转线网2.txt`：批次级故事线网络拆解与二轮修正
- `长期档案更新.txt`：九模块长期档案的增量维护

结论：**没有全部考虑进去。** 当前实现覆盖了“章节实体/状态/少量场景 + 九模块骨架 + 账本审计”，但四份规格里最核心的“完整因果记忆、认知信息差、故事线网络、长期档案增量更新”大部分缺失。

---

## 1. 覆盖总表

| 规格 | 当前状态 | 说明 |
|---|---|---|
| 章节边界 | 🟡 部分 | 有 `chapter_index` 和唯一约束，但抽取截断、无跨章校验 |
| 场景因果链 | 🟡 部分 | 有 `scenes.summary/participants/evidence`，不是完整因果段 |
| 认知与信息差 | 🔴 缺失 | 没有判断/传闻/谎言/认知更新/信息差/未知结构 |
| Δ状态 | 🟡 部分 | 有 resource/item/debt/state，缺人物/关系/目标分类 |
| 未决线程 | 🟡 部分 | 有 `threads + new/advanced/blocked/resolved`，无线程词典/生命周期 |
| 批次快照 | 🔴 缺失 | 没有“截至第 X 章当前状态快照” |
| 线网拆解 | 🔴 缺失 | 没有风格基调、马甲、并行线、耦合、爽点轮换、八维出口 |
| 二轮修正 | 🔴 缺失 | 没有假设清单与证据修正流程 |
| 长期档案增量更新 | 🔴 缺失 | `compile_archive` 只读生成，不是“原档案 + 新章 → 新档案” |
| 九模块骨架 | 🟢 已有 | 默认九模块可配置 |
| 结构化流转 | 🔴 缺失 | 没有活跃→归档、角色→关系网、地图→拓扑的自动流转 |
| 三类型分治 | 🔴 缺失 | 没有机制规则/动态状态/历史事件的类型标记 |
| 单一事实源 | 🔴 缺失 | 记录可跨模块重复，无权威归属校验 |
| 时态/预设隔离 | 🔴 缺失 | 没有“未发生/预设字段”与“已发生事实”的隔离 |
| POV 认知边界 | 🔴 缺失 | graph 层有 holder/audience/belief，memory/archive 没用 |
| 诊断式校验 | 🔴 缺失 | 没有条件清单式判定 |

---

## 2. `提取细纲.txt`

### 2.1 已覆盖

- 按来源章节存储，`kb_source_meta` 有章节号
- 实体、事实、场景、线程、资源/道具/欠款变化
- 证据字段与部分逐字校验
- 章节唯一约束

### 2.2 关键缺口

| 规格要求 | 当前实现 | 缺口 |
|---|---|---|
| 一条记录 = 一个完整场景/因果链 | `scenes[].summary` 一句话 | 缺“处境→选择→行动→阻碍→调整→回应→结果→新状态”完整段 |
| 每章 3–7 个场景，2000–3000 字 4–6 个 | 无数量校验 | 场景过碎/过粗无法发现 |
| `认知与信息差`：判断/传闻/谎言/认知更新/信息差/未知 | 无 | 无法表达谁知道什么、谁误判什么 |
| `Δ状态`：人物/资源/关系/目标 | 只有 resource/item/debt/state | 缺人物状态、关系、目标的分类增量 |
| `未决`：新增/推进/受阻/解决 + 稳定线程名 | `threads.transition` 接近 | 无线程别名、去重、生命周期 |
| 四份内部账本（事件/状态/认知/线程） | 无 | 抽取时没有分账本校验 |
| 章节输出为自然语言条目 | 输出 JSON | 缺 prose renderer |
| 批次当前状态快照 | 无 | 下一批无法直接接续 |
| 对话/心理/战斗/面板/直播/过渡的编码规则 | 无 | 这些内容被压成 facts/scenes |
| 跨章校验、数值/顺序校验 | 无 | 冲突无法定位 |
| 重复事件、原文矛盾、截断处理 | 无 | 只能靠人工发现 |
| 弱自包含、静态规则单点定义 | 无 | 容易重复或漏前情 |

---

## 3. `细纲转线网1/2.txt`

当前实现基本没有对应能力。

| 规格要求 | 当前 |
|---|---|
| 风格基调、主/次驱动力、读者契约 | 无 |
| 马甲体系（现实/伪装/他人脑补/网络身份） | 只有实体别名，不是马甲 |
| 并行故事线：功能命名、主辅、压强角色 | `active_storyline` 是平面记录 |
| 状态节点：自包含完整叙事段、五要素 | 无 |
| 线间耦合：在哪个事件相遇、各自升损 | 无 |
| 爽点类型轮换序列 | 无 |
| 八维功能出口快照 | 无 |
| 待验证假设清单 | 无 |
| 第二轮逐项证据修正、全量重出 | 无 |
| 全文不出现章节号，只用事件名 | 当前到处是 `chapter_index` |
| 输出九节结构 | 无 |

这不是“再加一个字段”能解决的，需要独立的批次级线网模型：

```text
kb_storyline(work, line_id, name, role, drive, status, style, ...)
kb_storyline_node(line_id, seq, event_name, narrative_paragraph, state_delta)
kb_storyline_link(from_line, to_line, event_name, gain, loss)
kb_storyline_exit(batch, dimension, content)
kb_storyline_assumption(batch, content, verify_with, impact)
```

或者作为独立 JSON/Markdown 工件，不必进 `kb_*` 表。

---

## 4. `长期档案更新.txt`

### 4.1 已覆盖

- 默认九模块骨架
- 模块一题材、模块二力量、模块三地图、模块六派系、模块七活跃线、模块八伏笔、模块九完结线的雏形
- 伏笔的“反派表象/主角盲区”格式化
- 模块可配置

### 4.2 关键缺口

| 规格要求 | 当前 |
|---|---|
| 三阶段：流转代谢 → 增量融入 → 校验 | `compile_archive` 只读拼接，没有更新流程 |
| 先输出【更新判定草稿】再输出档案 | 无 |
| 动态状态“以新换旧”，禁止叠加 | 快照按章追加，档案不覆盖 |
| 三类型：机制规则/动态状态/历史事件 | 无类型标记 |
| 单一事实源、跨栏去重 | 无 |
| 时态完整性、预设字段隔离 | 无 |
| POV 认知边界：作者/读者/角色已知分离 | 无 |
| 诊断式判定清单 | 无 |
| 角色流转：模块五→模块四 | 无 |
| 故事线流转：模块七→模块九 | 无 |
| 地图流转：副本→大地图拓扑 | 无 |
| 资产/伏笔代谢 | 只有数量归零，无代谢 |
| 模块四人物全集关系网 | 只有“本章匹配人物简表” |
| 模块五核心角色高维面板 | 只有主角 hard_rules + dynamic_state JSON |
| 模块六势力关系网 | 只有 faction 记录 |
| 模块七起承转合节点链 + 当前最新节点 | 只有 active_storyline 平面记录 |
| 模块九完结结算 | 只有 completed_storylines 平面记录 |
| 无字数/条数上限、禁止删减 | 存储无上限，但 prompt 截断 12000 字 |

---

## 5. 根本原因

当前 `memory` 模式是“轻量实体/状态抽取”，不是四份规格里的“剧情记忆编码器”；`compile_archive` 是“只读九模块拼接”，不是“长期档案维护器”；线网则完全没有模型。

三者被混在同一个 `kb_*` 层里，所以每加一个需求就多一个字段，无法形成稳定契约。

---

## 6. 建议的三份契约

### 6.1 章节记忆契约

```json
{
  "chapter_index": 3,
  "scenes": [{
    "name": "前女友分手",
    "causal_paragraph": "一段完整因果，不插标签",
    "participants": ["杨凌", "苏婉"],
    "evidence": "原文区间"
  }],
  "cognition": [{
    "holder": "杨凌",
    "type": "judgment|rumor|lie|update|info_gap|unknown",
    "content": "...",
    "audience": ["杨凌"],
    "evidence": "..."
  }],
  "state_deltas": [{
    "entity": "杨凌",
    "category": "character|resource|relation|goal",
    "field": "...",
    "from": "...",
    "to": "...",
    "evidence": "..."
  }],
  "unresolved": [{
    "thread": "正式身份",
    "transition": "new|advanced|blocked|resolved",
    "next_condition": "..."
  }],
  "snapshot": {}
}
```

映射：

- `cognition` → `TemporalRecordContent(record_type=statement/observation/belief, holder_id, audience_ids, belief_state)`
- `state_deltas` → `StoryRegisterEntry(category=currency/inventory/relationship/goal/rule/...)`
- `resource` → `TypedEvent` + `asset_projection`

### 6.2 长期档案契约

每条档案条目带：

```json
{
  "module": "五",
  "authority": "character_panel",
  "info_type": "rule|dynamic|history",
  "subject": "杨凌",
  "status": "active|archived|dead",
  "preset": false,
  "source_chapters": [1, 2, 3],
  "content": {}
}
```

更新流程固定为：

```text
流转代谢 → 增量融入 → 覆盖旧动态 → 校验（跨栏/时态/POV/完整性）
```

`compile_archive` 只负责渲染，更新由 `archive_update` 负责。

### 6.3 线网契约

独立批次工件：

```json
{
  "style": {...},
  "lines": [{"name": "...", "role": "main|sub|hidden", "nodes": [...]}],
  "links": [{"from": "...", "to": "...", "event": "...", "gain": "...", "loss": "..."}],
  "exits": {"relationship": "...", "rules": "...", "...": "..."},
  "assumptions": [{"content": "...", "verify_with": "...", "impact": "..."}]
}
```

线网输出用事件名，不出现章节号；内部仍可保留 chapter_index 做锚定。

---

## 7. 落地顺序

1. **P0**：章节记忆契约（scene/cognition/state_delta/unresolved/snapshot）
2. **P0**：长期档案更新（原档案 + 新章 → 草稿 + 新档案）
3. **P1**：九模块条目结构化（模块四/五/六/七/八/九）
4. **P1**：线网模型与二轮修正
5. **P2**：prose renderer、诊断式校验、跨章/数值/顺序检查

在 1、2 完成前，继续加 `debts`、`inventory`、`relations` 字段不会让系统更接近这四份规格。
