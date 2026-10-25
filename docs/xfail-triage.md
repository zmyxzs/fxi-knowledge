# 既有失败的 triage（22 → 0）

- 日期：2026-10-24
- 方法：`pytest --runxfail --tb=short` 逐条跑，再对照测试夹具与实现
- 背景：这些用例在原项目里同样失败；本表只判断「为什么失败、能不能安全修」

## 结论

| 分类 | 数量 | 处理建议 |
|---|---:|---|
| 夹具/断言漂移（已修） | 20 | 已从 xfail 登记表移除 |
| 旧库升级用例（已删除） | 2 | 明确不支持旧库升级，直接删除 |
| 真实行为差异 | 0 | — |

当前基线：**1780 passed, 7 skipped, 0 xfailed**（本轮开始前是 1774 / 22）。

> 2026-10-25 的迁移链 squash 又删除了 6 个纯迁移测试文件，因此用例总数从 1801 降到 1787；本表记录的是 triage 当时的状态。

## 已修复（20）

| 用例 | 根因 | 修法 |
|---|---|---|
| `test_repository_purge_batching` | 夹具模拟旧 SQLite 变量上限，但 schema 停在加列之前，缺 `published_graph_version_id` 和两张表 | 补列和表 |
| `test_graph_parser_coverage_delta` | `current_segments` 只给了 `ref`，实现需要 `text` / 偏移 / `evidence_span_id` | 补全 segment |
| `test_parser_repair_anchor_identity` | 生产代码对 row repair 传 `response_schema=None`，假 runner 无条件构造 `Draft202012Validator(None)` | 判空后再校验 |
| `test_event_resolution_history` | `_event_attempt_decisions` 已移到 `repository_parse_event_attempt_validation`，测试还在 patch 旧模块 | 改 monkeypatch 目标 |
| `test_query_material_sharing::test_row_without_bound_evidence...` | 错误文案从 "outside its source row" 变成 "outside its exact source row" | 更新正则 |
| `test_embedding_enqueue_receipts` | 返回的 `result` 新增 `scanned` / `invalid_candidate_count` | 更新期望字典 |
| `test_embedding_candidate_migration[True/False]` | 在**已迁移到最新**的库上把 `user_version` 改回 80，再 `initialize()`，于是 82 之后的迁移全部重跑；对话修订迁移的 DDL 正则匹配不到旧唯一约束而报错 | 改为只跑到 v80，再显式应用 v81 两次；不再重跑后续迁移 |
| `test_knowledge_graph_parse_cleanup::...` | 夹具还在写 `category` 之前的候选内容（`entity_category` / `description`），被当前 `KnowledgeGraphCandidateContent` 拒绝 | 按当前字段重写夹具，并把另一条用例的 `description` 断言换成 `value` |
| `test_graph_parser_identity_recovery`（2 条） | 夹具的候选缺少 `semantic_review`，`_identity_input_is_eligible` 直接判否，recovery 返回 `None` | 补一条 `assessment="supported"` 的语义复核记录 |
| `test_parse_replacement::...` | 受保护事实是 rejected/draft、没有发布指针，索引器不写 `knowledge_graph_relation_reference`；另外测试注入的 purge 还是旧签名 | 夹具显式写关系索引行；purge 包一层带 `owner_kind` / `space` / `work_id` 的适配器 |
| `test_retrieval_search_scope` + `test_association_entity_source_scope`（同一用例两份） | 断言期望的是「原始选择器元组」，而 `_search_scopes` 已改成把 source/folder 解析成精确 source 列表（OR 语义仍然成立） | 改为断言解析后的单 scope：`work_id=None`、`folder_ids=None`、source 集合为两者并集 |
| `test_query_equivalent_operations[writing]` + `test_query_register_observations`（2 条） | 测试构造的「旧 key」漏了基线 settings（`plan_output_contract: 2` / `raw_scene_context_contract: 1`），于是所有 scope 都被判成「契约变化」 | 在旧 key 里补上基线 settings，再比较手动观察范围带来的增量 |
| `test_query_grounded_scene_read::test_long_discovery_evidence...` | 断言要求把 6000 字的已发现 span 截到 2400；但 `scene_context_span` 的契约是「窗口只限制新增上下文，不截断已发现 span」，同文件 `test_scene_read_preserves_a_discovered_span_larger_than_its_default_window` 也明确断言这一点 | 改为断言保留完整 span，并改名去掉「obeys budget」的误导 |
| `test_query_material_sharing::test_shared_text_does_not_allow_foreign_candidate_decisions` | 断言期望外来候选决策让整条 requirement 变 `unresolved`；但当前契约是「拒绝这个可选提示、保留独立引用的答案」，`validate_candidate_choices` 的 docstring 明确写了这一点 | 改为断言决策被拒（`candidate_choices_not_validated`）、没有记录被采用，而共享证据仍支持答案 |
| `test_planning_study_reading::test_saved_legacy_request_reuses_owner_and_current_key_takes_precedence[False]` | 测试用 `read_text` 读原始提示词构造 system message，而服务用 `load_prompt` 展开 `{{decision_method}}`，两者 prompt hash 不同，legacy 幂等 key 自然对不上 | 测试改用 `load_prompt`，与服务的 system message 完全一致 |

最后一条不是产品缺陷：迁移本来就不是可重复执行的，测试人为回退版本号才触发了重放。

## 已删除（2）：旧库升级用例

| 用例 | 为什么删除 |
|---|---|
| `test_dialogue_partial_resume::test_revision_migration_preserves_legacy_ids_quotes_fts_and_proofs` | 模拟 v81 库升级，但当前 `add_text_source` 需要 `source.edit_revision`（v97 才加）；本项目不支持旧库升级 |
| `test_published_graph_version_pointer::test_v82_to_83_restores_exact_checkpoint_version_before_newer_draft` | 同上，模拟 v82 库升级 |

删除后这两个文件里的其余用例（当前行为测试）全部保留并通过。

## 待修（0）

所有夹具/断言漂移都已修完；剩下 8 项都是真实行为差异。

## 真实行为差异（0）

22 项全部处理完：20 项是夹具/断言漂移（已修），2 项是旧库升级用例（已删除）。没有需要产品判断的遗留项。

> 复盘：这批失败里没有发现产品逻辑回归。共同原因是抽取快照里的测试没有跟上实现演进——候选内容 schema、幂等 key 的基线 settings、`_search_scopes` 的解析结果、场景窗口的保留策略、可选候选决策的失败语义，以及提示词的 `{{decision_method}}` 展开。

## 复跑

```powershell
$env:PYTHONPATH='D:\Projects\fxi-knowledge\src'
python -m pytest -q
```
