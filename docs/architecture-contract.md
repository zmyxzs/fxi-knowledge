# 知识库统一架构与执行契约

状态：2026-11-11  
适用范围：`fxi-knowledge` 的章节记忆、长期档案、线网、检索与上下文组装。

这份文档不是功能清单，而是后续所有改动的约束。目标是避免再次出现：

- 两套读取源
- 同一事实重复写入
- 每本书加一条特判
- 模型一次做太多事
- 生产版本与工作区不一致

---

## 1. 目标与非目标

### 目标

1. 一个作品一个 `work_id`，所有查询按作品隔离。
2. 一份章节记忆只生成一次、投影一次、读取一次。
3. 长期档案和线网作为版本化工件，可审阅、可回滚。
4. 模型只做“理解与生成”，不做算术、版本、权限和状态机。
5. 失败可降级、可重放、可观测。

### 非目标

- 不追求所有模型输出都严格符合 schema。
- 不追求一次调用完成章节记忆 + 长期档案 + 线网。
- 不为单本书、单章节增加字段或规则。
- 不把 graph 和 archive 同时作为查询真源。

---

## 2. 三条链路

```text
写入链路
章节正文
  → chapter_memory 工件
  → 投影到 kb_story_record
  → 检索 / 上下文 / 审计

长期链路
原 long_term_archive + 新章节
  → archive_update 工件
  → 新 active 版本
  → /api/context 读取

批次链路
批次细纲
  → storyline_network 第一轮
  → 下一批次证据
  → 第二轮修正工件
```

读取链路只有三个入口：

```text
/api/story        → kb_story_record
/api/context      → active 工件 + compiled archive
/api/documents    → 工件版本与状态
```

禁止新增：

- 第二套 story 表
- graph 回退读取
- 按书特判的查询分支
- 按单章新增的 `kb_*` 字段

---

## 3. 不变量

### I1：作品隔离

所有业务查询必须带 `work`。  
多作品库中不带 `work` 返回 400，不允许隐式取第一个作品。

### I2：单一读取源

`/api/story` 只读 `kb_story_record`。  
旧 graph 数据通过 `scripts/backfill_graph_story.py` 一次性回填，不再在线回退。

### I3：工件与投影分离

- 工件：`kb_document`，保存模型生成的 JSON/Markdown。
- 投影：`kb_story_record`、账本、道具、快照。
- 读取：只读投影。

工件生成成功但投影失败时：

```text
projection_status = failed
projection_error = ...
```

文档仍保留，可重放，不参与读取。

### I4：active 唯一

同一 `(work, kind, scope_key)` 只能有一个 `active`。  
新 active 自动 supersede 旧版本。

### I5：写入幂等

同一作品、同一章节、同一分类、同一核心事实只保留一条。  
去重键必须包含 `chapter_index`，跨章重复是合法状态延续。

### I6：分类稳定

核心分类只保留：

```text
fact
scene
cognition
thread
state_delta
```

模块类记录必须带 `module`，不混入核心分类。  
graph 分类只放 `graph_category`，不参与主分类。

### I7：分页完整

任何返回 `total` 的列表接口必须支持分页。  
当前使用 `offset/next_offset`；后续数据量增大后改为 cursor/keyset。

### I8：回滚一致

编辑/删除第 N 章时：

- 删除 `chapter_index >= N` 的快照与故事记录
- supersede `chapter >= N` 的章节记忆
- supersede 受影响的长期档案与线网

不允许只删一部分。

### I9：warning 不阻断

运行时只有“无法投影”才 hard fail：

- 输出不是对象
- `scenes` 为空
- JSON 无法解析

其余问题进 `warnings_json`，不阻断 active 与投影。

---

## 4. 冲突处理

| 冲突 | 规则 |
|---|---|
| 章节记忆 vs 长期档案 | 章节记忆写“本章发生什么”，长期档案写“截至本章的结论”；长期档案不复制场景 |
| 工件 vs 投影 | 工件是生成物，投影是查询源；投影失败不读工件 |
| 去重 vs 合法重复 | 去重键带章节号；跨章重复保留 |
| 核心分类 vs 模块分类 | 核心分类固定五种；模块记录带 `module` |
| 作者已知 vs 角色已知 | `cognition` 必须有 `holder/audience`；无 holder 不进投影 |
| 自动 active vs 人工审核 | 章节记忆可自动 active；长期档案默认 draft，人工 accept |
| 完整上下文 vs token 预算 | 长期档案按模块加载；章节记忆只带上一章；线网只带当前批次 |
| 线网事件名 vs 章节号 | 展示层不出现章节号；内部保留 anchor |
| 旧作品 vs 新作品 | 旧作品先回填，再切换 archive-only；不做永久双读 |

