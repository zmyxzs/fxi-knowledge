# fxi-knowledge 知识库技术说明

状态：2026-11-17  
定位：长篇创作与同人写作的记忆、一致性、关联查询服务。

---

## 1. 它是什么

`fxi-knowledge` 不是写作模型，也不是通用图数据库。

它是长篇创作的**记忆与一致性服务**：

```text
存住前面写过什么
查出人物、关系、事件、认知
给下一章提供上下文
发现吃书和状态倒退
支持回滚和审计
```

### 主要使用方

1. **连载写作流水线**
   - 写前取上下文
   - 写后入库、解析、对账
   - 防止人物状态、关系、伏笔、规则吃书

2. **同人/二创**
   - 区分原著 canon 与二创 fanon
   - 查原著事实、分叉点、角色认知边界
   - 当前 canon/fanon 结构还没有完全落地

3. **拆书/学习**
   - 抽章节记忆、场景因果、线程、线网、文风锚点
   - 当前事件/认知/线程可查，拆书聚合接口未完成

### 不做什么

```text
不生成正文
不做排版发布
不做通用知识图谱
不把模型输出无条件当 canon
不为每本书写特判
```

---

## 2. 核心问题

长篇写作真正难的不是“生成一段”，而是：

```text
第 300 章时，还能不能准确知道第 30 章发生过什么
人物现在是什么状态
谁和谁是什么关系
谁知道什么、误判什么
哪些伏笔没回收
哪些规则不能改
```

传统做法是塞全文或摘要，问题是：

- 上下文越来越长
- 模型记不住
- 摘要丢细节
- 状态会倒退
- 关系和事件查不到

知识库的做法是：

```text
章节正文 → 章节记忆 → 状态/关系/事件/认知 → 结构化查询
```

---

## 3. 总体架构

```text
作品 work
  ↓
来源 sources（章节正文）
  ↓
章节记忆 chapter_memory
  ├─ scenes           场景/事件
  ├─ cognition        角色认知/POV
  ├─ state_deltas     状态变化
  ├─ unresolved       未决线程
  └─ snapshot         章节快照
  ↓
代码派生
  ├─ canonical 状态
  ├─ event
  ├─ relation
  ├─ knowledge
  └─ thread
  ↓
长期档案 archive_update
  ├─ 规则
  ├─ 世界观
  ├─ 势力
  ├─ 故事线
  └─ 伏笔
  ↓
查询
  ├─ /api/story
  ├─ /api/entity
  ├─ /api/events
  ├─ /api/relations
  ├─ /api/knowledge
  ├─ /api/context
  └─ /api/search
```

---

## 4. 技术栈

```text
Python 3.11+
SQLite + FTS5
jieba 分词
pydantic v2
pytest
ruff
OpenAI-compatible LLM
```

服务：

```text
scripts/kb_server.py
systemd: fxi-kb
nginx: kg.zmteams.site
```

---

## 5. 数据模型

### 5.1 文档层 `kb_document`

保存版本化工件：

```text
chapter_memory
long_term_archive
storyline_network
```

状态：

```text
draft
reviewed
active
rejected
superseded
```

投影状态：

```text
pending
done
failed
not_applicable
```

### 5.2 记录层 `kb_story_record`

统一记录表，不按查询类型拆表。

关键字段：

```text
work_id
chapter_index
category
payload_json
entry_key
layer
module
info_type
origin
revision
status
```

`category` 包括：

```text
fact
scene
cognition
thread
state_delta
event
relation
knowledge
```

`layer`：

```text
evidence    证据
canonical   当前 canonical 状态
```

### 5.3 变更日志 `kb_archive_change`

append-only：

```text
entry_key
op = upsert / archive / delete
before_json
after_json
chapter_index
evidence
```

作用：

- 审计
- 回滚
- `at_chapter` 历史回放
- 对账

### 5.4 实体 `kb_entity`

```text
canonical_name
aliases
entity_type
lifecycle_status
```

用于人物/地点/组织/物品的别名解析。

### 5.5 作品画像 `kb_work_profile`

```text
work_type
read_layer
archive_modules
entity_types
story_categories
resource_units
glossary
```

代码只认识结构，题材名词由作品配置注入。

---

## 6. Canonical 设计

### 6.1 key 规则

```text
entry_key = subject:predicate
```

例如：

```text
唐三:心理状态
唐三:玄天功.境界
唐昊:对唐三发展的态度
```

