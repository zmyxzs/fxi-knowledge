# 解析契约评审：为什么不是“模型不够强”

> 结论：当前解析失败的主要原因是**输出契约过重、图装配负担转嫁给模型、同一事实多套表示**，不是 `deepseek-v4.1-flash` 能力不足。

---

## 1. 对照实验

同一个模型、同一段正文（约 120 字），只把契约换成“实体 + 事实”：

```text
{"entities":[{"name","type","aliases"}],
 "facts":[{"subject","predicate","value","evidence"}]}
```

结果：

```text
status 200
entities 8
facts 10

杨凌 · 拥有 · 一千铜钱 · “杨凌身上有一千铜钱。”
杨凌 · 花费 · 四百铜钱 · “他花掉四百铜钱买了灵米”
杨凌 · 剩余 · 六百铜钱 · “还剩六百铜钱”
赵洵 · 收徒 · 杨凌 · “赵洵收他为徒”
```

- JSON 合法
- 实体、别名、数值、证据都正确
- 没有一次重试

同一模型、同一正文，换成现有解析契约：

```text
schema_version 错误、quantities/events 互引失败、
对白说话者没有 canonical entity、event_entity_ref 关联失败
```

**说明：模型能做抽取，做不了“在文本抽取的同时装配一张带本地引用、双向关联、多集合一致性的知识图”。**

再按“记忆契约”试一次（entities / facts / state_changes / scenes / threads，全部用名字、不用 ID）：

```text
status 200
entities 6
facts 6
state_changes 1
scenes 3
threads 2

state_changes: 杨凌 铜钱 一千铜钱 → 六百铜钱
scenes:       杨凌用四百铜钱买灵米，身上铜钱从一千减至六百
threads:      入门、买灵米
```

同一段正文、同一个模型，**记忆契约一次成功**。

---

## 2. 当前契约的具体问题

### P0-1 模型被迫做“图装配”，不是抽取

现有输出要求：

- 实体 `N1..Nn`
- 事件 `V1..Vn`，且每个事件必须有一个 **event 类实体** `N#` 作为 `event_entity_ref`
- 数量 `Q1..Qn`，通过 `event_ref` 指向事件
- 规则 `R#`、实例 `I#`
- 事件侧反向引用 `quantity_ref` / `measurement_refs` 由代码生成，但模型仍要理解
- 对白 `D#` 必须对齐确定性片段

模型要先“发明一张图”，再保证所有引用闭合。这是编译器/图编辑器的活，不是抽取。

**证据**：失败响应里模型确实创建了 `N7`、`N8` 作为事件实体，然后 `asset_ref`、`quantity_ref`、`operation` 对不上。

### P0-2 同一事实有四套表示

“杨凌花掉 400 铜钱”可以同时写成：

| 位置 | 写法 |
|---|---|
| facts | `杨凌 · 花费 · 四百铜钱` |
| events | `V1 consume asset=N5` |
| quantities | `Q2 event_ref=V1 operation=consume amount=400` |
| temporal_state | `铜钱 1000→600` |

提示词没有明确“只能选一种”，模型就四套都写，然后互相冲突。

**证据**：失败响应里 `facts` 有花费，`events` 有 consume，`quantities` 有 Q2，且 Q1/Q3 又是余额。

### P0-3 字段与枚举过多

| 模型 | 字段数 | 必填 |
|---|---|---|
| `SourceLocalEventOutput` | 26 | 8 |
| `SourceLocalQuantityOutput` | 14 | 7 |
| `KnowledgeGraphFactOutput` | 22 | 6 |
| `KnowledgeGraphEntityOutput` | 8 | 7 |
| `KnowledgeGraphParseOutput` | 9 | 4 |

还要判断：

- `claim_basis`（6 种）
- `disposition`
- `asset_granularity` / `asset_nature`
- `operation`（10 种）
- `state_slot`
- `audience` / `audience_refs`

这些不是“抽一句话”，是同时做分类学、图论和证据对齐。

### P0-4 反向引用规则自相矛盾

提示词同时说：

- “不要输出事件侧的 `quantity_ref` / `measurement_refs`，代码会派生”
- 但事件 schema 里有这两个字段
- 一旦模型输出，就会被当成“模型伪造反向引用”拒绝

模型无法从自然语言里稳定区分“我该省略”与“字段存在但我不能填”。

### P0-5 一个资产事件只能有一个数量，但“余额”天然是第二个数量

正文：

```text
有一千铜钱 → 花掉四百 → 还剩六百
```

模型自然输出：

```text
Q1 初始 1000
Q2 花费 400，event_ref=V1
Q3 剩余 600
```

但 Q2、Q3 都指向 V1，校验要求“唯一关联”，于是失败。

这不是模型笨，是契约没有给“余额快照”一个明确的独立位置。

### P0-6 对白标注要求“说话者 canonical entity + D# 对齐”

模型要先：

1. 找到对白片段
2. 映射到确定性 `D#`
3. 解析说话者
4. 确认说话者有 `N#` 实体
5. 保证 evidence 覆盖精确片段

任何一步错，整条失败。对白在网文里又是最高频内容。

### P0-7 提示词过长、规则密度过高

- 文件 18.6 KB，65 行
- 实际 system 消息 9885 字
- 单段包含 5–8 条并列约束
- 示例只覆盖 `acquire`，没有 `consume`、余额、对白、关系

模型注意力被大量“不要/禁止/必须”稀释。

### P0-8 schema_version 多套并存

| 场景 | 版本 |
|---|---|
| 抽取 | 4 |
| 身份归并 | 2 |
| 事件对账 | 1/2 |
| 行修复 | 1 |

