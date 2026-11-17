# 知识库优化方案 v1

状态：2026-11-12  
目标：把当前“能跑但慢、查询弱、补丁多”的系统，收敛成可批量解析、可关联查询、可评估、可回滚的知识库。

---

## 0. 目的与非目标

### 目的

1. 一本书的解析从“几周”降到“小时级”。
2. 状态、人物、关系、事件、认知可查询。
3. 同人能区分原著 canon 与二创 fanon。
4. 拆书能查场景、事件、线网、伏笔、风格锚点。
5. 质量可评估，不靠感觉。
6. 成本可控，每章有预算。
7. 兼容现有数据和 API，不推倒重来。

### 非目标

1. 不做完整图数据库。
2. 不做通用语义搜索平台。
3. 不追求一次抽取 100% 正确。
4. 不为每本书写特判。
5. 不为每种查询加表、加参数、加类。
6. 不把所有状态都交给大模型反复重写。

---

## 1. 现状基线（实测）

### 1.1 速度

生产 12 章 `long_term_archive`：

```text
2026-11-12 07:50:30
→ 2026-11-12 08:14:56
```

```text
24 分 26 秒 / 12 章
≈ 122 秒/章
```

这只是 `archive_update`，不含 `chapter_memory`。

按当前串行流程，1000 章：

```text
archive_update 约 34 小时
chapter_memory 约 20–30 小时
+ 重试、人工、重放
= 几天到几周
```

### 1.2 数据

```text
chapter_memory 文档：12
state_deltas：38
唯一 state key：36
canonical：109
evidence：203
changes：124
```

### 1.3 状态覆盖

```text
state key 在 canonical 中覆盖率：36/36 = 100%
```

说明状态 key 可以由 `chapter_memory` 直接生成。

### 1.4 状态值一致性

把“每章最新 state_delta 的 to”与 canonical value 对比：

```text
36 个 key
29 个文本包含
7 个文本不同
```

差异多为措辞：

```text
state_delta: 同意参加
canonical:   同意唐三参加觉醒仪式

state_delta: 提供另一个火炉
canonical:   唐昊给唐三房间另设一个火炉；未指点；酒少食物多
```

结论：

```text
key 可以代码生成
value 不能直接说无损
```

### 1.5 测试

```text
1906 passed / 7 skipped
```

测试证明不变量，不证明吞吐和语义质量。

---

## 2. 核心问题

### 2.1 流水线

```text
每章 2 次模型调用
archive_update 每章重写整本档案
串行执行
人工轮询
```

### 2.2 数据模型

canonical 只有：

```text
subject / predicate / value
```

没有：

```text
entity_id
object_id
relation_type
event_id
participants
holder/audience
source_work
canon_scope
```

### 2.3 查询

能查 flat 条目，不能稳定查：

```text
人物关系
事件参与者
人物出场章节
谁知道什么
原著/二创来源
拆书结构
```

### 2.4 质量

```text
reconcile 100% 只证明 key 覆盖
不证明 value 正确
不证明 POV 正确
不证明事件因果正确
```

### 2.5 兼容与迁移

当前数据是清空重跑，旧迁移路径未验证。

### 2.6 运维

没有统一的：

```text
调用次数
token
耗时
重试
限流
每章成本
```

---

## 3. 目标架构

```text
上传批次
  ↓
内容哈希/去重/跳过未变章节
  ↓
chapter_memory（并发，模型 A）
  ↓
校验 + 低置信标记
  ↓
代码应用 state_deltas → canonical（0 次模型）
  ↓
代码派生 event / relation / knowledge / thread（0 次模型）
  ↓
每 K 章批量 archive_update（模型 B，只处理非状态）
  ↓
review / activate
  ↓
查询：entity / events / relations / knowledge / context / search
```

核心原则：

```text
状态：代码写
叙事：批量写
关联：代码派生
查询：结构化
```

---

## 4. 数据模型

### 4.1 只扩展一种记录

继续使用 `kb_story_record`，扩展 `category`：

```text
entity
relation
event
knowledge
thread
fact
state
rule
```

payload 统一 JSON：

```json
{
  "name": "...",
  "from": "...",
  "to": "...",
  "relation": "...",
  "participants": ["..."],
  "holder": "...",
  "audience": ["..."],
  "chapter": 4,
  "value": "...",
  "cause": "...",
  "outcome": "...",
  "evidence": "...",
  "source_work": "斗罗大陆",
  "canon_scope": "canon",
  "confidence": "high"
}
```

不新增每种查询的表。

### 4.2 实体

使用现有 `kb_entity`：

```text
canonical_name
aliases
entity_type
```

所有 subject / participant / holder 先做别名解析。

### 4.3 关系