---

## 5. 大模型执行契约

### 5.1 注意力预算

一次调用只做一件事：

```text
一章 chapter_memory
或 一次 archive_update
或 一批 storyline_network
```

不允许：

- 一次调用同时生成章节记忆和长期档案
- 一次调用处理整本书
- 一次调用要求模型同时维护九个模块和全部场景

### 5.2 输入预算

| 任务 | 输入上限 | 超限处理 |
|---|---|---|
| chapter_memory | 30k 字 | 按场景边界切块，禁止静默截断 |
| archive_update | 当前相关模块 + 本章 | 不注入整本档案 |
| storyline_network | 一批细纲 | 超过上限拆批 |

### 5.3 能力分层

| 任务 | 执行者 |
|---|---|
| 场景/认知/状态抽取 | 中等模型 |
| 长期档案合并 | 更强模型 |
| 线网二轮修正 | 更强模型 |
| 账本/数值/去重/版本 | 代码 |
| 结构校验 | 代码 |

### 5.4 输出契约

- 只输出 JSON
- 字段名允许别名，由代码归一
- 不让模型填 `version/status/work_id/source_chapters`
- 不让模型做算术
- 不让模型决定是否 active

### 5.5 失败分类

| 类型 | 处理 |
|---|---|
| JSON 不可解析 | 重试一次 |
| scenes 为空 | hard fail，进 draft |
| 证据不逐字 | warning |
| 分类/字段缺失 | warning |
| 投影异常 | failed + 可重放 |
| 模型超时 | 重试一次，再失败 |

---

## 6. 工程执行契约

### 部署

- `/health` 返回版本与作品列表
- 部署前 `py_compile`
- 部署前备份 DB
- 部署后跑 smoke：`/health`、`/api/story?work=...`、`/api/context?work=...`
- 保留代码 tar 与 DB 备份

### 数据迁移

- 只加列，不删列
- 迁移前备份
- 迁移后校验计数
- 回填脚本必须支持 `--dry-run`

### 观测

至少记录：

- job 状态与耗时
- `projection_status=failed` 数量
- warning 类型计数
- 每个作品的记录数
- 每次部署版本

### CI

- 合成 fixture 进 CI
- 真实章节 fixture 本地保留，不进公开仓库
- 跨书抽样人工执行，结果写 docs

---

## 7. 完成路线

### Phase 1：查询与存储统一（已完成）

- work 必填
- `/api/story` archive-only
- 735 条 graph 回填
- 分页
- 去重与旧记录清理

验收：

```text
/api/story?work=神域囚徒       → 735
/api/story?work=斗罗大陆验证   → 155
GET /api/story                 → 400
```

### Phase 2：长期档案 diff（下一步）

- `archive_update` 输出结构化 diff
- 只更新变化模块
- 人工 accept 后 active
- 模块级回滚

验收：连续 5 章，档案不重复、不叠加、可回滚。

### Phase 3：线网接入

- 线网进入 context
- 线网与章节记忆交叉校验
- 二轮修正带证据差异

验收：下一章生成能直接使用批次线网。

### Phase 4：结构化投影

- cognition → graph holder/audience
- relation/goal → StoryRegisterEntry
- resource/item/debt → TypedEvent

验收：同一事实不再同时存在文档和 graph 两个口径。

### Phase 5：质量与规模

- prompt v4 重跑 3 次
- 6 本 × 3 章抽样
- 10 章连续更新
- POV/因果/信息差评估

验收：跨书 warning 类型稳定，不靠单书补丁。

---

## 8. 不再做的事

1. 不为单本书加字段
2. 不为单章加特判
3. 不在 graph 和 archive 之间做在线双读
4. 不让一个 prompt 同时做多类工件
5. 不用模型做数值和版本
6. 不用 offset 分页承载超大结果
7. 不把 warning 当 hard fail
8. 不在没有跨书复现时改通用规则
