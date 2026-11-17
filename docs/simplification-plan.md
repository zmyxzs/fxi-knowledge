# 简化方案：把长期档案更新变成唯一 canonical

状态：2026-11-11（Phase 0 已实施，影子模式）

## 1. 知识库到底做什么

它不是写作模型，也不是通用知识图谱。它是长篇创作的**记忆与一致性服务**。

### 主要用途

| 场景 | 用法 |
|---|---|
| 连载写作 | 写第 N 章前取上下文；写完后入库、抽取、审校，防止吃书 |
| 长线连续性 | 谁在场、在哪、什么状态；谁知道什么；哪些伏笔/危机未解决 |
| 初稿审计 | 检查账本、死亡状态、硬规则、红线 |
| 回滚 | 编辑/删除第 N 章后回到第 N-1 章状态 |
| 拆书/学习 | 把爆款章节拆成去名化骨架、文风锚点 |
| 多作品 | 每个作品独立 `work_id`，查询按作品隔离 |

### 不做什么

- 不生成正文
- 不做发布/排版/平台对接
- 不做通用知识图谱
- 不把模型输出直接当 canon

### 成功标准

对任意章节 N，能稳定回答：

1. 谁在场、在哪、什么状态
2. 谁知道什么、误判什么
3. 哪些承诺/危机/伏笔未解决
4. 哪些硬设定不能被改写
5. 下一章需要哪些上下文

---

## 2. 当前复杂在哪

```text
graph
  → 735 条回填成 story_record

chapter_memory
  → 投影成 story_record
  → 又生成工件

archive_update
  → 只生成 long_term_archive 文档

storyline_network
  → 只生成文档

/api/story
  → 只读 story_record
```

问题不是“字段多”，而是**同一份事实有三套表示、两个写入者**：

- graph / story_record / long_term_archive 都在表达状态
- chapter_memory 和 archive_update 都在写事实
- 为了兼容，又加了去重、旧记录清理、失败投影过滤、分类过滤

这些补丁是在补“所有权不清”。

---

## 3. 简化目标

```text
1. chapter_memory 工件
   = 证据层：场景、认知、线程、原文锚点

2. archive_update diff
   = 唯一 canonical 状态写入者

3. storyline_network 工件
   = 批次视图，不写 canonical

4. kb_story_record
   = archive_update 应用后的结构化当前视图
```

读取：

```text
/api/story      → canonical story_record
/api/search     → chapter_memory 工件 FTS
/api/context    → canonical + 上一章证据 + 线网
/api/documents  → 工件版本
```

关键变化：

- `chapter_memory` 不再投影成 canonical story_record
- `archive_update` 不再输出整本档案，只输出 changes
- `storyline_network` 不再写事实
- graph 不再参与读取

---

## 4. canonical 条目契约

```json
{
  "entry_key": "character:唐三:玄天功.境界",
  "module": "五",
  "info_type": "dynamic",
  "op": "upsert",
  "subject": "唐三",
  "predicate": "玄天功.境界",
  "value": "第一重",
  "status": "active",
  "chapter_index": 3,
  "evidence": "……",
  "source": "archive_update"
}
```

规则：

1. `entry_key` 由代码生成，不让模型填。
2. `module` 由代码/配置决定，模型只能建议。
3. `info_type` 由模块决定：`rule/dynamic/history`。
4. `op` 只允许 `upsert/archive/delete`。
5. `value` 可以是标量、对象、数组，但必须有稳定 key。
6. `evidence` 缺失只 warning，不阻断。
7. `chapter_index` 由系统注入。

---

## 5. diff 应用规则

### 省略 = 不变

模型没输出的条目，视为不变，不删除。

### 显式删除

只有 `op=delete` 或 `op=archive` 才改变状态。

### 动态状态以新换旧

`info_type=dynamic`：

- 同一 `entry_key` 覆盖旧值
- 旧值写历史表/审计，不留在当前视图

### 历史事件追加

`info_type=history`：

- 同一 `entry_key` 追加新版本
- 读取时取最新/指定章节

### 冲突

同一响应内同一 `entry_key` 出现多次：

- 默认拒绝，返回 warning
- 或 last-wins，但必须记录

### 顺序

- `chapter_index` 小于当前 canonical 的 diff 默认拒绝
- 重跑同一章：先撤销该章旧 diff，再应用新 diff

### 并发

- 请求带 `base_version`
- 与当前版本不一致返回 409

---

## 6. 需要新增/调整的数据

### 当前视图

`kb_story_record` 继续作为 canonical 当前视图，但增加：

```text
entry_key
module
info_type
origin
revision
```

### 变更日志

新增 `kb_archive_change`：

```text
id, work_id, chapter_index, entry_key, op,
before_json, after_json, evidence, created_at
```

用途：

- 审计
- 回滚
- 重放
- 回答“这章改了什么”

这是必要的第二张表，不是补丁。

### 证据层

`chapter_memory` 工件保留，进入 `kb_document_fts`。

不再写 canonical story_record。

如果以后需要按 cognition 结构化查询，再加 `layer=evidence`，不要混入 canonical。

---

## 7. 兼容性

### 旧 graph 数据

- 标记 `origin=legacy_graph`
- `module=legacy`
- `info_type=history`
- 不删除，先只读

### 旧 chapter_memory 投影

- 标记 `origin=chapter_memory`
- 不再新增
- 迁移后从 canonical 读取中排除
- 证据仍在文档/FTS

### 旧 archive_update 文档

- 保留
- 新 diff prompt 重跑后生成 canonical
- 旧文档不删

### 旧客户端

- `/api/story` 形状保持
- 增加 `entry_key/module/info_type` 可选字段
- 多作品仍必须带 `work`