```json
{
  "category": "relation",
  "entry_key": "relation:唐昊:父子:唐三",
  "payload": {
    "from": "唐昊",
    "to": "唐三",
    "relation": "父子",
    "value": "亲近",
    "chapter": 9,
    "source_work": "斗罗大陆",
    "canon_scope": "canon"
  }
}
```

### 4.4 事件

```json
{
  "category": "event",
  "entry_key": "event:4:1",
  "payload": {
    "name": "一万锤的资格考验",
    "chapter": 4,
    "participants": ["唐三", "唐昊"],
    "location": "唐三家",
    "cause": "唐昊要求锻造一万下",
    "outcome": "唐三开始锻造，双手无伤",
    "evidence": "..."
  }
}
```

### 4.5 认知

```json
{
  "category": "knowledge",
  "entry_key": "knowledge:唐三:玄玉手隐瞒:9",
  "payload": {
    "holder": "唐三",
    "audience": ["唐三"],
    "content": "唐昊不知道唐三修炼玄玉手",
    "belief_state": "believes",
    "chapter": 9
  }
}
```

### 4.6 同人边界

```text
source_work：事实来自哪本书
canon_scope：canon | fanon | crossover
base_work_id：二创基于哪本原著
divergence_chapter：从哪章分叉
```

查询默认：

```text
canon 查询只读 canon
二创查询读 fanon + 必要的 canon
```

---

## 5. 处理流水线

### 5.1 批量上传

```json
{
  "work": "斗罗大陆",
  "chapters": [
    {"title": "第1章", "content": "..."},
    {"title": "第2章", "content": "..."}
  ]
}
```

按内容哈希去重，未变章节跳过。

### 5.2 chapter_memory 并发

```text
并发 4 起步
自适应到 8
429 退避
单章失败重试 2 次
```

模型 A：便宜、快、稳定。

### 5.3 状态代码应用

```text
state_deltas → canonical
```

规则：

- key 来自 `entity + field`
- value 来自 `to`
- evidence 来自原文
- 短值/歧义值标记 `provisional`
- 不调用模型

### 5.4 关联派生

```text
scenes → event
cognition → knowledge
relation state_deltas → relation
unresolved → thread
所有参与者 → entity 解析
```

不调用模型。

### 5.5 批量叙事档案

每 K 章（先 K=10）：

```text
输入：本批 chapter_memory
输出：非状态 changes + 可选快照
```

只处理：

```text
规则
世界观
势力
地图
故事线
伏笔
```

每条 change 必须带 `chapter_index` 和 `source_delta`。

模型 B：更强、更贵、调用少。

### 5.6 review

```text
高风险：关系、POV、冲突、短值
→ provisional，等待确认
低风险：事实、位置、数量
→ auto
```

---

## 6. 状态质量策略

### 6.1 不追求文本完全一致

代码状态和 archive 状态允许措辞不同，但必须：

```text
key 正确
value 不矛盾
evidence 存在
不覆盖更新值
```

### 6.2 provisional

以下情况标记：

```text
value 长度 < 4
value 含“未知/可能/似乎”
同一 key 本章多次变化
relation 类
POV 类
```

### 6.3 批量增强

`archive_update` 批量时可以对 provisional 条目做：

```text
补全
合并
纠错
```

但不能每章重写全部状态。

### 6.4 冲突处理

同一 key 同章多值：

```text
保留最后一条
记录 conflict
进入 review
```

---

## 7. 查询设计

### 7.1 公开查询

```text
GET /api/entity?work=&name=
GET /api/events?work=&chapter=&participant=
GET /api/relations?work=&from=&to=
GET /api/knowledge?work=&holder=&chapter=
GET /api/story?work=&subject=&module=
GET /api/context?work=&chapter=
GET /api/search?work=&q=
```

### 7.2 entity 返回

```json
{
  "entity": "唐三",
  "state": [],
  "relations": [],
  "events": [],
  "knowledge": [],
  "threads": []
}
```

### 7.3 查询原则

- 不为每种关系加参数
- 统一 `category + work + chapter + subject`
- 分页统一 `limit/offset`
- 管理接口需要 key
- 查询不扫全文

---

## 8. 效率与成本

### 8.1 调用数

当前：

```text
2 次/章
```

目标：

```text
chapter_memory 1 次/章
archive_update 1 次/10 章
≈ 1.1 次/章
```

### 8.2 并发

```text
4 并发起步
观察 429
自适应 2–8
```

### 8.3 批量档案

```text
K=10 起步
质量不过关降到 K=5
召回足够再升 K=20
```

### 8.4 输入裁剪

`archive_update` 只传：

```text
本批 chapter_memory
相关模块
上一版相关片段
```

不传整本档案。

### 8.5 输出裁剪

```text
只输出 changes
不每批输出整本九模块
快照每 10 批一次
```

