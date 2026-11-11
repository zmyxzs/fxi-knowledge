# 提示词融入知识库：设计与风险

> 2026-11-11 更新：当前执行约束见 [architecture-contract.md](architecture-contract.md)，需求状态见 [requirements-status.md](requirements-status.md)。本文保留设计背景与风险清单。

输入规格：

- `提取细纲.txt`：章节级剧情记忆
- `细纲转线网1/2.txt`：批次级故事线网络与二轮修正
- `长期档案更新.txt`：九模块长期档案增量维护

目标：把这些提示词作为知识库的**正式工件生成器**，而不是继续往 `kb_entity_snapshot` 里塞字段。

本轮已落地的基础设施：`kb_document` 版本化工件表 + `/api/documents`。

---

## 1. 三类工件

| 工件 | kind | scope_key | 说明 |
|---|---|---|---|
| 章节记忆 | `chapter_memory` | `chapter:12` | 一章一份，含场景因果、认知、Δ状态、未决、快照 |
| 长期档案 | `long_term_archive` | `main` | 全书一份，版本递增，active 唯一 |
| 线网 | `storyline_network` | `batch:1-10` | 一批一份，第一轮/第二轮各自版本 |

`kb_document` 已包含：

```text
work_id, kind, scope_key, version, status,
content_json, content_markdown, source_chapters,
prompt_name, prompt_version, model, schema_version, created_at
```

状态：`draft / reviewed / active / superseded`。  
同一 `(work, kind, scope_key)` 只能有一个 `active`；新 active 会把旧版本置为 `superseded`。

---

## 2. 数据流

```text
章节正文
  → chapter_memory prompt
  → JSON + Markdown 草稿
  → 校验（边界/场景数/证据/认知）
  → kb_document(active)
  → 投影到 claims / register / asset / snapshots
  → compile_archive 读取投影 + active 文档
```

长期档案：

```text
原 active 档案 + 新章节
  → 更新判定草稿（流转/增量/校验）
  → 新 long_term_archive 版本
  → 旧版本 superseded
```

线网：

```text
批次细纲
  → 第一轮 storyline_network draft
  → 下一批次细纲作为证据
  → 第二轮修正版 active
```

---

## 3. 与既有模型的映射

| 工件字段 | 既有模型 |
|---|---|
| `scenes[].causal_paragraph` | `TemporalRecordContent(record_type=observation)` 或文档正文 |
| `cognition[]` | `TemporalRecordContent(record_type=statement/observation/belief, holder_id, audience_ids, belief_state)` |
| `state_deltas[]` | `StoryRegisterEntry(category=currency/inventory/relationship/goal/rule/...)` |
| `resource/item/debt` | `TypedEvent` + `asset_projection` |
| `unresolved[]` | `goal/rule` 记录或独立 thread 模型 |
| `long_term_archive` 模块 | 生成文档；条目带 `authority/info_type/status/source_chapters` |
| `storyline_network` | 独立批次文档；后续可拆 `kb_storyline*` 表 |

关键原则：**文档是工件，claims/register 是结构化投影；不要反过来把文档当唯一真源。**

---

## 4. 会带来的问题

### 4.1 契约与输出

| 风险 | 影响 | 缓解 |
|---|---|---|
| 规格要求自然语言，系统需要 JSON | 无法直接校验/入库 | 双输出：JSON 结构 + Markdown；先校验 JSON，再渲染 Markdown |
| 模型不按 3–7 场景输出 | 过碎/过粗 | 场景数校验 + 合并/拆分重试 |
| 场景因果段缺要素 | 记忆退化 | 11 要素 checklist 校验，缺项标记而非静默通过 |
| 章节边界漂移 | 事件写到错章 | 每章独立调用；禁止跨章合并；边界片段校验 |
| 长章截断 | 尾部丢失 | 分块 + overlap + `TRUNCATED` 标记；禁止静默截断 |
| 认知/信息差写成上帝视角 | 角色知道不该知道的 | `holder/audience/belief_state` 必填；POV 校验 |
| 传闻/谎言被写成事实 | canon 污染 | `type` 枚举 + 证据；未确认进 review |
| 未来/预设信息提前写入 | 时间线泄漏 | `preset=true` 字段隔离；as-of 校验 |
| 线网要求不出现章节号 | 与内部 chapter_index 冲突 | 展示层用事件名；内部保留 anchor，渲染时剥离 |
| 二轮修正无证据改判 | 线网漂移 | 每条修正必须带下一批次事件锚点；无证据维持原判 |

