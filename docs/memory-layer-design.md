# 记忆层设计：场景、认知、线程、线网与长期档案

> 状态：方案稿（不含实现）
> 范围：`提取细纲`、`细纲转线网 1/2`、`长期档案更新`
> 明确不做：`线网转脑洞`、`线网转新书细纲`（生成类，留在写作/编排层）

---

## 0. 结论

知识库现在存的是“原子事实 + 章节快照”。这三份提示词要求的是另一种东西：

- **场景**：一段完整的因果链，不是词条
- **认知**：谁在什么时间相信什么，不是客观事实
- **线程**：长期问题的生命周期，不是一次性事件
- **档案**：持续代谢的九模块文档，不是追加记录
- **线网**：批次级的版本化结构产物，不是逐条补丁

所以不能“加几个 category”，要加一层**记忆层**。

一句话原则：

> **LLM 负责写，数据库负责记、判、验；提案与正典分离；所有读取都有 as-of。**

---

## 1. 分层架构

```text
L0 正典层   work / source / entity / snapshot          （现有）
L1 场景层   scene                                      （新增）
L2 认知层   assertion                                  （新增）
L3 线程层   thread / thread_alias / thread_event       （新增）
L4 档案层   archive_document / archive_entry           （新增）
L5 产物层   artifact（chapter_memory / line_network）  （新增）
L6 治理层   run / conflict / glossary / review         （新增）
```

读取方向：上层可以引用下层，下层不感知上层。
写入方向：LLM 只写 draft，校验通过后由代码写 canon。

---

## 2. 数据模型

### 2.1 scene

| 字段 | 说明 |
|---|---|
| `id` | 主键 |
| `work_id` / `chapter_index` / `ordinal` | 定位；`(work, chapter, ordinal)` 唯一 |
| `title` | 场景名 |
| `causal_summary` | 一段完整因果链（散文，不拆标签） |
| `participants` | 实体 id 数组 |
| `location` | 地点 |
| `before_state` / `after_state` | 关键状态前后 |
| `evidence_quote` | 原文引用，必须能在本章正文中定位 |
| `source_id` / `as_of_chapter` / `run_id` | 溯源 |

索引：`(work, chapter)`、FTS(`causal_summary`)。

### 2.2 assertion

| 字段 | 说明 |
|---|---|
| `id` | 主键 |
| `work_id` / `subject` / `predicate` / `object` | 断言内容 |
| `status` | `fact / judgment / rumor / lie / cognition_update / unknown / info_gap` |
| `holder` | 谁相信/谁说；`fact` 为 null |
| `audience` | `author / reader / character` |
| `as_of_chapter` | 生效章节 |
| `revealed_at` | 对读者揭示的章节 |
| `supersedes_id` | 被哪条认知更新替代 |
| `scene_id` / `source_id` / `evidence_quote` / `run_id` | 溯源 |

约束：`supersedes` 链无环；`judgment` 只能被 `cognition_update` 替代，不能被前章“改成错误”。

### 2.3 thread

| 字段 | 说明 |
|---|---|
| `id` / `work_id` / `canonical_name` | 标准名，work 内唯一 |
| `status` | `new / advanced / blocked / resolved` |
| `opened_chapter` / `last_advanced_chapter` / `closed_chapter` | 生命周期 |
| `current_node` | 当前最新节点 |
| `history_json` | 历史节点 |
| `related_entity_ids` / `related_scene_ids` | 关联 |
| `priority` | 主线/支线/背景 |

`thread_alias(thread_id, alias)`：改名不新建线程。
`thread_event(thread_id, scene_id, transition, note)`：状态迁移审计。

### 2.4 artifact

| 字段 | 说明 |
|---|---|
| `kind` | `chapter_memory / line_network` |
| `range_start` / `range_end` | 章节范围（章节记忆为单章） |
| `round` | 线网为 1/2 |
| `version` / `parent_id` | 版本链 |
| `status` | `draft / accepted / superseded` |
| `content_json` | 结构化产物 |
| `input_hash` / `prompt_id` / `prompt_version` / `model` / `run_id` | 可复现 |

约束：同一 `(work, kind, range, round)` 的 `version` 递增；旧版本不可改。

### 2.5 archive_document / archive_entry

`archive_document`：

| 字段 | 说明 |
|---|---|
| `work_id` / `as_of_chapter` / `version` | 定位 |
| `status` | `draft / accepted / superseded` |
| `parent_id` / `input_hash` / `run_id` | 版本链与溯源 |

`archive_entry`：

| 字段 | 说明 |
|---|---|
| `document_id` / `module`(1–9) / `entry_key` | 唯一 |
| `entry_type` | `rule / dynamic / history / preset / archive` |
| `content` | 条目正文 |
| `status` | `active / archived` |
| `source_assertion_ids` | 来源断言 |
| `revision` | 条目修订号 |

