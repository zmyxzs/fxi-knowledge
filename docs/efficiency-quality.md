# 效率与质量预算

状态：2026-11-11（设计稿）

## 1. 先定义“质量”

这个知识库的质量不是“文笔好不好”，而是：

- 记忆是否完整
- 状态是否一致
- 谁知道什么是否写对
- 下一章上下文是否够用
- 回滚是否可靠
- 旧数据是否兼容

文笔质量属于写作层，不属于知识库。

---

## 2. 每章预算

目标：一章两次调用，不把整本档案塞进模型。

| 任务 | 调用 | 输入 | 输出 | 目标耗时 |
|---|---:|---:|---:|---:|
| chapter_memory | 1 | 8k–15k 字 | 2k–4k 字 | < 60s |
| archive_update diff | 1 | 6k–12k 字 | 1k–3k 字 | < 60s |
| 合计 | 2 | < 25k 字 | < 6k 字 | < 2min |

不包含：

- 线网（按批）
- 审计（按需）
- 重试（失败时一次）

---

## 3. 当前 vs 简化

| 项 | 当前 | 简化后 |
|---|---|---|
| chapter_memory | 生成 + 投影 canonical | 只生成证据 |
| archive_update | 输入整本档案，输出整本九模块 | 输入相关模块，输出 changes |
| story_record 写入者 | chapter_memory + archive_update + graph 回填 | archive_update |
| 去重 | 哈希/核心字段扫描 | `entry_key` 唯一索引 |
| 读取 | 多套记录混在一起 | canonical 当前视图 |
| context | 长期档案 + 章节记忆 + compiled 重复 | canonical 模块 + 上一章证据 + 线网 |
| 回滚 | 删除章节记录 | 撤销该章 changes |
| 历史 | 覆盖/删除 | `kb_archive_change` 保留 |
| 补丁 | 去重、旧记录清理、失败过滤 | 大部分消失 |

---

## 4. 效率设计

### 4.1 输入选择

`archive_update` 不读整本档案，只读：

```text
本章出现的角色 → 模块四/五
本章出现的势力 → 模块六
本章出现的线索 → 模块七/八
本章涉及的地图 → 模块三
```

模块选择由代码做，不让模型做。

### 4.2 输出选择

只输出 changes：

```json
{"changes": [{"entry_key": "...", "op": "upsert", "value": "..."}]}
```

不输出整本档案。

### 4.3 数据库

- `entry_key` 唯一索引
- `(work_id, module, entry_key)` 查询索引
- `chapter_index` 回滚索引
- canonical upsert O(log n)
- 不再每次插入扫描全表去重

### 4.4 上下文

`/api/context` 不再同时放：

```text
long_term_archive
chapter_memory
compiled_archive
```

只放：

```text
canonical 模块摘要
上一章证据摘要
当前线网
```

`compiled_archive` 是渲染结果，不应再作为独立输入重复。

### 4.5 并发

- 解析并发 1
- 一章一个 job
- 同一作品同一章只允许一个写入
- `base_version` 冲突返回 409

### 4.6 缓存

可缓存：

- 模块选择结果
- canonical 渲染结果
- 线网摘要

不可缓存：

- 当前章节 evidence
- 审计结论

---

## 5. 质量设计

### 5.1 抽取质量

指标：

- JSON 合法率
- 场景数
- evidence 逐字/归一化
- cognition 类型
- state 分类
- thread 状态

### 5.2 canonical 质量

指标：

- 条目覆盖率
- 动态状态是否覆盖旧值
- 历史事件是否追加
- 同一 `entry_key` 是否冲突
- 是否有重复
- 是否有孤儿实体

### 5.3 检索质量

指标：

- 实体查询召回
- story 分类召回
- context 是否包含关键实体/线程
- 分页是否完整

### 5.4 审计质量

指标：

- 账本错误发现率
- 误报率
- 死亡/生命周期冲突
- 硬规则/红线冲突

### 5.5 回滚质量

指标：

- 删除第 N 章后，canonical 是否等于第 N-1 章
- 重跑第 N 章是否幂等
- 旧 changes 是否被撤销

### 5.6 人工质量

指标：

- 审核接受率
- 每条 diff 的拒绝原因
- 人工修改量

---

## 6. 质量门禁

### 每章

- JSON 可解析
- canonical apply 成功
- 无 `entry_key` 冲突
- 章节顺序正确
- evidence 缺失只 warning

### 每批

- 线网能关联章节记忆
- 二轮修正有证据
- 没有跨批重复

### 每次发布

- 全量测试
- 6 本 × 2 章抽样
- 10 章连续更新
- 回滚演练
- 旧作品 smoke

---

## 7. 效率与质量的冲突

| 冲突 | 选择 |
|---|---|
| 少调用 vs 质量 | 两调用：证据 + 状态；不合成一次 |
| 大模型 vs 成本 | 证据用中等，状态合并用更强 |
| 全档案 vs 注意力 | 只注入相关模块 |
| diff vs 完整性 | diff + 代码渲染 + 全量校验 |
| 自动 active vs canon | 章节记忆自动；长期档案人工 accept |
| 多写 vs 单一真源 | 只允许 archive_update 写 canonical |
| 去重 vs 合法重复 | 按 `entry_key` + 章节 |
| 快速失败 vs 可恢复 | hard fail 只限无法投影；其余 warning |

---

## 8. 不能接受的“优化”

1. 为了少调用，把证据和状态合成一次
2. 为了省 token，静默截断长章
3. 为了过校验，让模型编造字段
4. 为了兼容，保留两套在线真源
5. 为了快，跳过回滚验证
6. 为了指标，只统计 JSON 合法率

---

## 9. 测量计划

### 基线

- 3 个合成用例
- 5 个真实章节
- 10 个盲标章节
- 6 本 × 2 章抽样

### 指标

```text
valid_rate
evidence_exact_rate
cognition_type_recall
state_category_recall
thread_keyword_recall
canonical_duplicate_rate
canonical_conflict_rate
rollback_equality
context_recall
review_accept_rate
```

### 目标

第一版目标不是 100%，而是：

```text
valid_rate >= 0.9
evidence_exact_rate >= 0.95
canonical_duplicate_rate = 0
canonical_conflict_rate = 0
rollback_equality = true
```

---

## 10. 结论

效率靠：

- 两次调用
- 模块选择
- diff 输出
- 唯一索引
- 不重复上下文

质量靠：

- 证据层与 canonical 层分离
- 代码应用 diff
- 变更日志
- 回滚
- 审核
- 跨书抽样

两者不冲突，冲突的是“想用一次调用同时解决所有问题”。