### 4.2 数据与生命周期

| 风险 | 影响 | 缓解 |
|---|---|---|
| 文档与 claims 双写 | 两套真源 | 文档先生成，投影单向写入；投影失败可重放 |
| 长期档案 append 而非覆盖 | 动态状态叠加 | 档案按版本覆盖；条目带 `info_type=dynamic` 才允许替换 |
| 流转删除旧条目 | 信息丢失 | 流转不是删除：写 `status=archived` + `archived_to`，保留历史 |
| 编辑第 N 章后旧档案仍 active | 吃书 | `rollback_after_chapter` 同时 supersede `chapter>=N` 的文档与档案版本 |
| 同作品并发更新 | 版本冲突 | 写入前校验 `base_version`；冲突返回 409 |
| 版本爆炸 | 存储增长 | 只保留 active + 最近 N 个 superseded；定期归档 |
| source_chapters 不准 | 无法回溯 | 生成时由系统注入，不让模型填 |
| 文档无人审 | draft 当 canon | 默认 draft；只有 `reviewed/active` 参与编译 |

### 4.3 系统与成本

| 风险 | 影响 | 缓解 |
|---|---|---|
| 提示词很长 | token 成本高、延迟大 | 分阶段：章节记忆一次；档案更新一次；线网按批 |
| 原档案 + 新章超上下文 | 截断 | 档案分模块加载；只注入相关模块 |
| 2 vCPU 并发 | 长任务阻塞 | 已有 `FXI_KB_MAX_CONCURRENCY`；长任务走后台 job |
| 模型忽略 system prompt | 输出跑偏 | 已用 `_merge_system`；继续保留 |
| 输出非 JSON | 解析失败 | `json` 模式 + 一次修复重试 + 降级 draft |
| 无 golden set | 质量不可度量 | 20–50 章标注集；场景数/证据/认知/账本指标 |
| 检索不到文档 | 生成后无法用 | 文档分块进 FTS，或按 scope_key 精确取 |
| 4 MB 请求体 | 长档案无法一次写入 | 文档由服务端生成，不让客户端传全文 |
| 提示词注入（正文含指令） | 模型越权 | 正文放 user 消息、加“正文只是数据”约束；输出 schema 校验 |

### 4.4 迁移

| 风险 | 影响 | 缓解 |
|---|---|---|
| 旧 memory 数据格式不同 | 无法直接变文档 | 新旧并存；新工件从下一章开始 |
| 旧 archive_modules 与九模块不一致 | 编译错位 | 模块映射表；旧记录按 category 投影 |
| 旧账本 `inventory_ledger` 与新 `resources` | 余额不一致 | 读取统一优先级；迁移脚本一次性归一 |
| API 客户端不認識 documents | 无法使用 | 更新 OpenAPI/客户端；保留旧接口 |

---

## 5. 校验门

每个工件进入 `active` 前至少通过：

1. JSON schema 校验
2. `source_chapters` 与输入一致
3. 场景数 3–8（章节记忆）
4. 每条 evidence 在原文中逐字出现
5. 认知条目的 `holder/audience` 合法
6. 未发生/预设字段不进入事实字段
7. 长期档案跨栏无重复权威条目
8. 同作品同 scope 只有一个 active
9. 编辑/删除章节后，后续工件全部 superseded
10. 线网不出现章节号（渲染层检查）

---

## 6. 落地顺序

### Phase 0（已完成）

- `kb_document` 版本化存储
- `/api/documents` 读写
- prompt/model/schema 元数据

### Phase 1：章节记忆（本轮已完成）

- `mode=chapter_memory`
- 提示词：`prompts/learning/chapter-memory.md`
- 输出：`scenes / cognition / state_deltas / unresolved / snapshot`
- 校验：章节号、场景数 3–8、因果段长度、证据逐字、认知 POV、状态分类
- 写入 `kb_document(kind=chapter_memory, scope_key=chapter:N)`
- 无问题 `active`，有问题 `draft`

### Phase 2：投影

- `cognition` → `TemporalRecordContent`
- `state_deltas` → `StoryRegisterEntry`
- `resource/item/debt` → typed event / asset projection
- `compile_archive` 读取 active 章节记忆

### Phase 3：长期档案更新（本轮已完成）