不带模块号，避免同一状态因模块不同而重复。

### 6.2 source_delta

`archive_update` 的状态变化不再让模型重新起 key，而是引用：

```json
{
  "source_delta": 0,
  "op": "upsert",
  "value": "初步掌握"
}
```

代码用：

```text
chapter_memory.state_deltas[0]
→ entity + field
→ entry_key
```

这样：

- 不需要 predicate 词表
- 不需要前缀剥离
- 不需要括号剥离
- 不需要模糊匹配
- 对账可以精确 key

非状态变化才允许模型给：

```json
{
  "entry_key": "规则:魂技",
  "subject": "魂技",
  "predicate": "定义"
}
```

### 6.3 变更应用

```text
upsert  → 写 canonical
archive → 标记归档
delete  → 删除
```

每次变更写 `kb_archive_change`。

---

## 7. 解析流程

### 7.1 上传

```text
POST /api/sources
{work, title, content}
```

### 7.2 章节记忆

```text
POST /api/parse
{work, mode: "chapter_memory"}
```

默认解析该作品全部来源；也可以传 `source_ids`。

### 7.3 长期档案

```text
POST /api/parse
{work, mode: "archive_update"}
```

负责：

```text
规则
世界观
势力
地图
故事线
伏笔
状态变化
```

### 7.4 线网

```text
POST /api/parse
{work, mode: "storyline_network"}
```

二轮：

```json
{"round": 2, "base_scope_key": "..."}
```

### 7.5 其他模式

```text
state_update
memory
deconstruct_extract
```

保留兼容，主流程优先 `chapter_memory` 和 `archive_update`。

---

## 8. 查询能力

### 8.1 `/api/story`

当前 canonical 条目。

参数：

```text
work
subject
module
page_size
offset
```

### 8.2 `/api/entity`

人物 dossier。

```text
GET /api/entity?work=&name=&at_chapter=&limit=
```

返回：

```text
canonical
events
relations
knowledge
threads
counts
```

`at_chapter` 会从 `kb_archive_change` 回放历史状态。

### 8.3 `/api/events`

```text
GET /api/events?work=&chapter=&participant=&limit=&offset=
```

返回：

```text
name
chapter
participants
cause
outcome
evidence
```

当前 `event` 来自 `chapter_memory.scenes`，`cause` 和 `outcome` 仍共用因果段落。

### 8.4 `/api/relations`

```text
GET /api/relations?work=&from=&to=&limit=&offset=
```

双向：

```text
GET /api/relations?work=&between=唐昊,唐三
```

返回：

```text
from
to
relation
value
chapter
direction
```

关系来源：

- 显式 `state_delta.category=relation`
- canonical 中的 `对X...` / `与X...` 谓词

### 8.5 `/api/knowledge`

```text
GET /api/knowledge?work=&holder=&chapter=&limit=&offset=
```

返回角色认知/POV：

```text
holder
type
content
audience
chapter
evidence
```

### 8.6 `/api/context`

```text
GET /api/context?work=&chapter=&max_chars=
```

返回下一章上下文：

```text
canonical_archive
chapter_memory
```

当前 50 章时约：

```text
canonical_archive 23974 chars
chapter_memory 1349 chars
```

偏大，需要后续按人物/线程/事件裁剪。

### 8.7 `/api/search`

词法检索，FTS5。

返回：

```text
text
source
chapter_index
cards
```

### 8.8 管理接口

需要 `X-API-Key`：

```text
/api/canonical
/api/canonical/duplicates
/api/canonical/dedupe
/api/canonical/apply
/api/reconcile
/api/archive_changes
/api/story_record
```

---

## 9. 一致性与质量

### 9.1 对账

```text
chapter_memory.state_deltas
vs
archive_update changes
```

精确 key 匹配优先。

### 9.2 变更校验

- 状态变化必须有 `source_delta`
- 非状态变化必须有 `entry_key` 或 `subject/predicate`
- 越界 `source_delta` 跳过
- 缺字段 change 跳过
- 不因单条坏 change 中断整批

### 9.3 去重

`find_canonical_duplicates` / `dedupe_canonical`

按 `subject + predicate + info_type` 分组。

### 9.4 审计

`/api/audit` 检查：

- 账本
- 死亡状态
- 红线
- 硬设定冲突

### 9.5 回滚

`kb_archive_change` 保留 before/after。

`at_chapter` 可回放历史状态。

---

## 10. 当前验证结果

### 10.1 斗罗大陆前 50 章

