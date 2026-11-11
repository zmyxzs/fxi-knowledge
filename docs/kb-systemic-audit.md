# 知识库系统性复审

> 范围：`src/fxi_novel/knowledge` 306 个模块、`scripts/kb_server.py`、`archive.py`、`archive_extraction.py`、6 张 `kb_*` 表、20 个 HTTP 路由、189 个测试文件、19 份文档。
>
> 结论：当前主要问题不是“功能少”，而是**两套模型并存**，以及“缺什么加什么”导致的结构漂移。

---

## 1. 当前是“两套大脑”

```text
原文
 ├─ graph_parser → claims / typed events → asset projection / story register / FTS
 │                                      → /api/search、/api/story、/api/entity(legacy)
 │
 └─ memory mode → kb_entity / kb_entity_snapshot / kb_story_record
                → /api/compile_archive、/api/audit、/api/entity(archive 优先)
```

同一个问题，两个入口可能给出不同答案：

| 查询 | 读哪套 |
|---|---|
| `/api/search` | graph + FTS |
| `/api/story` | graph `query_story_knowledge` |
| `/api/entity` | 有 `kb_entity` 就用 archive，否则 graph |
| `/api/compile_archive` | archive |
| `/api/audit` | archive |
| `/api/story_record` | archive |

`entity()` 的 `if archived is not None: return ...` 是典型分裂点：一旦某作品跑过 memory，旧 dossier 就永久消失。

---

## 2. 真源矩阵

| 数据 | 现有 graph 真源 | archive 重复实现 | 冲突 |
|---|---|---|---|
| 实体 | identity group / `kb_entity`? | `kb_entity` | 同名不同 id；别名解析只走 archive |
| 事实 | graph claims / `TemporalRecordContent` | `kb_story_record(category=fact)` | 两份事实，无 cross-check |
| 资产余额 | `TypedEvent` + `asset_projection` | `resources` / `inventory_ledger` / `debts` / `inventory` | 四套账本 |
| 关系 | graph `category=relationship` | `cognition.relations`、`records` | 关系图 vs 文本列表 |
| 场景/线程 | graph events / temporal records | `story_record(scene/thread)` | 无 id 对齐 |
| 章节顺序 | `narrative_order` | `kb_source_meta.chapter_index` | 两套 as-of |
| 检索 | FTS / card index | `search_deconstruct` | 检索范围不同 |
| 审计 | `consistency_audit` | `audit_draft` | 规则不同 |

**最严重的是资产**：`state_update` 写 `inventory_ledger`，`memory` 写 `resources/debts/inventory`，`asset_projection` 又是第三套。

`_merge_ledger()` 只更新 `inventory_ledger.currency/items`，不会更新 `resources`；`audit_draft()` 合并时 `currency` 覆盖 `resources`。混合模式会直接算错余额。

---

## 3. 现有模型其实已经覆盖大部分需求

不需要再造 `kb_relation`。项目里已有：

### `StoryRegisterEntry`

```text
category: currency | inventory | skill | level | condition | identity | relationship | goal | rule
mode:     state | change | rule
event_kind: balance_snapshot | acquire | consume
amount / unit / quote / source / snapshot / interval
```

欠款可以映射为 `currency` 或 `inventory` + 借款变更；道具是 `inventory`；关系是 `relationship`；目标是 `goal`；规则是 `rule`。

### `TemporalRecordContent`

```text
record_type: fact | state | statement | observation | belief | identity_relation
predicate / value / state_slot
asserted_by / observed_by / holder_id / audience_ids / belief_state
evidence_basis / proposed_assessment
```

事实、状态、谁相信什么、谁知道什么，都已经有字段。

### `TypedEvent` + `asset_projection`

```text
kind: balance_snapshot | acquire | transfer | borrow | use | consume | ...
quantity / asset_id / holder
```

`borrow` 已经存在，`asset_projection` 明确写了“borrowing never silently changes ownership”。

### graph fact

```text
category: prop | event | location | attribute | ability | organization | relationship | rule | temporal_state | claim | concept | other
```

