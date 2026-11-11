# 章节记忆质量评估

## 用途

对 `chapter_memory` 输出做离线评估，不调用模型：

```bash
PYTHONPATH=src python scripts/evaluate_chapter_memory.py --cases cases.jsonl
```

每行一个用例：

```json
{"chapter_index": 1, "text": "原文……", "output": {"scenes": [], "cognition": [], "state_deltas": [], "unresolved": []}}
```

## 指标

| 指标 | 含义 |
|---|---|
| `total` | 用例数 |
| `valid` / `valid_rate` | 通过全部校验的比例 |
| `scene_count_ok` | 场景数在 3–8 的用例数 |
| `evidence_errors` | evidence 缺失或不在正文的次数 |
| `cognition_errors` | 认知条目字段/类型错误 |
| `state_errors` | 状态增量字段错误 |
| `thread_errors` | 未决线程字段错误 |
| `chapter_errors` | 章节号不匹配 |
| `issue_counts` | 按问题前缀聚合 |
| `scene_keyword_recall` | 人工场景关键词命中率 |
| `cognition_type_recall` | 人工认知类型命中率 |
| `state_category_recall` | 人工状态分类命中率 |
| `thread_keyword_recall` | 人工线程关键词命中率 |

输出为 JSON，可写 `--report report.json`。

## 人工标注字段

```json
{
  "id": "dialogue_gap",
  "chapter_index": 1,
  "text": "正文",
  "expected": {
    "scene_keywords": ["山门", "拜师"],
    "cognition_types": ["judgment"],
    "state_categories": ["item"],
    "thread_keywords": ["裂痕"]
  }
}
```

## 实时跑模型

```bash
PYTHONPATH=src python scripts/evaluate_chapter_memory.py \
  --cases tests/fixtures/chapter_memory_golden.jsonl \
  --live \
  --base-url https://wd.zmteams.site/v1 \
  --api-key ... \
  --model global:deepseek-v4.1-flash
```

多次运行：

```bash
--repeat 3
```

输出包含 `runs`、`aggregate.mean/min/max`。

小样本基线见 [`chapter-memory-baseline.md`](chapter-memory-baseline.md)。

## 限制

评估器仍按**严格校验**统计 `valid`，不会把 warning 当成通过；这样指标偏保守。实际入库使用 [模糊兼容策略](fuzzy-compatibility.md)。

- 只评估结构、证据、章节号，不评估“因果是否写得好”
- 不判断场景是否真的完整，只判断长度与字段
- 不检查“未来信息泄漏”的语义，只检查章节号
- 需要人工 golden set 才能评估 POV、因果质量、信息差

## 数据

- 合成 3 例：`tests/fixtures/chapter_memory_golden.jsonl`
- 真实 5 章：`tests/fixtures/chapter_memory_golden_real.jsonl`
- 盲标 10 章：`tests/fixtures/chapter_memory_golden_blind.jsonl`

## 建议的 golden set

20–50 章，覆盖：

- 对话推进
- 战斗/求生过程
- 系统面板/数值
- 多视角与信息差
- 伏笔埋设/回收
- 长章节与章节边界
- 原文数值矛盾

每章人工标注：应出现的场景、认知条目、状态增量、未决线程。