### 8.6 缓存

- 系统提示固定
- 前缀缓存
- 相同内容哈希跳过
- 失败只重试失败批次

### 8.7 目标

```text
100 章 <= 2 小时
1000 章 <= 8–12 小时
成本 <= 基线 60%
每章调用 <= 1.2
```

这是目标，不是承诺；必须 A/B 验证。

---

## 9. 质量保障

### 9.1 状态

```text
key 覆盖 100%
value 语义一致 >= 95%
重复 key = 0
过期覆盖 = 0
```

### 9.2 非状态

```text
规则/伏笔 recall >= 90%
人工 precision >= 90%
```

### 9.3 关联

```text
event participants >= 95%
relation from/to >= 95%
knowledge holder >= 95%
```

### 9.4 评估集

```text
3 本书 × 10 章
1 本 50 章连续
```

人工抽检：

```text
每本 30 条状态
30 条非状态
20 条事件/关系/认知
```

### 9.5 不能只看 reconcile

reconcile 只证明 key 覆盖。

语义质量必须人工/独立评审。

---

## 10. 兼容与迁移

### 10.1 影子运行

```text
旧流程 A
新流程 B
同章跑
不写生产
对比
```

### 10.2 开关

```text
pipeline=new|old
```

按作品切换。

### 10.3 数据

- 旧 canonical 保留
- 新派生记录可重建
- `category` 只增加值
- 旧 API 响应不变
- 管理接口加 key

### 10.4 回滚

- 保留数据库备份
- 按作品回滚
- 旧流程可重跑

---

## 11. 风险与解决

| 风险 | 后果 | 解决 |
|---|---|---|
| chapter_memory 漏状态 | canonical 缺失 | key 覆盖检查、低置信重试、抽样 |
| 代码 value 太短 | 状态丢失细节 | provisional、evidence、批量增强 |
| 批量 archive 漏规则 | 世界观缺失 | A/B recall、K 降级、单章回退 |
| 并发被限流 | 任务失败 | 自适应并发、429 退避 |
| 章节乱序 | 状态倒退 | 单作品串行写、章节锁 |
| key 重复 | canonical 冲突 | 唯一索引、冲突队列 |
| 原著/二创混写 | 同人吃书 | source_work + canon_scope |
| 旧客户端不兼容 | 调用失败 | 加字段不删字段、旧接口保留 |
| 长章节超 token | 解析失败 | 场景切块、预算控制 |
| 成本超预算 | 费用失控 | 每章预算、批次限额、熔断 |
| 模型漂移 | 质量下降 | prompt 版本、golden set、canary |
| review 堆积 | 上线慢 | 低风险自动、抽样、高风险队列 |
| 派生非原子 | 半成品 | 每章事务、按 source_document_id 重建 |
| 指标自欺 | 假优化 | 人工抽样、独立评估 |

---

## 12. 实施阶段

### Phase 0：基线

```text
30 章 A/B
记录耗时、调用、token、质量
```

### Phase 1：查询结构

```text
event / relation / knowledge 派生
/api/entity 返回关联
/api/events
```

### Phase 2：状态代码化

```text
state_deltas → canonical
shadow 对比
```

### Phase 3：批量叙事

```text
K=10
只输出非状态
shadow 对比
```

### Phase 4：并发

```text
4 → 8
限流测试
```

### Phase 5：切换

```text
按作品开关
监控
回滚预案
```

---

## 13. 验收问题

```text
1. 唐三当前状态？
2. 唐三和唐昊什么关系？
3. 素云涛对唐三什么态度？
4. 第4章发生了什么？
5. 谁参与第4章？
6. 唐三在哪几章出场？
7. 唐三第5章知道什么？
8. 这条设定是原著还是二创？
9. 前10章主线是什么？
10. 哪些伏笔没回收？
```

---

## 14. 不做清单

1. 不为每种查询加表
2. 不为每种关系加字段
3. 不为每本书加规则
4. 不重新调模型抽事件
5. 不每章重写整本档案
6. 不追求状态文本完全一致
7. 不用 reconcile 代替语义评估
8. 不在没有基线时宣布优化成功
9. 不双写新旧 canonical
10. 不让管理接口混进公开查询

---

## 15. 结论

当前瓶颈是流程，不是模型能力：

```text
每章两次调用
每章整本档案
串行
```

优化核心：

```text
状态代码写
叙事批量写
关联代码派生
查询结构化
质量用门槛验证
```

只有 A/B 通过，才切换。

---

## 16. 实现进度

### 已完成

```text
/api/events
/api/knowledge
/api/relations
/api/entity 返回 events/relations/knowledge/threads
```

生产实测（斗罗大陆，12 章，唐三）：

```text
canonical: 50
events: 47
relations: 4
knowledge: 40
threads: 41
```

