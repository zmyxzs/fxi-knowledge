# 端到端验证记录：神域囚徒 313–315 章

- 日期：2026-10-24
- 环境：Windows，Python 3.11.15，`PYTHONPATH=D:\Projects\fxi-knowledge\src`
- 数据：`D:\文本\素材\神域囚徒！\正文` 中的 3 章
  - `0313 第313章 大品青龙劲.txt`
  - `0314 第314章 我考考你们.txt`
  - `0315 第315章 风神金刚腿，出神入化.txt`
- 数据库：`data/verify.sqlite3`（可复跑，已 gitignore）
- 查询产物：`data/verify-query.json`

## 1. 导入与切分

| 章节 | 字符数 | 分块 | section |
|---|---:|---:|---:|
| 0313 大品青龙劲 | 2736 | 4 | 1 |
| 0314 我考考你们 | 2602 | 4 | 1 |
| 0315 风神金刚腿，出神入化 | 3030 | 5 | 1 |

- `add_text_source` 直接接受整章文本，按段落切块并建 FTS。
- 词法检索（`search_planned`，`coverage_mode="focused"`）在导入后立即可用：
  查询「杨凌 拳」返回 5 条命中，命中片段正确落在三章内。

## 2. 解析与发布

### 装配

独立仓库没有写作系统的 narrative manifest，因此写作空间解析必须自备
`set_writing_narrative_order_reader`。验证时用了一个最小 stand-in（现已整理为
`examples/writing_order_reader.py` 的 `SourceOrderManifest`）：

- 按导入顺序给每章 `ordinal=1..N`、`chapter_order=2*ordinal`；
- `source_pin` 使用真实的 `snapshot_id` / `content_hash`，`artifact_*` 和 `event_id` 由 stand-in 生成；
- `manifest_status="verified"`、`complete=True`、`manifest_hash` 为 entries 的 SHA-256。

解析服务装配：

```python
stage_audit = KnowledgeParseStageAuditRunner(knowledge, gemini_gateway)
event = EventVectorReconciliationService(
    knowledge, deepseek_gateway,
    prompts / "knowledge-event-reconciliation.md",
    stage_audit_runner=deepseek_audit_runner,
)
parser = KnowledgeGraphParseService(
    knowledge, gemini_gateway,
    prompts / "knowledge-graph-parse.md",
    prompts / "knowledge-graph-coverage-audit.md",
    prompts / "knowledge-graph-identity-reconcile.md",
    prompts / "knowledge-graph-semantic-review.md",
    event_reconciliation=event,
    stage_audit_runner=stage_audit,
)
```

### 结果

- 批次状态：`completed`；3/3 来源的 `check`、`resolution`、`publish` 全部 `completed`。
- 耗时：465.5 秒；模型调用 26 次 Gemini（抽取/覆盖审计/语义复核/身份整理）+ 1 次 DeepSeek（事件整理）。
- 知识条目：**155** 条，全部写入 `published_graph_version_id`，`knowledge_graph_projection` 与 `knowledge_graph_fts` 各 155 行。

| 类别 | 数量 |
|---|---:|
| attribute | 39 |
| character | 26 |
| organization | 17 |
| prop | 16 |
| event | 15 |
| relationship | 11 |
| rule | 10 |
| temporal_state | 7 |
| claim | 6 |
| ability | 5 |
| location | 3 |

身份整理：75 个实体候选 → 63 个 identity group，合并 12 个实体，6/6 窗口完成。

抽取质量抽样：

- 杨凌：等级 80、基础力量 99 → 149（4 点自由属性）、精神力 200、修为判定下六品、综合战力中六品。
- 武技：大品青龙劲（一流内功）、风神金刚腿（熟练度 100 万，晋升出神入化）。
- 物品：气血太岁（增涨气血 3000、可服 3 次）、吞天金剑（上六品神兵）、蕴灵丹 4000 颗。
- 关系：杨凌持有气血太岁、杨凌与赵洵的交接事件、青云山首席弟子副本身份。

事件整理：6 个目标事件、15 对候选，其中 14 对被规则排除（`asset_granularity_conflict` / `event_kind_conflict`），1 对交给 DeepSeek 复核，最终 `partial`。

## 3. 查询

### 结构化枚举

`query_story_knowledge(purpose="work_fact", branch="main")` 返回 155 条，字段含证据偏移、原文引用、`claim_basis`、`semantic_assessment`。

### 自然语言问答