提示词示例里也混用。模型输出 3 或不输出，直接失败。

### P1-1 证据唯一性过严

`evidence_quote` 必须在本段中**唯一出现**。短句“还剩六百铜钱”可能重复；模型改写一个字就失败。

### P1-2 strict + extra=forbid

任何多余字段都拒绝。模型习惯补 `name`、`quantity`、`unit` 等辅助字段，直接失败。

### P1-3 错误信息不可行动

```text
数量与事件未形成唯一关联（事件侧反向引用由代码生成）
```

模型不知道要改哪一行、删哪个字段。重试提示也只能泛泛说“请输出 JSON”。

### P1-4 一次调用承担太多阶段

抽取 + 分类 + 引用分配 + 证据对齐 + 图一致性 + 对白标注，全在一次 JSON 里完成。任何局部错误都让整批失败。

---

## 3. 为什么会“冲突/无效内容”

1. **多套表示**：facts/events/quantities/temporal_state 对同一变化各写一遍，数值不一致
2. **引用闭合**：模型给了 `event_ref`，但 `asset_ref` 与事件资产不一致
3. **余额当事件数量**：Q3 既想记余额，又想挂到 V1
4. **事件实体化**：每个事件都要造一个 `event` 实体，模型造了但没被引用
5. **对白实体缺失**：说话者没有 canonical entity，整条 annotation 无效
6. **schema 版本**：输出 3 或字符串 "4"，校验失败

这些都不是“知识不够”，是**契约设计把一致性责任推给了模型**。

---

## 4. 应该怎么改

### 4.1 原则

> 模型只负责“这句话里有什么”，代码负责“图怎么连、ID 怎么分配、冲突怎么合并”。

### 4.2 三阶段契约

**阶段 A：实体 + 事实（必选）**

```json
{
  "entities": [{"name": "杨凌", "type": "character", "aliases": []}],
  "facts": [{"subject": "杨凌", "predicate": "剩余铜钱", "value": "600", "evidence": "还剩六百铜钱"}]
}
```

- 不用 `N#`，用名字
- 不用 `event_entity_ref`
- 代码负责实体解析、别名归并、ID 分配

**阶段 B：变化事件（可选）**

```json
{
  "changes": [
    {"subject": "杨凌", "asset": "铜钱", "direction": "decrease",
     "amount": 400, "unit": "铜钱", "reason": "买灵米", "evidence": "花掉四百铜钱"}
  ]
}
```

- 平铺，不要求事件实体
- 不要求反向引用
- 代码生成 event/quantity 图和余额

**阶段 C：对白与关系（可选）**

```json
{
  "dialogues": [{"speaker": "赵洵", "quote": "从今天起，你就是青云山弟子。"}],
  "relations": [{"from": "赵洵", "to": "杨凌", "kind": "师徒"}]
}
```

- 用名字，不用 D#；代码按 quote 定位
- 说话者先走实体解析，再写 annotation

### 4.3 每阶段独立校验、独立重试

- 阶段 A 失败只重跑 A
- 阶段 B 失败只重跑 B
- 不再“一处数量错，整章失败”

### 4.4 对知识库记忆层的最小契约

记忆层真正需要的是：

```json
{
  "entities": [...],
  "facts": [...],
  "state_changes": [{"entity","field","from","to","evidence"}],
  "scenes": [{"summary","participants","evidence"}],
  "threads": [{"name","transition","evidence"}]
}
```

**不需要** `event_entity_ref`、`quantity_ref`、`D#`、`R#`、`I#`。

---

## 5. 短期可落地的改法

### 方案一：轻量解析模式（推荐）

新增 `mode=memory`：

- 只跑阶段 A（实体 + 事实）
- 代码做别名归并、账本加减、快照
- 不碰 events/quantities/rules/dialogue

收益：

- 同一模型已验证可稳定输出
- 解析成功率可从 `partial` 提升到 `completed`
- 记忆层所需数据齐全

**已落地**（`/api/parse` `mode=memory`）。实测同一章：

```text
status=completed  archive=completed  12s
entities=6  facts=7  scenes=3  threads=2
杨凌 dynamic={"铜钱":"六百铜钱","inventory_ledger":{"currency":{"铜钱":600}}}
```

### 方案二：保留全量解析，但拆成三次调用

- A：实体 + 事实
- B：变化 + 数量
- C：对白 + 关系
- 每阶段独立 schema、独立重试、独立校验

成本增加约 2 倍，但成功率显著提升。

### 方案三：把现有契约改成“平铺 + 代码装配”

- 所有记录用名字，不用 N/V/Q
- 代码生成引用和事件实体
- 兼容旧数据，但需要迁移

---

## 6. 验收指标

| 指标 | 目标 |
|---|---|
| 阶段 A JSON 合法率 | ≥ 98% |
| 阶段 A 实体/事实召回 | ≥ 90% |
| 证据逐字匹配率 | ≥ 95% |
| 整章 completed 率 | ≥ 90% |
| 单章调用次数 | ≤ 2 |
| 单章耗时 | ≤ 60 s |

---

## 7. 结论

- 不是模型不够强，是**契约让模型做图装配**
- 同一模型在“实体 + 事实”契约下已验证可用
- 现有 events/quantities/dialogue 契约应拆阶段、去 ID、去反向引用
- 记忆层不需要全量图契约，建议先落 `mode=memory`

下一步建议：实现 `mode=memory` 的轻量抽取，先让 `completed` 稳定，再决定是否保留全量图解析。