```text
sources：50
chapter_memory：50
documents：51
canonical：427
archive changes：484
duplicate groups：0
reconcile：159/159 100%
```

唐三：

```text
canonical 156
events 203
relations 24
knowledge 158
threads 110
```

历史：

```text
at_chapter=10
  canonical 43  events 39  relations 6   knowledge 35  threads 23

at_chapter=25
  canonical 100 events 99  relations 15  knowledge 84  threads 63

at_chapter=50
  canonical 162 events 203 relations 24  knowledge 158 threads 110
```

### 10.2 跨书验证

6 本书 × 2 章：

```text
大王饶命          100% / 100%
十日终焉          75% / 100%（复跑 100%）
超神机械师        100% / 100%
网游之天谴修罗    100% / 100%
我在精神病院学斩神 100% / 100%
逆天邪神          100% / 100%
```

### 10.3 1000 章合成压测

```text
chapters 1000
story rows 12000
changes 5000
```

查询 p95：

```text
entity current      340ms
entity at_chapter   280ms
events chapter      49ms
knowledge chapter   49ms
story page          223ms
canonical at_chapter 98ms
```

### 10.4 测试

```text
1912 passed / 7 skipped
```

---

## 11. 性能与成本

当前解析：

```text
chapter_memory 1 次/章
archive_update 1 次/章
```

50 章新增 38 章耗时约：

```text
2 小时 45 分钟
```

瓶颈：

```text
archive_update 串行
每章重写长期档案
```

优化方向：

```text
chapter_memory 并发
archive_update 批量
状态由代码写
事件/关系/认知由代码派生
```

目标：

```text
100 章 ≤ 2 小时
1000 章 8–12 小时
```

尚未完成 A/B 验证。

---

## 12. 适用性

已覆盖：

```text
玄幻
都市/搞笑
无限流
科幻/游戏
网游
都市异能
```

同人需要补：

```text
canon_scope
source_work
divergence_chapter
```

拆书需要补：

```text
/api/deconstruct
场景/事件/线程/线网聚合
爽点/节奏/文风查询
```

---

## 13. 当前限制

1. 关系覆盖只到 24 条 / 50 章，仍偏少
2. 没有独立 ability 层，“这个点会什么”只能按 canonical 关键词过滤
3. 事件 `cause` 和 `outcome` 仍共用一段文字
4. 上下文 24k 字符，偏大
5. threads 按文本提及过滤，仍偏粗
6. canon/fanon 未落地
7. 拆书聚合接口未完成
8. 投影不是单事务
9. archive_update 仍每章串行
10. 旧数据缺 change log 时历史状态会缺

---

## 14. 最短使用路径

### 上传

```bash
curl -X POST "$BASE/api/sources" \
  -H "X-API-Key: $FXI_KB_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"work":"斗罗大陆","title":"第1章","content":"..."}'
```

### 解析章节记忆

```bash
curl -X POST "$BASE/api/parse" \
  -H "X-API-Key: $FXI_KB_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"work":"斗罗大陆"}'
```

### 解析长期档案

```bash
curl -X POST "$BASE/api/parse" \
  -H "X-API-Key: $FXI_KB_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"work":"斗罗大陆","mode":"archive_update"}'
```

### 查人物

```bash
curl "$BASE/api/entity?work=斗罗大陆&name=唐三&at_chapter=25"
```

### 查事件

```bash
curl "$BASE/api/events?work=斗罗大陆&chapter=25&participant=唐三"
```

### 查认知

```bash
curl "$BASE/api/knowledge?work=斗罗大陆&chapter=25&holder=唐三"
```

### 查双向关系

```bash
curl "$BASE/api/relations?work=斗罗大陆&between=唐昊,唐三"
```

### 查上下文

```bash
curl "$BASE/api/context?work=斗罗大陆&chapter=51"
```

---

## 15. 设计原则

```text
代码只认识结构，不认识题材名词
状态由证据生成，不让模型反复重写
key 稳定，变更可审计
查询复用同一套记录，不按查询建表
管理接口不混入公开查询
单条坏数据不阻断整批
不追求一次 100% 正确，追求可发现、可回滚
```

---

## 16. 下一步

```text
1. ability 层
2. 关系覆盖补全
3. 事件 cause/outcome 拆分
4. 上下文按人物/线程裁剪
5. canon/fanon
6. 拆书聚合接口
7. 批量 archive_update + 并发
8. 50 章 A/B 基准
```