模块归属表（单一事实源）：

| 模块 | 权威内容 |
|---|---|
| 1 | 题材与元规则（永驻） |
| 2 | 力量与规则体系 |
| 3 | 地图与副本 |
| 4 | 人物关系网（全集） |
| 5 | 核心角色高维面板 |
| 6 | 势力关系网 |
| 7 | 活跃故事线 |
| 8 | 伏笔与待回收 |
| 9 | 已完结故事线 |

### 2.6 治理表

- `run`：prompt 版本、模型、温度、输入 hash、token、耗时、错误
- `conflict`：`numeric / source_contradiction / continuity`，只记录不修正
- `glossary`：专名、别名、锁定标记
- `review`：提案、线程候选、档案 diff 的待审队列

---

## 3. 流程一：提取细纲 → 章节记忆

### 3.1 输入

- 本章正文（`content_raw`）
- 实体别名表 + 当前快照
- 线程词典（标准名/别名/状态）
- 专名表

### 3.2 输出（JSON sidecar + 散文）

```json
{
  "chapter_index": 12,
  "scenes": [
    {
      "ordinal": 1,
      "title": "马东纠正站桩",
      "causal_summary": "……一段完整因果链……",
      "participants": ["马东", "凌子阳"],
      "location": "武馆前院",
      "before_state": {},
      "after_state": {},
      "evidence_quote": "原文片段"
    }
  ],
  "assertions": [
    {"subject": "凌子阳", "predicate": "功法效率", "object": "下降20%",
     "status": "fact", "audience": "reader", "evidence_quote": "……"},
    {"subject": "马东", "predicate": "相信", "object": "姿势错误",
     "status": "judgment", "holder": "马东", "audience": "character", "evidence_quote": "……"}
  ],
  "delta_state": [
    {"entity": "凌子阳", "field": "current_realm", "from": "一层", "to": "一层(65/100)"},
    {"entity": "凌子阳", "ledger": {"currency": {"铜钱": -400}, "items_added": ["灵米"]}}
  ],
  "threads": [
    {"name": "月底站桩考核", "transition": "new", "related_entities": ["凌子阳"]}
  ],
  "conflicts": [
    {"kind": "numeric", "detail": "先称1000铜钱，后称1200", "evidence": "……"}
  ]
}
```

### 3.3 写入步骤

1. 校验 schema
2. 校验章节边界：`evidence_quote` 必须能在本章正文中找到
3. 解析实体/线程名：精确 → 别名 → 词法 → 候选
4. 写 `scene`、`assertion`
5. 应用 `delta_state`（代码做加减，不信模型算的余额）
6. 迁移 `thread` 状态，写 `thread_event`
7. 记录 `conflict`
8. 保存 `artifact(chapter_memory)`

### 3.4 问题与解法

| 问题 | 解法 |
|---|---|
| 跨章合并 | evidence 子串校验；scene.chapter 必须等于来源章 |
| 标签混乱 | 枚举校验；`判断·人物` 必须带 holder；无标记默认 fact |
| Δ 写成绝对值 | schema 要求 `from/to` 或 delta；绝对值拒绝 |
| 模型自算余额 | 只接受 delta，余额由代码算 |
| 线程重名 | 注入词典 + 匹配 + 候选评审 |
| 场景过碎/过粗 | 每场景必须有完整因果链；过短合并，过长拆分 |
| 长章节超上下文 | 分段抽取后合并；场景序号连续校验 |
| 代词指代 | 先做实体链接，输出必须用标准名 |
| 数值冲突 | 写 conflict，不修正 |
| 成本 | 单章一次调用；只注入相关实体/线程 |

---

## 4. 流程二：细纲转线网 1/2 → line_network

### 4.1 批次

- 固定窗口：`[start, end]`，默认 10 章
- 批次连续、不重叠
- 批次对象：`batch(work, start, end, status, source_ids)`

### 4.2 第一轮

输入：本批次章节记忆（不是原文）、上一批次已接受的线网快照、线程词典。
输出：完整结构

```json
{
  "range": [1, 10],
  "storylines": [
    {"id": "L1", "name": "站桩考核", "status": "active",
     "chapter_span": [1, 10], "nodes": ["N1", "N2"]}
  ],
  "engines": [{"name": "冲突碾压", "fuel_chain": ["..."]}],
  "reader_contract": {"promise": "...", "payoff": "..."},
  "style_baseline": {"tone": "...", "rhythm": "..."},
  "chapter_anchors": [{"node": "N1", "chapters": [3, 4]}]
}
```

### 4.3 第二轮