### 结论

欠款、道具、关系、因果**不应该新增 `debts` / `inventory` / `kb_relation` 三套结构**，而应该：

1. 把 memory 输出映射到上述既有模型
2. `kb_*` 只保留：作品画像、章节元数据、编译缓存、待审队列
3. 资产余额、实体快照、关系网全部由 projection 生成

---

## 4. 参数与结构散落

### 4.1 作品画像 11 个字段

```text
work_type, genre, world_summary, protagonist_name,
resource_units, entity_types, story_categories,
archive_modules, glossary, allow_debt
```

问题：

- 没有 `schema_version`
- 嵌套 JSON 没有统一 schema
- 默认值在 3–4 处重复

### 4.2 默认值重复

| 配置 | 重复位置 |
|---|---|
| `entity_types` | `archive.py` schema DEFAULT、`get_work_profile()`、`archive_extraction.DEFAULT_ENTITY_TYPES` |
| `story_categories` | schema DEFAULT、`get_work_profile()`、`DEFAULT_STORY_CATEGORIES` |
| `work_type` | schema DEFAULT、`get_work_profile()`、server 默认 |
| `archive_modules` | schema `'[]'`、`DEFAULT_ARCHIVE_MODULES` |
| `allow_debt` | schema `0`、default dict、server 校验 |

改一个默认值要改 3 处，容易漂移。

### 4.3 分类不一致

`story_categories` 默认不包含：

```text
fact, scene, thread
```

但 memory 模式照样写这三类，`apply_memory_output()` 只过滤 `records`，不过滤 `facts/scenes/threads`。结果是“分类配置”只约束了一部分数据。

### 4.4 `work_type` 目前没有行为

`kb_work_type` 只用于校验；注册 `fanfic` 后，解析、编译、检索、审计行为完全不变。它是“标签”，不是“工作流”。

---

## 5. 新发现缺陷

> 编号接现有 `docs/kb-defect-register.md` 的 D38。

### P0

| ID | 问题 | 证据 | 影响 |
|---|---|---|---|
| D39 | 两套真源并存 | `entity()` archive 优先；`search/story` 读 graph；`compile/audit` 读 archive | 同一问题答案不一致 |
| D40 | 混合模式账本冲突 | `state_update` 写 `inventory_ledger`；`memory` 写 `resources/debts/inventory` | 余额可能被旧值覆盖 |
| D41 | 预审丢失硬规则/红线 | `audit_draft(llm=true)` 已改为只跑 `state_changes` | 违反硬设定也可能 `passed=true` |

### P1

| ID | 问题 | 证据 | 影响 |
|---|---|---|---|
| D42 | 审计固定用主角账本 | `_audit_structured_changes()` 无视 `change.entity` | 其他角色消费误报/漏报 |
| D43 | `allow_debt` 直接豁免 | 超支时不要求存在 `debt` 变化 | 真错账被放过 |
| D44 | 长章截断 | `extract_memory_with_openai(text[:12000])` | 长章尾部漏抽 |
| D45 | 欠款只按债权人聚合 | `debts[creditor]` | 同人多种资源互相覆盖 |
| D46 | 道具只按名字聚合 | `inventory[item]` | 不同品质/实例合并 |
| D47 | 静默截断负数 | `max(0, current - quantity)` | 超扣/超还被吞 |
| D48 | 编译看不到资产/债务/关系 | 模块五只打印 `dynamic_state` JSON | 下一章上下文缺关键状态 |
| D49 | facts/scenes/threads 未归一 | 直接存原始名字 | 别名/重复/无生命周期 |
| D50 | `story_categories` 与 fact/scene/thread 不一致 | 默认列表不含这三类 | 配置形同虚设 |
| D51 | entity 两种响应结构 | archive 返回 vs legacy dossier | 客户端要分支 |
| D52 | 画像无 schema/version | 6 次 ALTER，无版本表 | 迁移不可追踪 |
| D53 | `work_type` 无行为 | 只做校验 | 注册无意义 |
| D54 | 模块 category 未校验 | `archive_modules` 可引用不存在的 category | 编译空模块 |
| D55 | `kb_story_record` 无索引 | 只有主键索引 | 编译随记录增长变慢 |

