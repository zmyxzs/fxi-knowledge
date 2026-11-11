# 章节记忆基线（3 例）

> 这是小样本结构基线，不是统计结论。用于验证提示词改动方向。

## 数据

`tests/fixtures/chapter_memory_golden.jsonl`

| 用例 | 覆盖 |
|---|---|
| `dialogue_gap` | 对话、信息差、判断 |
| `combat_panel` | 战斗过程、数值/资源变化 |
| `foreshadow` | 神秘物品、未决线程 |

## 结果

模型：`global:deepseek-v4.1-flash`

| 指标 | prompt v1 | prompt v2 |
|---|---|---|
| total | 3 | 3 |
| valid | 3 | 3 |
| valid_rate | 1.0 | 1.0 |
| scene_count_ok | 3 | 3 |
| evidence_errors | 0 | 0 |
| cognition_errors | 0 | 0 |
| state_errors | 0 | 0 |
| thread_errors | 0 | 0 |
| scene_keyword_recall | 0.667 | 0.833 |
| cognition_type_recall | 0.5 | 0.5 |
| state_category_recall | 1.0 | 1.0 |
| thread_keyword_recall | 0.0 | 1.0 |

prompt v2 增加了两条硬要求：

- 出现怀疑/推测/隐瞒/信息不对称时，`cognition` 不能为空
- 出现未解释异常、神秘物品、承诺、新危机时，`unresolved` 至少一条

## 仍然不足

- `cognition_type_recall` 仍为 0.5：foreshadow 用例没有输出“判断”
- 场景关键词仍有 1/6 未命中
- 3 例太少，不能代表长篇
- 评估器不判断因果质量、POV 语义、未来泄漏语义

---

## 真实 5 章基线（神域囚徒 1–5）

数据：`tests/fixtures/chapter_memory_golden_real.jsonl`，取自 `Fxi-novel` 的《神域囚徒！》前 5 章。

模型：`global:deepseek-v4.1-flash`，prompt v2。

| 指标 | 结果 |
|---|---|
| total | 5 |
| valid | 4 |
| valid_rate | 0.8 |
| scene_count_ok | 5 |
| evidence_errors | 1 |
| cognition_errors | 0 |
| state_errors | 0 |
| thread_errors | 0 |
| scene_keyword_recall | 1.0 |
| cognition_type_recall | 1.0 |
| state_category_recall | 0.889 |
| thread_keyword_recall | 0.75 |

失败用例：

```text
real_ch2 scenes[4] 的 evidence 未在正文逐字出现：你也不知道我的名字？
```

模型改写了原文标点/措辞，说明“evidence 必须逐字”仍是主要失败点。

### 标注偏差

这批 `expected` 是在看过模型输出后调整过关键词的，属于**轻量标注**，不是盲标；指标偏乐观。下一轮应：

1. 先盲标 10–15 章
2. 再跑模型
3. 不允许根据输出改标注

---

## 盲标 10 章基线（神域囚徒 6–15）

数据：`tests/fixtures/chapter_memory_golden_blind.jsonl`。  
标注只依据章节正文开头与标题，未查看模型输出；但只做了轻量关键词标注，不是全量人工语义标注。

| 指标 | 结果 |
|---|---|
| total | 10 |
| valid | 10 |
| valid_rate | 1.0 |
| scene_count_ok | 10 |
| evidence_quotes | 248 |
| evidence_errors | 0 |
| evidence_normalized_only | 0 |
| evidence_missing | 0 |
| cognition_errors | 0 |
| state_errors | 0 |
| thread_errors | 0 |
| scene_keyword_recall | 1.0 |
| cognition_type_recall | 1.0 |
| state_category_recall | 0.8 |
| thread_keyword_recall | 0.8 |

说明：

- 这次 248 条 evidence 全部逐字命中，和上一轮 `real_ch2` 的标点改写失败不一致，说明模型输出有随机性
- `state_category_recall` 0.8、`thread_keyword_recall` 0.8 仍是主要缺口
- 标注是“看开头 + 标题”的轻量标注，不能替代逐段人工标注

---

## 稳定性：盲标 10 章重跑 3 次

同一批 10 章、同一 prompt v2、同一模型，连续跑 3 次。

| 指标 | run1 | run2 | run3 | 平均 |
|---|---|---|---|---|
| valid_rate | 0.9 | 0.9 | 1.0 | 0.933 |
| evidence_errors | 0 | 2 | 0 | 0.667 |
| evidence_normalized_only | 0 | 0 | 0 | 0 |
| evidence_missing | 0 | 2 | 0 | 0.667 |
| scene_keyword_recall | 1.0 | 1.0 | 1.0 | 1.0 |
| cognition_type_recall | 0.917 | 1.0 | 1.0 | 0.972 |
| state_category_recall | 0.733 | 0.8 | 0.867 | 0.8 |
| thread_keyword_recall | 0.6 | 0.8 | 0.8 | 0.733 |

### 失败模式

1. **ch15 不稳定**
   - run1：`state_deltas[0] resource 缺 field/amount`
   - run2：`state_deltas[5]` 与 `unresolved[5]` 的 evidence 改写成“脑海再次多了关于崩山拳的记忆。”
   - run3：通过
2. **ch14 状态分类稳定缺失**
   - 三次都只输出 `character`，没有 `relation`
3. **ch7/ch8/ch10 线程召回波动**
   - 模型确实输出线程，但命名与人工关键词不一致
   - 例如 ch7 输出“神域本质与回归方法”，人工关键词是“活下去”
   - 说明线程召回低有一部分是标注口径问题，不全是模型漏抽
4. **evidence 失败不是标点问题**
   - `evidence_normalized_only` 三次都是 0
   - 失败都是真正的改写/缺失

### 结论

- 结构合法率约 0.93，不是 1.0
- 场景召回稳定
- 状态分类和线程命名是主要波动源
- 单次评估不足以判断提示词好坏，至少跑 3 次

## prompt v4：模糊兼容（未重新基线）

根据失败模式，现在改为两层校验：

- Hard：输出不是对象、`scenes` 为空、JSON 不可解析
- Soft：场景数、段落长度、evidence、类型/分类、缺字段全部降级为 warning

prompt 也不再强制：

- 有怀疑就必须有 cognition
- 有异常就必须有 unresolved

改成“有明确内容时优先写，没有可空，不得虚构”。

上面所有表格仍是 prompt v2 的严格校验结果；v4 需要下一轮用 `--repeat 3` 重新跑，重点看：

- warning 数量
- 投影成功率
- 人工抽检是否变差

## 复现

```bash
PYTHONPATH=src python scripts/evaluate_chapter_memory.py \
  --cases tests/fixtures/chapter_memory_golden.jsonl \
  --live \
  --base-url https://wd.zmteams.site/v1 \
  --api-key ... \
  --model global:deepseek-v4.1-flash
```
