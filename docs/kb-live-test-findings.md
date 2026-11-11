# 第三轮：真实调用与压测发现

> 环境：本地 `kb_server.py`（8785，ds7 副本 108 MB）+ 生产 `kg.zmteams.site`
> 模型：`wd.zmteams.site` / `global:deepseek-v4.1-flash`
> 方法：真实上传、真实解析、并发上传、并发检索、模型探针

---

## 1. 真实解析：连续两次失败

新建作品「第三轮验证」，上传 1 章约 120 字，`mode=state_update`：

```text
job=a8b1dfda...  t=5s  status=failed  archive=running
job=31cb7de3...  t=5s  status=failed  archive=running
```

批次诊断（数据库）：

```text
初次抽取阶段：G49f2f296de4f9878ba18 失败：模型响应不是 JSON 对象起始符（首字符 U+6211）
```

- 两次都失败，首字符都是 `我`
- `job.error` 为 `null`
- 服务日志没有 traceback
- 只能查数据库 `knowledge_parse_batch_item.diagnostic`

**结论**：解析对模型输出没有 JSON 契约、没有重试；失败信息不通过 API 返回。

---

## 2. 解析失败后，档案抽取仍继续

失败任务的 `archive` 状态最终是 `completed`：

```json
{"status":"completed","chapters":[{"chapter_index":1,
 "entities":[{"name":"杨凌"},{"name":"赵洵"},{"name":"马东"},
             {"name":"青云山"},{"name":"山门"},{"name":"灵米"},{"name":"铜钱"}],
 "story_records":[],"summary":null}]}
```

- 7 个实体，`dynamic_state` 全为空
- `hard_rules` 为空
- `related` 为 0
- 实体包含「铜钱」「灵米」「山门」这类物品/地点，`entity_type` 未设置

**结论**：档案抽取没有以解析成功为前提，也没有对抽取结果做完整性校验。

---

## 3. 编译真实结果

对同一作品编译第 2 章：

```text
模块一 | 模块二 | 模块四 | 模块五 | 模块三 | 模块六 | 模块七 | 模块八 | 模块九
```

- 顺序错误复现：模块 3 在 4/5 之后
- 输出长度 465 字符
- 模块五没有 hard_rules / 状态
- 模块四没有 1 度关系

**结论**：编译缺陷在真实数据上复现，且会继承档案抽取的空状态。

---

## 4. 并发上传：章节号撞车

两个并发 `POST /api/sources`，都不传 `chapter_index`：

```text
(201, chapter_index=1)
(201, chapter_index=1)
```

数据库：

```text
work 4f0e9754  chapter_index 1  count 2
```

**结论**：`next_chapter_index()` 的 `max+1` 无锁，也没有唯一约束。

---

## 5. 压测

### 本地（30 请求 / 10 并发）

```text
p50 11,026 ms
p95 21,638 ms
max 26,914 ms
errors 0
```

### 生产（20 请求 / 5 并发）

```text
p50 2,258 ms
p95 3,990 ms
max 4,654 ms
errors 0
```

单请求热查询此前为 0.8–1.1 s。

**结论**：查询是 CPU 密集 + SQLite 串行，并发下几乎没有并行收益；生产 p95 已接近 4 s。

---

## 6. 模型探针

直接调用同一端点：

```text
输入：解析提示词 + 原始章节文本
输出：'这段开头信息清楚：杨凌有1000铜钱……请问你希望我做什么：续写、润色……'
```

- 模型把任务当成对话，不输出 JSON
- 端点支持 `response_format={"type":"json_object"}`，返回带 ```json 围栏的对象
- 解析器没有使用 `response_format`，也没有围栏修复后的重试

**结论**：不是模型完全不能用，而是**输出契约没有强制**。

---

## 7. 数据完整性

第三轮数据库：

| 检查 | 结果 |
|---|---|
| 重复 `(work, chapter_index)` | 1 组（2 条） |
| `kb_entity_snapshot` | 7 条 |
| `dynamic_state` 为空 | 7 条（100%） |
| `kb_story_record` | 1 条（active_storyline） |
| 失败批次 | 2 个 |
| `kb_source_meta` | 3 条 |

**结论**：档案层当前产出“有实体、无状态”，不能支撑防吃书。

---

## 8. 新缺陷登记

| ID | 严重度 | 问题 | 证据 | 修复 | 新风险 |
|---|---|---|---|---|---|
| L1 | P0 | 解析无 JSON 契约/重试 | 2/2 非 JSON 失败 | `response_format` + Schema + 一次修复重试 | 延迟、模型不支持 |
| L2 | P0 | 失败原因不进 API | `error=null`，无日志 | job.error 填 diagnostic；记录 traceback | 暴露内部信息 |
| L3 | P0 | 解析失败仍跑档案 | archive=completed | 仅 `completed` 才进入档案 | 部分成功语义 |
| L4 | P1 | 档案实体类型/状态缺失 | 7 实体、0 状态、含铜钱/灵米 | 输出 Schema + 类型校验 + 必填状态 | 模型可能补造 |
| L5 | P0 | 编译模块顺序错 | 真实输出 1,2,4,5,3… | 固定顺序构建 | 需重排测试 |
| L6 | P0 | 并发上传章节号撞车 | 两条 chapter_index=1 | 唯一约束 + 事务内分配 | 旧数据冲突 |
| L7 | P1 | 并发查询串行 | p95 21.6 s / 3.99 s | 缓存、索引、只读连接、预热 | 缓存一致性 |
| L8 | P1 | 未使用 JSON mode | 模型返回对话 | 加 `response_format` | 兼容性 |
| L9 | P1 | 无原始输出/run 记录 | 无法复现失败 | `run` 表存 raw output | 存储增长 |
| L10 | P1 | 档案输出无校验 | 空状态也 completed | 必填字段 + 置信度 | 误拒 |
| L11 | P1 | 故事记录只有 active_storyline | 0 条状态/伏笔 | 抽取 Schema 覆盖 | 模型负担 |
| L12 | P1 | 生产 p95 接近 4 s | 压测 | 见 L7 | — |

---

## 9. 根因

1. **输出契约缺失**：提示词要求 JSON，但没有 API 级强制
2. **失败传播缺失**：异常被吞，状态置 failed，原因只进 DB
3. **阶段耦合**：档案抽取不依赖解析结果
4. **并发无锁**：章节号、任务、缓存
5. **SQLite CPU 串行**：查询没有缓存/并行策略

---

## 10. 修复优先级

**P0**：L1、L2、L3、L5、L6
**P1**：L4、L7、L8、L9、L10、L11、L12

---

## 11. 修复后的验证方法

1. 同一章连续解析 3 次，成功率 ≥ 2/3
2. 失败时 `job.error` 包含模型首字符/诊断
3. 解析失败时 `archive.status = skipped`
4. 并发上传 10 次，章节号 1..10 不重复
5. 编译模块顺序严格 1..9
6. 生产 5 并发 p95 < 2 s（目标）
7. 档案抽取：实体必须有 type，主角必须有 dynamic_state

---

## 12. 结论

真实测试确认：**当前解析链路在 `deepseek-v4.1-flash` 上不可用**，失败原因是模型输出非 JSON，而不是业务逻辑。

在修 L1–L3 之前，不应该继续做章节记忆/线网/档案代谢——否则会在“解析本身不稳定”的地基上继续加层。