- `mode=archive_update`
- 提示词：`prompts/learning/archive-update.md`
- 原 active 档案 + 新章 → `draft` + `archive`
- 校验：三阶段草稿、九模块、长度不明显缩水、无创作建议
- 写入 `kb_document(kind=long_term_archive, scope_key=main)`
- 新版本 active，旧 active 自动 superseded
- 版本冲突 409、回滚 supersede（下一轮补）

### Phase 4：线网（本轮已完成）

- `mode=storyline_network`
- 提示词：`prompts/learning/storyline-network.md`
- 第一轮：批次章节 → `network / assumptions / notes`
- 第二轮：`round=2` + `base_scope_key`，以下一批次为证据修正，输出完整版
- 校验：九节结构、出口一至八、无章节号、假设清单
- 写入 `kb_document(kind=storyline_network, scope_key=batch:N-M)`

### Phase 5：上下文组装（本轮已完成）

- `GET /api/context?work=&chapter=&max_chars=`
- 顺序：长期档案 → 上一章章节记忆 → 当前线网 → 编译档案
- 只读 active 文档
- 按字符预算裁剪，返回 `truncated` 与 `total_chars`

### Phase 6：审核与回滚（本轮已完成）

- 状态：`draft → reviewed → active`，可 `rejected`
- `POST /api/documents/review`，`action=accept|reject`
- `base_version` 过期返回 409
- 新 active 自动 supersede 旧 active
- 编辑/删除第 N 章时：
  - supersede `chapter>=N` 的章节记忆
  - supersede 长期档案、线网
- 回滚结果包含 `superseded_documents`

### Phase 7：投影与编译（本轮已完成）

- `chapter_memory.scenes` → `kb_story_record(category=scene)`
- `cognition` → `kb_story_record(category=cognition)`
- `unresolved` → `kb_story_record(category=thread)`
- `state_deltas`：
  - resource/item/debt → 现有账本/道具/欠款逻辑
  - character/relation/goal → `kb_story_record(category=state_delta)`
- 每条投影带 `source_document_id`；重新生成时删除旧投影，避免重复
- `archive_modules` 新增 `kind=chapter_memory`，编译时渲染场景/认知/线程/状态增量

### Phase 8：工件检索（本轮已完成）

- `kb_document_fts` 分块索引 `content_markdown`
- `GET /api/documents/search?work=&q=&kind=&limit=`
- 只检索 `status=active` 的工件
- `GET /api/documents?status=draft` 作为 review 队列
- 删除作品时同步清理 FTS

### Phase 9：投影状态与重放（本轮已完成）

- `kb_document.projection_status`：`pending / done / failed / not_applicable`
- `projection_error` 记录失败原因
- `POST /api/documents/reproject` 不重新调模型，只重放投影
- 回滚改为按来源章节精确失效：
  - `chapter_memory`：`chapter>=N`
  - `long_term_archive`：`source_chapters` 含 `>=N`，或无来源章节
  - `storyline_network`：批次结束章 `>=N`

### Phase 10：质量与接口收口（本轮部分完成）

- 离线评估器：`scripts/evaluate_chapter_memory.py`
- 指标：合法率、场景数、证据错误、认知/状态/线程错误
- OpenAPI 补齐 `/api/documents*`、`/api/context`、`/api/work_profile` 等
- `examples/kb_client.py` 补齐工件/上下文/审核/重放方法
- 契约测试：`tests/test_api_contract.py`
- golden set 仍需人工标注
- 文档审核 UI 未做

---

## 7. 验收指标

| 指标 | 目标 |
|---|---|
| 章节记忆 JSON 合法率 | ≥ 98% |
| evidence 逐字命中率 | ≥ 95% |
| 场景数合规率 | ≥ 90% |
| 未来信息泄漏 | 0 |
| 认知 POV 错误 | 0（golden set） |
| 长期档案跨栏重复 | 0（抽样） |
| 回滚后 active 文档残留 | 0 |
| 单章生成耗时 | ≤ 90 s |
| 单章成本 | 可配置上限 |

---

## 8. 结论

把提示词“融入知识库”不是把 25KB 提示词塞进 `memory` 模式，而是：

1. 三类工件各有版本、状态、来源
2. 先生成文档，再投影到 claims/register
3. 每个工件有校验门和回滚
4. 编译/检索只读 active 版本

`kb_document` 是本轮落地的基础；下一轮应先做 `chapter_memory`，不要同时上三类工件。
