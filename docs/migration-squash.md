# 迁移链 squash 记录（2026-10-25）

已执行：42 个迁移模块合并为一份 baseline schema。

## 结果

| 指标 | 之前 | 之后 |
|---|---:|---:|
| 迁移模块 | 42 | 1（`migrations_baseline.py`） |
| 迁移代码 | 6780 行 | 约 50 行 + 260 KB SQL |
| `MIGRATIONS` 条目 | 105 | 1（version 105） |
| 测试文件 / 用例 | 185 / 1801 | 180 / 1787 |
| 全量测试耗时 | 731s | 416s |
| wheel 文件数 | 447 | 407 |
| storage 模块 | 55 | 13 |

## 做法

1. `scripts/dump_baseline_schema.py` 从空库导出 564 个 schema 对象（127 表 / 158 索引 / 279 触发器）和 6 张单例 seed 表，写入 `src/fxi_novel/storage/schema_baseline.sql`。
2. 新增 `migrations_baseline.py`：`MIGRATIONS_BASELINE = (Migration(version=105, sql=...),)`，SQL 在导入时从同目录文件读取。
3. `migrations.py` 只保留 `MIGRATIONS = (*MIGRATIONS_BASELINE,)` 和原有的 `apply_migration` 机制。
4. `Database.initialize()` 增加旧库保护：`0 < user_version < 105` 直接抛 `RuntimeError`（"old databases are not supported"），不再尝试重放。
5. 删除 42 个 `migrations_*.py`。
6. 测试处理：
   - 删除 6 个纯迁移测试文件：`test_embedding_candidate_migration`、`test_migration_graph_reference_scope`、`test_migration_parse_concurrency`、`test_migration_source_split`、`test_migration_work_base_scope`、`test_conversation_summary_index`；
   - 删除 3 个迁移专用函数：`test_assistant_run_storage::test_v41_...`、`test_knowledge_graph_parse_cleanup` 的 legacy 迁移类、`test_query_reviewed_claim_publication::test_schema_91_...`；
   - 重写 `test_parse_processing_units` 的 seed：直接重放 baseline SQL；
   - 移除 `test_query_reviewed_claim_publication._fixture` 的旧库分支。
7. 新增 `tests/test_schema_baseline.py`：baseline 重放与 `initialize()` 的 schema 必须一致、链里只有一条迁移、旧库必须被拒绝。

## 验证

- baseline 重放 vs `initialize()`：归一化 schema 差异 0，`foreign_key_check` 为空，seed 表各 1 行。
- 全量测试：**1780 passed / 7 skipped / 0 failed**（416s）。
- wheel 包含 `fxi_novel/storage/schema_baseline.sql`；解包安装后建库得到 `user_version=105`、128 张表。

## 后续修改 schema

```powershell
$env:PYTHONPATH='src'
python scripts/dump_baseline_schema.py     # 重新生成 src/fxi_novel/storage/schema_baseline.sql
# 然后把 migrations_baseline.BASELINE_VERSION 提升到新版本
```

`test_schema_baseline.py` 会保证 SQL 文件与 `initialize()` 的结果一致，避免忘记重新生成。

## 代价

- 本仓库的 `storage/` 不再能直接回迁到仍使用迁移链的 `Fxi-novel`；回迁需要只取 `schema_baseline.sql` 之外的知识库模块。
- 旧库无法升级，这是明确的取舍（见 README 已知限制第 1 条）。