问题：「杨凌在第313到315章里修为、等级和武技发生了什么变化？」

规划（`KnowledgeQueryPlanner` + `query-plan.md`）：

- `intent=question`、`completeness=all_matching`、2 条 requirement
- 第 1 条：修为/等级变化，operations `state/events/compare`，查询词「杨凌 修为 突破」等
- 第 2 条：武技变化，operations `state/events/enumerate`，查询词「杨凌 武技 领悟」等
- 两条 requirement 都带同一时间约束（313–315 章）

执行（`KnowledgeQueryAssessmentService.execute_query`）：

- 56.2 秒，6 个操作，21 条证据
- 两条 requirement 均为 `supported`（14 条 / 8 条证据）
- 证据引用精确到原文，例如：
  - 「基础力量从95来到99点」
  - 「杨凌的基础力量本来是99点，眨眼间来到149点！」
  - 「杨凌把4点自由属性加在力量上」
  - 「【综合战力判定：中六品】」
  - 「【玩家杨凌，修为判定：下六品】」

检索（`search_planned`）：命中里出现知识条目文本，如「杨凌 · 基础力量 · 99点」。首次调用没有传 `branch`，`knowledge_cards` 为空；补传 `branch="main"` 后每个命中带 2–3 张卡片，例如：

- 「杨凌 · 等级 · 80级」「杨凌 · 连升等级 · 4 级」
- 「气血太岁 · 增涨气血 · 3000 点」「气血太岁 · 持有状态 · 杨凌持有」
- 「杨凌 · 基础力量数值 · 99 点」

## 4. 发现的问题

| 级别 | 问题 | 说明 |
|---|---|---|
| P1 | 写作空间解析依赖外部 narrative-order reader | 独立 checkout 没有 writer-owned manifest，直接解析会抛 `RuntimeError: writing narrative-order reader is not configured`。本次的 stand-in 已整理为 `examples/writing_order_reader.py`，接上即可。 |
| P1 | 事件整理硬性要求 DeepSeek | `event_reconciliation_review_execution` 校验 `provider_model` 含 "deepseek"；用 Gemini 时 resolution 必然失败（错误信息为 "requires DeepSeek"）。本次改为 Gemini 抽取 + DeepSeek 事件复核的混合装配。 |
| P2 | DeepSeek 抽取默认配置会耗尽输出预算 | 同一批章节用 DeepSeek 做初次抽取时 3/3 来源 `review_incomplete`。查 `knowledge_parse_unit_attempt` 发现 `finish_reason=length`、`completion_tokens=16000` 全部是 reasoning、正文为空。把 `reasoning_effort` 设为 `"none"` 后单章抽取 17 次调用、批次 `completed`（92 条知识、33 个身份组、4 对事件复核）。不是 schema 不兼容。 |
| P2 | 跨章节重复事件只部分合并 | 「杨凌进入吞天道宫副本」3 条、「风神金刚腿晋升出神入化」2 条、「杨凌升级80级」3 条仍并存；事件整理只复核了 1 对，其余被规则以粒度/类型冲突排除。 |
| P2 | `search_planned` 的 `knowledge_cards` 为空 | 不是缺陷：调用时没传 `branch`，`writing` 空间的卡片会按分支过滤掉。补传 `branch="main"` 后每个命中带 2–3 张卡片（如「杨凌 · 等级 · 80级」「气血太岁 · 增涨气血 · 3000 点」）。 |
| — | 项目级发布指针不存在（已澄清） | 发布只有 item 级 `published_graph_version_id`；`knowledge_project` 没有该列。此前把 purge 测试夹具缺列误判为项目指针缺失，夹具已修复（`d4ce404`）。 |
| P3 | 条目 `status` 仍为 `draft` | 已文档化：发布看 `published_graph_version_id` 指针，`status` 是生命周期字段（见 `docs/publication-semantics.md`）。 |

## 5. DeepSeek 抽取失败的真实原因

`knowledge_parse_unit_attempt` 里记录的是：

- `finish_reason: "length"`，`completion_tokens: 16000`，其中 `reasoning_tokens: 16000`
- `response_text` 为空，错误为 `InvalidChatResponse: chat provider returned empty text`

也就是推理把输出预算吃光，正文还没开始。把路由的 `reasoning_effort` 设为 `"none"`（网关对 DeepSeek 会发 `thinking: {"type": "disabled"}`）后重跑：

