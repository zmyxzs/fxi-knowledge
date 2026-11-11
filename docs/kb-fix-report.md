# P0 修复报告（第三轮之后）

> 范围：JSON 契约、失败传播、档案门禁、编译顺序、并发章节号
> 验证：本地真实解析 + 全量测试

---

## 1. 修了什么

| 编号 | 问题 | 修复 |
|---|---|---|
| L1 | 模型把解析任务当对话，返回非 JSON | 1) `ModelRoute.merge_system_prompt`：把 system 提示折进首条 user 消息；2) `chat_validated(retry_invalid_output=True)`：校验失败时追加纠正消息重试一次；3) 档案抽取同样合并 system |
| L2 | 失败原因不进 API | 从 `knowledge_parse_batch_item.diagnostic` 取首条诊断写入 `job.error`；`_parse_thread` 记录 traceback |
| L3 | 解析失败仍跑档案 | 仅 `status=completed` 才进入 `_run_archive_update`，否则 `archive.status=skipped` |
| L5 | 编译模块顺序错 | 改为固定顺序组装：1→2→3→4→5→6→7→8→9 |
| L6 | 并发上传章节号撞车 | `reserve_source_meta` 在单事务内分配并插入；`(work_id, chapter_index)` 唯一索引；重复显式章节号返回 400 |
| 附加 | 重试被旧批次卡住 | `/api/parse` 新增 `force: true`，以 `force_reparse` 重建批次 |
| 附加 | 死亡状态被复活 | 省略 lifecycle 时继承上一快照 |
| 附加 | 关系被整体替换 | relations 按 target 合并 |
| 附加 | 新 timer 当章被扣 | timer 记录 `created_chapter`，同章不减 |
| 附加 | 未来 timer 泄漏 | 编译 timer 查询带 `at_chapter` |

---

## 2. 关键发现：system 提示被忽略

直接回放解析请求：

```text
system: 9885 字解析提示
user:   860 字 JSON 输入
```

模型返回：

```text
I can see you've provided a structured JSON payload, but the instruction
you want me to follow isn't included. What would you like me to do with it?
```

把同一段 system 内容折进 user 消息后：

```json
{"schema_version":4,"covered_segment_refs":["E1"],
 "entities":[{"ref":"N1","category":"character","name":"杨凌",...}]}
```

结论：`wd.zmteams.site` 的 `deepseek-v4.1-flash` 在当前请求形态下没有按 system 角色执行指令；折进 user 后恢复。

---

## 3. 真实解析复测

修复后同一章连续解析：

```text
run1  status=partial  archive=skipped  42s
      err=单行修复后仍有局部校验错误：quantities/events 未形成唯一关联
run2  status=failed   3s
      err=解析批次尚未显式恢复；未重复调用模型
run3  status=failed   3s
      同上
```

加 `force=true` 后：

```text
run4  status=partial  archive=skipped  39s  err=null
run5  status=partial  archive=skipped  54s
      publish failed：published dialogue speaker identity has no canonical entity
```

结论：

- 非 JSON 问题已解决，模型现在能输出结构化 JSON
- 解析仍卡在**语义校验**：数量/事件双向关联、对白说话者缺少规范实体
- 这是模型输出质量/提示词约束问题，不是服务端异常

---

## 4. 并发章节号复测

两个并发上传，均不传 `chapter_index`：

```text
(201, chapter_index=1)
(201, chapter_index=2)
```

修复前两条都是 1。

---

## 5. 编译顺序复测

```text
模块一 | 模块二 | 模块三 | 模块四 | 模块五 |
模块六 | 模块七 | 模块八 | 模块九
```

修复前为 `1,2,4,5,3,6,7,8,9`。

---

## 6. 测试

```text
1821 passed, 7 skipped, 1 warning, 20 subtests passed in 605s
```

新增测试：

- 编译九模块顺序
- 省略 lifecycle 保持 dead
- relations 合并
- timer 同章不扣
- 重复章节号拒绝
- system 提示折叠/保留

---

## 7. 仍未解决

1. 解析的语义校验：quantity/event 互引、dialogue speaker canonical entity
2. `partial` 状态缺少 `job.error`（batch 无 item diagnostic 时）
3. 档案抽取质量：实体类型、空状态
4. 并发查询性能（p95 仍高）

---

## 8. 第二轮语义修复尝试

在 system 折叠之后，模型已能输出结构化 JSON，但仍卡在语义校验。追加了两项：

1. **提示词**：资产事件最多一个 `event_ref` 数量；余额/剩余必须独立；`operation` 只用枚举（余额用 `balance_snapshot`）
2. **重试提示**：校验错误涉及 quantity 时，追加同样的规则；`schema_version` 错误时提示必须是整数 4

结果（干净作品、无 force）：

```text
status=partial  archive=skipped  36s
err=初次抽取阶段 ... quantities[1]: event measurements cannot carry an asset reference；
    events[0]: 模型输出的实体关联或原文证据校验未通过
```

说明：模型仍在 `quantities`/`events` 的互引与资产语义上出错。这不是服务端逻辑能完全兜底的，需要更强的模型或更小的抽取 schema。

建议下一步二选一：

- 换更强的模型跑解析（当前端点只有 `deepseek-v4.1-flash`）
- 增加“轻量解析模式”：只抽实体/事实，不抽事件/数量，先让 `completed` 稳定

下一步建议：针对 1 调整解析提示词/输出 schema，或换用更强的模型；在解析能稳定 `completed` 之前，不进入章节记忆层。
