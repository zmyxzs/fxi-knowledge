# 设定一致性检查

给新建作品做入库后的确定性检查，回答「有没有前后矛盾 / 哪些数值发生了变化 / 哪些需要人工复核」。

## 使用

```powershell
# 1. 导入并解析作品（发布后会自动重建 FTS）
# 2. 跑检查
python scripts/audit_consistency.py data/verify.sqlite3 --work 神域囚徒 --output data/audit.json
```

输出四类结果：

| 字段 | 含义 |
|---|---|
| `conflicts` | **同一章内**、单值属性出现不同值（最可能是真冲突） |
| `state_changes` | 跨章数值变化（等级/力量/敏捷等正常成长） |
| `needs_review` | 非数值、较短、跨章不同的值，需要人工或 LLM 判断 |
| `category_mismatches` | 同一身份组内实体类别不一致 |
| `dangling_targets` | 关系指向未发布的身份组 |

## 判定规则

1. 只检查单值谓词：等级、力量、敏捷、精神、精神力、修为、境界、年龄、性别、生命值、气血、职业、身份、位置、状态、品级
2. 数值先归一化（`149` / `149 点` / `一百四十九` → 同一数值）
3. **同章不同值** → `conflicts`
4. **跨章不同值** → `state_changes`（视为成长/变化，不报冲突）
5. 非数值但较短（≤12 字）→ `needs_review`

## 10 章实测

734 条已发布记录：

- `conflicts`：1 条
  - 徐文君的儿子 · 修为：0313 章同时出现「上五品」「下六品」
- `state_changes`：杨凌 力量 149→259→300、敏捷 53→103→293 等
- 其余为 `needs_review` 与类别/悬空引用统计

## 适用场景

- 新作品入库后先跑一遍，处理 `conflicts`
- 连载更新后跑增量检查，确认没有同章矛盾
- `state_changes` 可作为「数值成长时间线」直接使用
- 具体问题仍可用 `consistency_check` 问答（planner 会拆 requirement 并给证据）

## 限制

- 只覆盖单值谓词；技能列表、效果列表等多值谓词不参与冲突判定
- 依赖 `story_time_expression` 区分时间；没有时间表达时，跨章差异一律视为变化
- `needs_review` 不做语义判断；需要时再对单条调用 LLM 复核
- 不检查事件顺序、因果和视角知识边界（这些由 event/claim 投影和问答覆盖）
