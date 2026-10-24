# 既有失败的 triage（22 → 14）

- 日期：2026-10-24
- 方法：`pytest --runxfail --tb=short` 逐条跑，再对照测试夹具与实现
- 背景：这些用例在原项目里同样失败；本表只判断「为什么失败、能不能安全修」

## 结论

| 分类 | 数量 | 处理建议 |
|---|---:|---|
| 夹具/断言漂移（已修） | 8 | 已从 xfail 登记表移除 |
| 夹具/断言漂移（待修） | 6 | 可以修，但需要重写夹具或改断言 |
| 真实行为差异 | 8 | 不要用改测试的方式「修」，需要产品判断 |

当前基线：**1782 passed, 7 skipped, 14 xfailed**（本轮开始前是 1774 / 22）。

## 已修复（8）

| 用例 | 根因 | 修法 |
|---|---|---|
| `test_repository_purge_batching` | 夹具模拟旧 SQLite 变量上限，但 schema 停在加列之前，缺 `published_graph_version_id` 和两张表 | 补列和表 |
| `test_graph_parser_coverage_delta` | `current_segments` 只给了 `ref`，实现需要 `text` / 偏移 / `evidence_span_id` | 补全 segment |
| `test_parser_repair_anchor_identity` | 生产代码对 row repair 传 `response_schema=None`，假 runner 无条件构造 `Draft202012Validator(None)` | 判空后再校验 |
| `test_event_resolution_history` | `_event_attempt_decisions` 已移到 `repository_parse_event_attempt_validation`，测试还在 patch 旧模块 | 改 monkeypatch 目标 |
| `test_query_material_sharing::test_row_without_bound_evidence...` | 错误文案从 "outside its source row" 变成 "outside its exact source row" | 更新正则 |
| `test_embedding_enqueue_receipts` | 返回的 `result` 新增 `scanned` / `invalid_candidate_count` | 更新期望字典 |
| `test_embedding_candidate_migration[True/False]` | 在**已迁移到最新**的库上把 `user_version` 改回 80，再 `initialize()`，于是 82 之后的迁移全部重跑；对话修订迁移的 DDL 正则匹配不到旧唯一约束而报错 | 改为只跑到 v80，再显式应用 v81 两次；不再重跑后续迁移 |

最后一条不是产品缺陷：迁移本来就不是可重复执行的，测试人为回退版本号才触发了重放。

## 待修（6）：夹具或断言漂移

| 用例 | 根因 | 修复风险 |
|---|---|---|
| `test_dialogue_partial_resume::test_revision_migration...` | 把 `MIGRATIONS` 截到 ≤81 后仍调用当前的 `add_text_source`，而它需要 `source.edit_revision`（v97 才加） | 中高：要么用裸 SQL 造旧库，要么给旧库场景单独写导入路径 |
| `test_published_graph_version_pointer::test_v82_to_83...` | 同上，v82 库 + 当前 `add_text_source` | 中高：同一条路 |
| `test_knowledge_graph_parse_cleanup::...` | 夹具写入的候选内容还是旧 schema（缺 `category/subject/predicate/value/claim_basis`，`identity_resolution` 取值过期） | 中：按当前 `KnowledgeGraphCandidateContent` 重写夹具 |
| `test_graph_parser_identity_recovery`（2 条） | `recover_completed_identity_result` 返回 `None`；夹具的 attempts/rows 可能缺新字段或方法 | 中：对照 recovery 准入条件逐项补 |
| `test_parse_replacement::...` | 被 `protected_fact_relation_index_does_not_match_latest_content` 挡住 | 高：需要判断是夹具索引过期还是替换逻辑真有问题 |

两条 legacy 迁移用例的关键约束：`source.edit_revision` 是 v97 加的，而它们模拟 v81/v82 的库，却调用当前版本的 `add_text_source`。要么绕过该 API，要么把测试目标改成「v96 之前」的兼容路径——后者等于改测试意图，需要先确认产品是否还要支持这么老的库。

## 真实行为差异（8）：不要只改测试

| 用例 | 现象 | 涉及语义 |
|---|---|---|
| `test_association_entity_source_scope` + `test_retrieval_search_scope`（同一用例两份） | 显式 source/folder 选择器没有保持 OR 语义，候选集合不同 | 检索范围契约 |
| `test_planning_study_reading[False]` | 旧保存请求复用时返回了另一个 id（owner/current key 优先级） | 规划请求复用 |
| `test_query_equivalent_operations[writing]` | 缓存 key 在 writing 下也变化，测试只期望 learning 变化 | 查询缓存契约 |
| `test_query_register_observations`（2 条） | learning 范围的缓存 key 也变化 | 观察范围与缓存 |
| `test_query_grounded_scene_read` | 场景窗口预算 2400，实际引文 6000 | 阅读窗口裁剪 |
| `test_query_material_sharing::test_shared_text...` | 外来候选决策仍被判为 supported | 材料共享边界 |

这些要么是快照里的真实缺陷，要么是产品语义在抽取后发生了变化。修它们需要先确定「期望行为是哪一边」，否则改测试只会掩盖问题。

## 建议顺序

1. 先处理 `test_knowledge_graph_parse_cleanup` 和 `test_graph_parser_identity_recovery`：都是夹具 schema 漂移，按当前模型补字段即可，风险可控。
2. `test_parse_replacement` 需要先定位 `protected_fact_relation_index` 的期望内容，再决定改夹具还是改逻辑。
3. 两条 legacy 迁移用例留到最后，并先确认「是否还需要支持 v81/v82 旧库升级」。
4. 行为类问题逐条对照原项目确认期望值，不要只改断言。

## 复跑

```powershell
$env:PYTHONPATH='D:\Projects\fxi-knowledge\src'
python -m pytest -q --runxfail --tb=short <node-id>...
```