输入：第一轮完整版 + 下一批次章纲（**evidence_only**）。
输出：修正后的完整版，`round=2`，`parent=round1`。

### 4.4 问题与解法

| 问题 | 解法 |
|---|---|
| 边界移动 | `range` 写入 artifact；校验节点 `chapter_span ⊆ range`；提示词声明不可移动 |
| 未来信息泄漏 | 下一批次上下文标 `evidence_only`；校验节点证据只能来自本批次 |
| 输出章节号 | 内部 `chapter_anchors` 存锚点；渲染层剥离章节号 |
| 补丁式修正 | 每轮新版本，不原地改；diff 审阅 |
| 节点粒度漂移 | 节点必须有 id、名称、跨度、证据；粒度规则写进校验 |
| 引擎/风格主观 | 存为 proposal，接受后生效；记录依据 |
| 批次重叠 | 数据库约束 `[start,end]` 不重叠 |
| 跨批次不一致 | 上一批次 accepted 版本作为基线；diff 必须解释变化 |
| 与章节记忆冲突 | 交叉校验节点 claims vs scene/assertion |

---

## 5. 流程三：长期档案更新 → archive_document

### 5.1 输入

- 当前已接受的档案（as_of N-1）
- 第 N 章章节记忆（不是全文）
- 模块索引 + 受影响条目

### 5.2 三阶段

**阶段一：流转与代谢（代码判定）**

- 活跃故事线是否完结 → 7 → 9
- 角色是否死亡/退场/15 章无互动 → 5 收起，4 保留
- 地图是否离开且机缘清算 → 3 收起
- 伏笔是否回收 → 8 移除（归档）
- 消耗品是否耗尽 → 资产库更新

这些判定用 DB 查询（状态、章节、关联），不让模型通读全档案。

**阶段二：增量融入（LLM 只改受影响条目）**

- 新规则/新地图/新人物/新关系/新状态/新伏笔
- 动态状态“以新换旧”，禁止“原为 X 现为 Y”
- 预设字段兑现后迁移到事实字段

**阶段三：校验（代码）**

- 单一事实源：`(document, module, entry_key)` 唯一
- 动态条目无叠加表述
- 预设字段不含已发生事实
- 未发生事件不写事实语气
- 角色认知边界不混入上帝视角
- 条目只归档不删除
- 与 assertion 交叉核对

### 5.3 问题与解法

| 问题 | 解法 |
|---|---|
| 档案超上下文 | 只注入模块索引 + 受影响条目 + 模块 1/2 常驻 |
| 跨栏重复 | entry_key + 模块归属表 + 后处理去重 |
| 动态叠加 | 校验器拒绝“原为…现为…”模式 |
| 预设泄漏 | entry_type=preset，事实字段禁止写入 |
| 删除丢信息 | status=archived，永不物理删除 |
| 作者/读者/角色认知混淆 | assertion.audience + holder |
| 模型改写专名 | glossary 校验 |
| 档案与事实漂移 | 条目挂 source_assertion_ids，定期对账 |
| 更新不可回滚 | 每次生成新 document 版本，可回滚 |
| 评审负担 | diff 只展示新增/修改/归档，支持批量接受 |

---

## 6. 跨模块机制

### 6.1 as-of

- 所有记忆行带 `as_of_chapter`
- 所有查询/编译接受 `at_chapter`，默认最新
- 校验器：任何记录不得引用 `> at_chapter` 的事实
- 线网节点用 `chapter_anchors`，渲染不写章节号

### 6.2 版本与 diff

- artifact / archive_document 不可变
- 新版本 `parent_id` 指向旧版
- diff API：新增/删除/修改/归档
- 接受后才成为 canon

### 6.3 评审

- 线程候选、线网版本、档案 diff 进 `review`
- 状态：`pending / accepted / rejected`
- 拒绝保留理由，便于复盘

### 6.4 校验门（统一）

| 编号 | 校验 |
|---|---|
| V1 | JSON schema |
| V2 | 章节边界（evidence 可定位） |
| V3 | 未来信息泄漏 |
| V4 | 线程状态迁移合法 |
| V5 | 单一事实源 |
| V6 | 动态状态不叠加 |
| V7 | 预设字段隔离 |
| V8 | 专名不改写 |
| V9 | 数值冲突已记录 |
| V10 | 认知边界（holder/audience） |
| V11 | 死亡/账本/红线（现有） |

### 6.5 检索

- FTS：scene 摘要、thread 名称、archive 条目
- 图查询：实体→场景→线程→故事线
- 排序：as-of、实体命中、线程优先级、证据密度
- 向量检索后置，先解决结构与词法

### 6.6 任务编排