这些查询直接复用已有投影：

```text
scene       → events
cognition   → knowledge
thread      → threads
state_delta → relations（category=relation）
```

没有新增模型调用。

### 待完成

```text
关系覆盖补全
canon_scope / source_work
30 章 A/B 基准
批量 archive_update
并发控制
```

### 当前限制

- 关系只来自显式 relation state_delta，覆盖不全
- 事件就是 scene，没有单独 event_type
- 认知是 chapter_memory cognition，不是完整 POV 图
- entity 的 canonical 数组尚未分页

---

## 17. 上千章后的查询

### 已支持

```text
GET /api/events?work=&chapter=100
GET /api/knowledge?work=&chapter=100
GET /api/entity?work=&name=&at_chapter=100
GET /api/context?work=&chapter=100
GET /api/search?work=&q=
```

`/api/entity?at_chapter=` 现在从 `kb_archive_change` 回放：

```text
第1章之前的状态
第5章时的状态
第12章时的状态
```

实测（斗罗大陆，唐三）：

```text
at_chapter=1   canonical 1   events 3   knowledge 4
at_chapter=5   canonical 25  events 20  knowledge 21
at_chapter=12  canonical 50  events 47  knowledge 40
```

说明前面的章节和状态可以查。

### 当前限制

1. 事件/认知查询仍先取全量再 Python 过滤  
   1000 章约几千条，可接受；10 万条会慢。

2. `at_chapter` 回放扫描 change log  
   1000 章约几千到一万条，可接受；更大需要快照。

3. `offset` 分页在很大偏移时变慢  
   1000 章可接受；10k 章建议 cursor。

4. `long_term_archive` 是单文档  
   章数很大时文档会变长，需要分段。

5. SQLite 单写者  
   并发解析需要队列，不适合多进程同时写。

### 扩展方案

#### 1. SQL 下推

事件/认知/关系过滤下沉到 SQL：

```sql
WHERE work_id=?
  AND category='scene'
  AND chapter_index=?
```

参与者过滤增加 JSON 索引或投影列。

#### 2. Cursor 分页

```text
after_chapter
after_id
```

替代大 offset。

#### 3. 状态快照

每 50 章保存一次 canonical 快照：

```text
chapter 50
chapter 100
chapter 150
```

`at_chapter=120`：

```text
加载 100 快照
+ 回放 101–120 changes
```

避免全量回放。

#### 4. 档案分段

`long_term_archive` 按卷/每 100 章分段：

```text
archive:1-100
archive:101-200
```

查询时只取相关段。

#### 5. 索引

```text
(work_id, category, chapter_index)
(work_id, chapter_index, created_at)
(work_id, layer, status, chapter_index)
```

#### 6. 查询目标

```text
1000 章：entity p95 < 300ms
5000 章：entity p95 < 500ms
10000 章：需要快照 + cursor
```

这是目标，需压测验证。

### 容量估算

1000 章大致：

```text
chapter_memory 文档：1000
scene/event：3000–5000
cognition：3000–6000
thread：1000–3000
canonical：8000–12000
changes：10000–20000
```

SQLite 完全可以承受，关键是查询下推和分页。

---

## 18. 结论补充

上千章后还能查前面，前提是：

```text
章节索引保留
change log 保留
at_chapter 回放
分页/快照
```

当前 12 章已验证；1000 章需要压测。

---

## 19. 1000 章合成压测

脚本：

```text
scripts/bench_query_scale.py
```

数据：

```text
chapters=1000
story_rows=12000
changes=5000
```

结果：

| 查询 | p50 | p95 |
|---|---:|---:|
| entity current | 311ms | 340ms |
| entity at_chapter=500 | 257ms | 280ms |
| events chapter=500 | 48ms | 49ms |
| knowledge chapter=500 | 47ms | 49ms |
| story page | 144ms | 223ms |
| canonical at_chapter=500 | 61ms | 98ms |

已做优化：

- 事件/认知过滤下推到 SQL
- 章节精确过滤下推
- `max_chapter` 下推
- `LIMIT/OFFSET` 下推
- 新增 `(work_id, layer, status, chapter_index)` 索引
- canonical 当前查询按 name 先过滤

结论：

- 事件/认知查询已达标
- at_chapter 已达标
- entity current 340ms，略高于 300ms 目标
- story page 223ms，可接受
- 1000 章可用，不是几周才能查

下一步：

```text
1. entity 多个 count 合并
2. story page 索引/覆盖查询
3. 每 50 章快照
4. 10000 章压测
```

---

## 20. 结论

当前不是“上千章后查不到”，而是：

```text
1000 章查询可用
部分 p95 还需优化
10000 章需要快照 + cursor
```

后续不再加查询种类，先把已有查询的延迟和稳定性压下去。



