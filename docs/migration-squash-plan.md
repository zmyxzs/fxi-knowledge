# 迁移链 squash 方案（未执行）

- 日期：2026-10-25
- 结论先说：**技术上可行，已生成并验证 baseline；但不建议在本仓库执行**，原因见最后一节。

## 当前状态

| 指标 | 数值 |
|---|---:|
| 迁移模块 | 42 个文件 |
| 迁移代码 | 6780 行 |
| 最新版本 | `user_version = 105` |
| 空库 schema 对象 | 564（127 表 + 158 索引 + 279 触发器） |
| 迁移 seed 的单例行 | 6 张表 |

seed 表（空库初始化后即有 1 行）：

- `embedding_read_cache_revision`
- `knowledge_graph_fts_state`
- `knowledge_card_index_state`
- `knowledge_source_title_search_state`
- `knowledge_card_candidate_evidence_state`
- `knowledge_card_folder_summary_state`

FTS 影子表（`*_fts_data` / `_idx` / `_content` / `_docsize` / `_config`）由 `CREATE VIRTUAL TABLE` 自动创建，不需要写入 baseline。

## 运行时对版本的依赖

全仓库只有一处按 `user_version` 分支：

- `repository_parse_queue.attach_parse_queue_states`：`user_version < 84` 时跳过 submission queue 状态。

因此 baseline 必须把版本保持在 **≥84**（建议沿用 105），不能压成 1，否则队列状态会被静默跳过。

## 生成与验证

```powershell
$env:PYTHONPATH='src'
python scripts/dump_baseline_schema.py            # 输出 build/migration-baseline-draft.sql
```

本次生成结果：

- 260,484 字符，823 条语句（564 个 schema 对象 + seed INSERT）
- 用 `executescript` 在空库重放后，与 `Database.initialize()` 的 schema 对比：
  - 对象数 564 vs 564，**归一化后差异 0**
  - `PRAGMA foreign_key_check` 为空
  - 6 张 seed 表各 1 行

差异只出现在尾部空白（`sqlite_master` 保留原始 SQL 的尾随空格），归一化后完全一致。

## 若执行，步骤与影响面

1. 保留 `Migration` / `apply_migration` 机制，把 `MIGRATIONS` 换成一个 `Migration(version=105, sql=<baseline>)`。
2. 删除 42 个 `migrations_*.py` 模块及其导出。
3. 增加一个 schema 快照测试：归一化 `sqlite_master` 的 `(type,name,sql)` 集合 + seed 行数 + `user_version`，替代逐版本迁移测试。
4. 处理 10 个依赖版本号的测试文件：

| 测试文件 | 依赖点 | 处理 |
|---|---|---|
| `test_assistant_run_storage.py` | 应用到 `>=41`，断言最新版本 | 删除旧库构造，改用 `initialize()` |
| `test_conversation_summary_index.py` | 全部应用 + 重放 summary 迁移 | 只保留行为断言 |
| `test_embedding_candidate_migration.py` | v77/78、v80→81 | 删除（迁移语义不再存在） |
| `test_knowledge_graph_parse_cleanup.py` | `<=13` / `<=14` | 删除旧库构造 |
| `test_migration_graph_reference_scope.py` | 直接 import 迁移模块 | 删除 |
| `test_migration_parse_concurrency.py` | v79 重建与失败回滚 | 删除 |
| `test_migration_source_split.py` | 模块组成/版本区间断言 | 删除 |
| `test_migration_work_base_scope.py` | 应用除最后一条外的全部 | 删除 |
| `test_parse_processing_units.py` | 单独应用一个迁移 | 改为 `initialize()` |
| `test_query_reviewed_claim_publication.py` | 伪造 `user_version=91` | 删除版本伪造，保留发布行为 |

5. 全量测试 + 用 `data/verify.sqlite3` 重新打开验证（读路径、检索、发布指针）。

## 风险

- **回迁能力**：本仓库的定位是「和 `Fxi-novel` 逐文件对比、择优回迁」。删除 42 个迁移模块会让回迁变成单向分叉，原项目仍然需要它们。
- **升级覆盖**：删掉旧库构造测试后，如果将来又要支持旧库，没有回归网。
- **seed 漂移**：baseline 里的 seed 行是快照，后续代码若新增单例表，必须同步更新脚本的 `_SINGLETON_TABLES`。
- **收益有限**：空库初始化目前只是毫秒级；squash 的主要收益是仓库体积和阅读清晰度，不是性能。

## 建议

- 如果这个仓库**只作为独立知识库长期维护**、不再回迁：可以执行，按上面 5 步走。
- 如果还要和原项目对比/回迁：**保留迁移链**，只把 `scripts/dump_baseline_schema.py` 作为工具留着，未来需要时再生成 baseline。