- 1 章、17 次模型调用、批次 `completed`
- 92 条知识（attribute 17 / event 14 / ability 13 / concept 10 / organization 9 / prop 8 / character 7 / temporal_state 6 / relationship 6 / rule 2）
- 33 个身份组，事件整理复核 4 对

所以这是输出预算配置问题，不是抽取契约与模型不兼容；DeepSeek 路由应关闭推理或显著提高 `max_output_tokens`。

## 6. 复跑步骤

1. 准备网关配置（不要把 key 提交）：一个 Gemini 兼容路由 + 一个 DeepSeek 路由（事件整理必须）。
2. 用 `Database("data/verify.sqlite3")` + `KnowledgeService` 建作品并导入章节。
3. 接上 `examples/writing_order_reader.py` 的 `SourceOrderManifest`（或实现等价的 `get_order_in_connection`，返回 verified manifest）。
4. `enqueue_parse_batch(space="writing", branch_id="main", ...)` 后 `await run_parse_batch(batch_id)`。
5. 查询：`query_story_knowledge` / `search_planned` / `KnowledgeQueryPlanner` + `KnowledgeQueryAssessmentService`。

## 7. 行为等价性检查（2026-10-25）

用 `git archive cec8887` 取出抽取时的快照，和当前 HEAD 各跑同一份 `data/verify.sqlite3` 副本，比较：

- 两条 `search_planned`（其中一条带 `entity_mentions=["杨凌"]`）的命中顺序、原文、分数、`retrieval_mode`、`knowledge_cards`
- `query_story_knowledge` 的 `total_count`
- `list_knowledge_items` 的 `total`

两边输出的 JSON **逐字节相同**（`items_total=155`，命中/分数/卡片一致）。

结论：本轮改动（打包、测试、lint、文档）没有改变知识库的运行时行为；抽取快照与当前 HEAD 在知识读取路径上等价。

## 8. 2026-10-25 DeepSeek 10 章复跑与系统修复

### 诊断：失败与模型差异无关

查 `knowledge_parse_stage_attempt` 后确认，DeepSeek 的失败不是输出预算问题（失败请求的 `completion_tokens` 只有 238–3340，远低于 16000），而是系统侧的三类问题：

1. **评审按来源整章打包**：单请求 30–58 KB，一次要求模型回几十行；漏行（C36/C41）、证据顺序错误、correction 附带非法字段都会让整组失败。
2. **身份整理请求过大**：单请求约 58 KB、最多 120 个候选，端点直接断连（`ReadError` / `response_not_received`）。
3. **校验与模型习惯冲突**：契约要求 `uncertain/conflicted/excluded` 不得携带 correction，但模型反复附带；repair 重试后仍失败，来源被整体标记失败。

### 修复

1. 评审前本地归一化（`normalize_raw_semantic_review_payload`）：
   - 非支持性结论（uncertain/conflicted/excluded）丢弃 correction；
   - correction 字段按角色白名单裁剪（实体只保留分类/粒度/描述等）；
   - 证据引用按来源 segment 顺序排序。
2. 身份分块从 120 候选降到 **48**，避免超大请求。
3. 对话引用校验与发布端对齐（见 `b1e63c5`）。

### 结果（10 章、DeepSeek、空库，最终）

- 113 次调用、1299 秒
- `check` **10/10**、`resolution` **10/10**、`publish` **10/10**
- **735 / 742 条知识已发布（99.1%）**，7 条 rejected
- 身份：263 个候选 → 161 个组，合并 102 个
- 事件整理：52 个目标事件、263 对候选
- 对话集：**10/10 published**

### 最后一处级联修复

`resolution` 曾对 9 个来源统一报 `event resolution attempt has an invalid validated response`。根因是事件整理校验把**失败的 attempt**（`status=failed`、无 `validated_response`）也当成 proof 遍历；一个瞬时失败就让整个 folder 的 resolution 归零。修复：校验只读取 `validated` 且带 `validated_response` 的 attempt（`repository_parse_event_attempt_validation.py`）。

### 问答层的兜底

10 章问答偶尔出现 `not_executed`：模型给出的 requirement 没有对应的 operation，执行器按契约只能标为未执行。修复：`_ensure_requirement_operations` 在计划生成后为每个缺少操作的 requirement 补一条 `semantic` 检索（优先用它的 `search_queries`，否则用描述）。同一问题连续 3 次复跑均为 2 条 requirement、全部 `supported`。