### 旧 prompt

- v1–v4 文档可读
- v5 只输出 changes
- `prompt_version` 记录来源

---

## 8. 可以删掉的补丁

| 补丁 | 简化后 |
|---|---|
| `dedupe_story_records` | `entry_key` 幂等 upsert |
| `drop_legacy_chapter_records` | 一次性迁移 |
| `only_projected` | canonical 只由 archive_update 写 |
| `graph_category` 过滤 | graph 不参与读取 |
| 旧 memory 记录清理 | 迁移时处理 |
| 章节记忆投影去重 | 不投影，问题消失 |
| 分类混用 | canonical 固定 module + entry_key |

保留：

- work 必填
- 分页
- 模糊校验
- `_first_present`
- 投影状态
- warning

---

## 9. 大模型注意力

### 两次调用

```text
第 1 次：chapter_memory
  输入：本章正文
  输出：场景/认知/状态/线程证据

第 2 次：archive_update
  输入：相关旧模块 + chapter_memory 摘要
  输出：changes
```

不让一次调用同时做两件事。

### 模块选择

不把整本档案塞进去。按本章实体/模块选择：

```text
本章出现的角色 → 模块四/五
本章出现的势力 → 模块六
本章出现的线索 → 模块七/八
```

选择由代码做，不让模型选。

### 长章节

- 30k 以内直接抽
- 超过按场景边界切块
- 合并后只把摘要交给 archive_update

### 能力分层

| 任务 | 模型 |
|---|---|
| chapter_memory | 中等 |
| archive_update diff | 更强 |
| storyline_network | 更强 |
| 合并/渲染/去重 | 代码 |

---

## 10. 适用性

- 短章节：允许少于 3 个场景
- 没有状态变化：允许空 diff
- 没有线网：允许不生成
- 不同题材：模块配置化
- 不同作品：work 隔离
- 旧数据：标记 origin，不强行重写

---

## 11. 迁移步骤

### Step 1：加字段

- `entry_key`
- `module`
- `info_type`
- `origin`
- `revision`

### Step 2：标记旧数据

```text
graph 735 → origin=legacy_graph
chapter_memory 投影 → origin=chapter_memory
```

### Step 3：新 prompt

- `archive_update` 输出 changes
- 先 dry-run
- 只对 1 个作品试

### Step 4：对照

```text
旧 /api/story vs 新 canonical
数量、分类、关键实体、章节
```

### Step 5：切换

- `/api/story` 只读 canonical
- 旧的只保留为 evidence
- 回滚备份

---

## 12. 验收

1. 同一章重跑不产生重复
2. 省略条目不删除
3. 显式 delete 才删除
4. 动态状态不叠加
5. 历史事件可追溯
6. 回滚第 N 章能恢复第 N-1 状态
7. 并发写返回 409
8. 旧客户端仍能读 `/api/story`
9. 旧 graph-only 作品迁移后不丢数据
10. 跨书抽样 warning 不增加

---

## 13. 需要确认

1. canonical 是否确定为 `archive_update`？
2. `chapter_memory` 是否只做证据？
3. 是否接受一次性迁移旧数据？
4. `kb_archive_change` 是否允许新增？
5. 是否先做一个作品试点，再全量？

如果这五条确认，下一步不是继续修 `/api/story`，而是把 `archive_update` 改成 diff 模型，然后删掉一层投影。

---

## 14. Phase 0 实施状态（2026-11-11）

已实施：

- `kb_story_record` 增加 `entry_key/layer/module/info_type/origin/revision/status`
- 新增 `kb_archive_change` 变更日志
- 新增 `archive.apply_archive_changes`
- 新增 `GET /api/canonical`
- 新增 `POST /api/canonical/apply`
- `archive-update.md` 增加 `changes`
- `apply_archive_update` 应用 changes
- `migrate_canonical.py` 影子迁移

验证：

```text
神域囚徒
  evidence 735
  canonical 564
  迁移 changes 700

斗罗大陆
  evidence 155
  canonical 84
  迁移 changes 89

本地两章真实模型
  chapter_memory 2
  archive_update changes 10 + 34
  canonical 42
  reconcile 0.8 / 0.714

生产十二章重放（斗罗大陆）
  chapter_memory 12
  archive_update changes 1 + 14 + 23 + 22 + 10 + 10 + 10 + 3 + 15 + 5 + 5 + 3
  canonical 109
  evidence 203
  documents 13
  change log 124
  reconcile 12/12 100%，exact_matched 12/12
  context canonical_archive 4395 + chapter_memory 1257
  duplicate groups 0
  missing evidence 0
```

说明：

- 旧作品已全部删除，生产只保留 `斗罗大陆`
- 十二章全部走 `chapter_memory → archive_update diff → canonical`
- 使用 `source_delta` 后，12 章全部 exact 100%
- 第 11 章旧口径问题已通过重放消除
- canonical key 为 `主体:字段`，不带模块号
- canonical 109 比旧 212 少，因为不再把大量重复/派生条目写入
- `read_layer=canonical`
- `/api/story` 109，`/api/canonical` 109

读取策略：

```text
/api/story      默认读作品画像 read_layer
/api/canonical  读 canonical 影子层
```

当前作品：

```text
斗罗大陆
  read_layer=canonical
  /api/story → 109
  evidence → 203
  canonical → 109
```

`/api/context` 也已按 `read_layer` 分流：

```text
canonical:
  canonical_archive 4395
  chapter_memory 1257
  （不再重复 compiled_archive）

evidence:
  long_term_archive
  chapter_memory
  compiled_archive
```

`/api/documents` 已支持 `limit/offset`。