```text
chapter_memory（每章）
  → thread 更新
  → 每 K 章：line_network round1 → round2 → review
  → archive update（每章或每批）
  → 检索/编译
```

- 每作品一把锁
- 幂等键：`(work, chapter, prompt_version)`
- 失败只重跑失败阶段
- 进度与成本写 `run`

---

## 7. 与现有系统的关系

| 现有 | 处理 |
|---|---|
| `knowledge_item` 原子事实 | 保留；新 `assertion` 与其并存，`fact` 可回填 |
| `kb_entity_snapshot` | 保留；章节记忆的 delta 写这里 |
| `kb_story_record` | 保留为原始记录；长期档案迁移到 `archive_entry` |
| `compile_archive` | 优先读 `archive_document`；无档案时回退旧逻辑 |
| `audit` | 增加 V2/V3/V6/V7/V8/V10 校验 |
| `/api/parse` | 增加 `mode=chapter_memory`、`mode=line_network`、`mode=archive_update` |
| 词法检索 | 增加 scene/thread/archive 索引 |

迁移策略：只增不删；旧数据可回填为 `assertion(status=fact)`、`archive_entry(draft)`。

---

## 8. 反复审视：各模块问题清单

### 8.1 提取/场景

- 场景切分粒度不稳 → 规则 + 最小因果链长度校验
- 证据引用伪造 → 子串定位失败即拒绝
- 长章超上下文 → 分段 + 合并
- 代词/称号 → 实体链接前置
- 场景与事件重复 → scene 为主，event 作为结算

### 8.2 认知

- holder 缺失 → 强制字段
- 谎言与错误判断混淆 → 状态枚举 + 证据
- 信息差双向性 → `info_gap` 记录“谁知道/谁不知道”
- 读者揭示顺序 → `revealed_at`
- 上帝视角 → audience=author 单独标记

### 8.3 线程

- 重名/改名 → 注册表 + 别名
- 线程合并/拆分 → 候选评审，保留历史
- 长期无进展 → `blocked` 或归档
- 一次性小事 → 准入规则：必须跨章影响
- 线程优先级 → 主线/支线/背景

### 8.4 线网

- 边界漂移 → 范围不可变 + 校验
- 未来泄漏 → evidence_only
- 章节号 → 锚点分离
- 补丁 → 版本化
- 引擎/风格主观 → proposal + 依据
- 批次衔接 → 上版基线 + diff

### 8.5 档案

- 上下文 → 切片注入
- 重复 → 单一事实源
- 叠加 → 覆盖校验
- 预设 → 类型隔离
- 删除 → 归档
- 认知混淆 → audience
- 漂移 → assertion 对账
- 回滚 → 文档版本

### 8.6 实体/快照

- hard_rules 被改写 → 只读锁
- 别名分裂 → 注册表
- 关系单向 → 双向补全
- 快照继承 → 已有
- 回滚 → 已有

### 8.7 检索

- 长文本 → FTS + 摘要
- 线程查询 → 名称/别名/状态
- as-of → 过滤
- 排序 → 结构信号优先

### 8.8 编译

- 事实与指令混排 → 分区
- 未来信息 → as-of
- 模块顺序 → 固定
- 长度 → 分层：摘要 + 详情链接

### 8.9 审计

- 误报 → 分级 + 证据
- 语义检查成本 → 只对高风险段落
- 数值冲突 ≠ 写作错误 → 分开
- 审校不可复现 → 记录模型/prompt

### 8.10 任务/存储/API

- DAG 失败 → 阶段重试
- 并发 → 作品锁
- 幂等 → 键
- 数据膨胀 → 归档 + 压缩
- 迁移 → 只增不删
- 兼容 → 旧端点不变，新端点叠加

---

## 9. 分期

| 阶段 | 内容 | 验收 |
|---|---|---|
| P0 | schema、as-of、校验器、run | 设计评审通过 |
| P1 | chapter_memory（scene/assertion/thread） | 边界 0 违规、未来泄漏 0 |
| P2 | 线程注册表 + line_network 两轮 + diff | 批次衔接一致 |
| P3 | archive_document 三阶段 + 评审 | 单一事实源 0 违规 |
| P4 | 检索/编译/审计接入 | 编译不含未来信息 |
| P5 | 迁移与评估 | 回填、指标达标 |

---

## 10. 需要拍板的点

1. 批次大小 K（10 / 20 / 按卷）
2. 场景粒度（按场景 / 按因果链）
3. 章节记忆是否取代现有 parse 抽取，还是并存
4. 评审是人工必审，还是自动接受 + 抽样
5. 档案更新频率（每章 / 每批）
6. 旧 735 条事实是否回填为 assertion
7. 线网是全书一条，还是按卷/地图分段
