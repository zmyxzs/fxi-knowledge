# 斗罗大陆前 50 章解析结果

状态：2026-11-13

## 数据

```text
作品：斗罗大陆
章节：50
模式：chapter_memory + archive_update
read_layer：canonical
```

## 结果

```text
sources：50
chapter_memory：50
long_term_archive：1（多版本）
documents：51
canonical：427
archive changes：484
duplicate groups：0
```

`canonical` 在解析中曾出现 1 组重复，已 dedupe 归档 1 条。

## 对账

```text
state_deltas：159
matched：159
exact_matched：159
rate：100%
```

50 章全部 100%。

## 唐三关联

```text
canonical：156
events：203
relations：24
knowledge：158
threads：110
```

说明：`relations` 去掉了没有目标的空关系，并补充了 canonical 里的显式关系谓词；`threads` 只保留文本中提及该人物的线程。

其他人物：

```text
小舞  canonical 19  events 38  relations 7  knowledge 15  threads 14
大师  canonical 54  events 78  relations 6  knowledge 59  threads 30
唐昊  canonical 35  events 38  relations 4  knowledge 22  threads 20
```

## 查询效果

### 人物状态

可用。`canonical` 能返回当前状态，但部分是长句，不是短字段。

### 关系

示例：

```text
唐昊 → 唐三 / 父子亲近 / 首次紧紧拥抱
唐昊 → 唐三 / 发展的态度 / 让唐三自己走今后的路
素云涛 → 唐三 / 态度 / 震惊并惋惜
唐三 → 大师 / 信任 / 有几分信任感
大师 → 唐三 / 收徒意向 / 提出愿收唐三为徒
```

双向查询：

```text
GET /api/relations?work=&between=大师,唐三
```

问题：

- 只覆盖显式关系
- 没有从所有 canonical 条目推导关系
- 覆盖率约 24 条 / 50 章

### 事件

示例，第 10 章：

```text
送走老杰克后唐昊斥魂师无用
参与者：唐三、唐昊、老杰克

蓝银草显现与唐昊失神
参与者：唐三、唐昊
```

事件质量较好，名称、参与者、因果段落都有。

### 认知

示例：

```text
唐三 / judgment / 玄天功第一重瓶颈已持续三个月
素云涛 / 成为魂师并不容易
```

可用，但第 25 章 `knowledge` 为 0，说明章节认知抽取有波动。

### 历史查询

`at_chapter=25`：

```text
canonical 100
events 99
relations 10
knowledge 84
threads 63
```

历史条目的 subject/predicate 已能从 entry_key 回填。

### 搜索

搜索“柔技”：

```text
chapter 29 / 第0029章 小舞，你还要啊？
chapter 25 / 第0025章 我叫小舞，跳舞的舞
```

现在命中结果带章节号。

### 上下文

第 51 章：

```text
canonical_archive 23974 chars
chapter_memory 1349 chars
```

问题：上下文偏大，接近 2.5 万字符，需要后续按实体/线程裁剪。

### 拆书

未完成。事件、线程、线网已有数据，但没有 `/api/deconstruct` 聚合查询。


历史查询：

```text
at_chapter=10
  canonical 43
  events 39
  relations 4
  knowledge 35
  threads 35

at_chapter=25
  canonical 100
  events 99
  relations 12
  knowledge 84
  threads 87

at_chapter=50
  canonical 162
  events 203
  relations 20
  knowledge 158
  threads 157
```

## 耗时

从上传第 13 章到第 50 章完成：

```text
约 2 小时 45 分钟
```

其中：

```text
chapter_memory：38 章
archive_update：38 章
```

这是串行流程的真实成本。

## 本轮暴露并修复的问题

### 1. 第 24 章 changes 缺 source_delta/entry_key

模型输出部分非状态 change：

```json
{"module": "五", "value": "小舞：掌握柔技", "op": "upsert"}
```

缺 `entry_key/subject/predicate`，整章应用失败。

已修：

```text
无 source_delta 且无 entry_key/subject
→ 跳过该条
→ warning
→ 不影响整章
```

### 2. 第 28 章 source_delta 越界

模型引用 `source_delta=0`，但该章 `state_deltas` 为空。

已修：

```text
source_delta 越界
→ 跳过该条
→ warning
```

### 3. 第 48 章 JSON 尾部多余内容

模型输出：

```text
{...}
额外说明
```

`json.loads` 失败，整个 job 失败。

已修：

```text
先 raw_decode 第一个 JSON 对象
忽略尾部文本
```

### 4. 重复 canonical key

出现：

```text
唐三修炼与唐门传承线:当前最新节点
七:唐三修炼与唐门传承线
```

原因：模型把模块前缀写进 entry_key。

已处理：

```text
dedupe 归档 1 条
```

未彻底解决：需要后续统一非状态 entry_key 规则。

### 5. 对账漏掉 archive 变更

`reconcile` 曾过滤 `after is None`，导致 archive 操作不参与匹配。

已修：

```text
archive/delete change 也参与精确 key 匹配
```

修复后 159/159。

## 结论

- 50 章可以跑通
- 状态对账 100%
- 关联查询可用
- 单章异常不再中断整批
- 串行吞吐仍然是瓶颈

## 下一步

```text
1. 批量 archive_update
2. 并发 chapter_memory
3. 非状态 key 统一
4. 50 章 A/B 对照
```