### P2

| ID | 问题 |
|---|---|
| D56 | `sources` / `story_record` 无分页 |
| D57 | OpenAPI 只有 11 个 path，实际 20 个 |
| D58 | README 测试数 1787/1780，实际 1839 |
| D59 | `examples/kb_client.py` 缺 work_profile/compile/audit_draft/story_record |
| D60 | 无 schema_version / 迁移记录 |
| D61 | memory 不发布图卡片/FTS，检索行为与 state_update 不同 |
| D62 | jieba 全局注册，跨作品污染 |
| D63 | 部分更新不能主动清空字段；`archive_modules=[]` 回退默认 |
| D64 | `parse_jobs.json` 全量重写 |
| D65 | `source_ids` 未校验属于该作品 |

---

## 6. 根因

1. **没有先定真源**：先加表、再加字段，最后两套模型都“能用”。
2. **没有契约**：memory/full parser/register/asset 四套 schema 各写各的。
3. **默认值分散**：schema、Python 默认、server 默认三处。
4. **测试只测函数**：没有“同一问题跨入口一致”的测试。
5. **文档靠手写**：OpenAPI、README、客户端与实现漂移。

---

## 7. 目标架构

```text
L0 原文/证据
L1 Claims：TemporalRecord / graph fact（事实、状态、关系、认知）
L2 Typed Events：资产流、事件、因果
L3 Projections：asset balance / story register / entity snapshot / FTS
L4 Compile & Audit：只读投影，不写自己的账本
L5 Config Registry：profile schema、默认值、work_type 能力
```

规则：

1. **一种事实只有一个 writer**
2. `kb_*` 只保留 profile、source meta、cache、review
3. 所有投影可重算
4. 每条事实必须有 evidence + source + chapter
5. 审计只读投影，不维护第二套账本

---

## 8. 迁移顺序

### Phase 0：修回归

- 恢复硬规则/红线检查
- 审计按 `change.entity` 取账本
- `allow_debt` 只在确实抽到 `debt` 时豁免
- 去掉 12000 静默截断，分块 + `TRUNCATED`

### Phase 1：单一资源真源

- 只保留 `resources`（或直接接 `asset_projection`）
- 删除 `inventory_ledger` 双写，保留只读兼容
- 欠款/道具改成 projection，不再单独存

### Phase 2：memory 输出接入既有模型

| memory | 映射 |
|---|---|
| fact | `TemporalRecordContent(record_type=fact)` |
| state | `TemporalRecordContent(record_type=state)` |
| resource | `TypedEvent(acquire/consume/balance_snapshot)` |
| debt | `TypedEvent(borrow)` + `asset_projection` |
| item | `TypedEvent(acquire/transfer/consume/loss)` |
| relationship | graph fact `category=relationship` 或 `StoryRegisterEntry(category=relationship)` |
| scene | temporal observation + participants |
| thread | `goal`/`rule`/待扩展的 thread 模型 |
| causality | typed event link 或 relation predicate `causes` |

### Phase 3：关系与因果

- 不再新增 `kb_relation`
- 关系用 graph fact / register 的 relationship
- 因果用 event link，避免把“因果”塞进文本字段

### Phase 4：配置治理

- 一个 `profile_schema.py` 定义所有字段、默认值、校验
- `schema_version` + 迁移表
- OpenAPI 由路由生成
- 客户端由 OpenAPI 生成

### Phase 5：一致性验收

- golden set：20–50 章
- 同一问题在 search/story/entity/compile/audit 的答案一致率 100%
- 0 双写
- 0 未索引热查询

---

## 9. 现在最该做的三件事

1. 修 D41/D42/D43（预审回归）
2. 修 D40（双账本）
3. 决定 archive 是 projection 还是独立真源；不能再两套都写

在这三件之前，继续加“欠款/道具/关系字段”只会扩大不一致面。
