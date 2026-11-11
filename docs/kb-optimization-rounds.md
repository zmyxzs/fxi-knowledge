# 知识库优化循环（4 轮）

> 约束：不做大改；不要求每轮提交 GitHub；每轮必须留下可验证的证据。

## 第 1 轮：服务层、性能、as-of

### 1.1 扫描范围

- `scripts/kb_server.py`：路由、鉴权、请求体、工作解析、来源归属、解析任务
- `archive.py`：实体解析、快照、故事记录、编译、回滚、删除
- SQLite 索引与查询路径
- 现有 189 个测试文件中与 archive/server 相关的部分

### 1.2 本轮修复

| 问题 | 修复 | 验证 |
|---|---|---|
| `kb_story_record` 只有主键索引，编译按 work/category/chapter 查询会全表扫 | 新增 `ix_kb_story_record_work_chapter`、`ix_kb_story_record_scope` | `test_story_record_scope_indexes_exist` |
| `POST /api/parse` 的 `source_ids` 可传其他作品/不存在的 id | 解析前校验归属，未知 id 直接 400 | `test_parse_rejects_unknown_and_foreign_sources` |
| `source_ids` 可重复，重复解析同一章 | 去重后保留顺序 | 同上用例覆盖非法路径；正常路径由去重保证 |

测试：`36 passed`（`test_archive.py` + `test_kb_server_scope.py`）。

### 1.3 本轮确认但未修的问题

#### P1：as-of 不成立

探针：

```text
赵洵在第 5 章死亡
编译第 1 章：
  list_entities() 直接排除赵洵
  related_entities() 仍返回赵洵，位置=“战场”，状态=dead
```

原因：

- `list_entities()` 取 `MAX(chapter_index)` 的最新快照，不接收 `at_chapter`
- `related_entities()` 调 `get_snapshot()` 不传 `at_chapter`
- `compile_archive()` 模块四/五因此可能缺人或多出未来状态

影响：回溯编译、改稿、前传都会读到未来信息。

#### P1：编译可超出最新章

```text
compile_archive(work, chapter=99) → chapter_index=99，无报错
```

没有校验 `chapter_index <= max(source.chapter)`，也无法显式选择“预编译未来章”。

#### P1：来源更新/删除的事务顺序

- `update_source()`：先改知识库，再回滚 archive；中途失败会留下半更新
- `delete_source()`：先删知识库，再回滚 archive；archive 失败会留下孤儿
- `delete_work()`：`delete_project` 与 `purge_work` 分两个事务

#### P2：别名解析全表扫

`resolve_entity()` 用 `json_each(aliases_json)` 扫描该作品全部实体；实体多时每次 dossier/compile 都变慢。

#### P2：`resolve_work(None)` 取第一个作品

多作品时默认行为不确定；同一标题重复时也只取第一条。

#### P2：HTTP 并发无上限

`ThreadingHTTPServer` 每个请求一个线程；`audit llm=true` 可能占用线程 120 秒，2 vCPU 机器容易被拖垮。当前只有 4 MB 请求体限制，没有连接数/速率限制。

#### P2：任务文件全量重写

`parse_jobs.json` 每次 `_update_job()` 都重写最近 100 条；高频进度更新会放大 I/O（D29/D64）。

### 1.4 下一轮（第 2 轮）

优先修 as-of：

1. `list_entities(..., at_chapter=...)`
2. `related_entities(..., at_chapter=...)`
3. `compile_archive(..., allow_future=False)`，默认拒绝超过最新章
4. 补 as-of 回归测试：第 1 章不能看到第 5 章死亡/位置

然后处理：

- `update_source/delete_source/delete_work` 的事务顺序
- HTTP 连接数上限与请求超时
- 别名索引或别名表

### 1.5 后续轮次

- 第 3 轮：配置默认值收敛、`schema_version`、`work_type` 行为化
- 第 4 轮：预审回归（硬规则/红线）、双账本、关系/因果归位

---

## 第 2 轮：as-of 与编译边界

### 2.1 修复

| 问题 | 修复 |
|---|---|
| `list_entities()` 永远取最新快照 | 增加 `at_chapter`，SQL 选 `<= chapter` 的最近快照 |
| `related_entities()` 取最新关系/位置 | 增加 `at_chapter`，并过滤该章已 dead/destroyed 的目标 |
| `compile_archive()` 不传章节 | 模块四/五、1 度关系都按 `chapter_index` 取 as-of |
| 预审死亡检查看最新状态 | `audit_draft()` 的实体列表传 `at_chapter` |
| `compile_archive(99)` 无边界 | 默认最多允许“最新章节的下一章”；更远需 `allow_future=true` |

保留正常写作流程：最新章是 1 时，编译第 2 章仍然允许。

### 2.2 验证

```text
第 1 章编译：
  赵洵出现，位置=山门，不出现“战场”
第 5 章编译（allow_future=true）：
  赵洵已 dead，不出现
compile_archive(chapter=99)：
  默认 ValueError
  allow_future=true 时返回 99
```

测试：`test_archive.py` **37 passed**。

### 2.3 本轮新发现

- 实体在目标章之前没有任何快照时，`list_entities(at_chapter)` 会返回 `lifecycle_status=None`，仍被当成 active；需要定义“未登场”语义。
- `compile_archive` 的“最新章”只来自 `kb_source_meta`；如果只有快照没有 meta，仍可编译任意未来章。
- `server.search` 的 inactive 过滤仍用最新状态；搜索无章节参数，暂时可接受，但和 as-of 编译口径不同。
- 别名解析仍是 `json_each` 全表扫。
- 来源更新/删除仍是“先改知识库、后回滚 archive”。

### 2.4 第 3 轮

优先做：

1. `update_source/delete_source/delete_work` 的事务补偿或顺序调整
2. 别名表或别名索引，去掉全表扫
3. HTTP 连接数上限、请求超时、`audit llm` 占线程保护
4. 画像默认值收敛为单一 schema（为第 4 轮铺路）

---

## 第 3 轮：默认值、并发上限、事务顺序复核

### 3.1 修复

| 问题 | 修复 |
|---|---|
| 默认值在 archive / extraction / schema 多处重复 | 新增 `profile_defaults.py`，`archive.py` 与 `archive_extraction.py` 统一引用 |
| 无并发上限，长 LLM 请求可占满线程 | `BoundedThreadingHTTPServer` + 请求槽；超过 `FXI_KB_MAX_CONCURRENCY` 返回 503 |
| 503 没有可读响应 | Handler 层 `_json({"error":"server busy"}, 503)`，不再裸 socket 写 |

### 3.2 验证

```text
FXI_KB_MAX_CONCURRENCY=1
第 1 个请求挂起（POST 未发完 body）
第 2 个 GET /health：
  HTTP/1.0 503 Service Unavailable
  {"error": "server busy"}
释放第 1 个请求后：
  /health → 200
```

测试：`test_archive.py` + `test_kb_server_scope.py` **39 passed**。

### 3.3 事务顺序复核

第 1 轮把“知识库先改、archive 后回滚”列为问题；本轮复核后结论相反：

- `update_source`：必须先改知识库；如果更新失败，不能先删 archive
- `delete_source`：必须先删知识库；如果删除失败，不能先回滚 archive
- `delete_work`：必须先删作品；如果作品删除失败，不能先 purge archive

archive 是派生数据，**主数据成功后再清理派生数据**是正确顺序。残留问题是“主数据成功、派生清理失败”时的孤儿数据，应靠后台清理/重试，而不是调换顺序。

### 3.4 新发现

- `_SCHEMA` 里的 DDL 默认值仍是字面量，只收敛了 Python 默认值；下一步可生成 DDL。
- 超过并发的 POST 如果 body 未读完，503 仍可能被 RST；GET 路径已验证正常。
- `FXI_KB_MAX_CONCURRENCY` 尚未写入部署文档。
- 别名解析仍是 `json_each` 全表扫。
- 无 `schema_version`，6 次 ALTER 无迁移记录。

### 3.5 第 4 轮

1. 修预审回归：硬规则/红线、`change.entity` 作用域、`allow_debt` 验证
2. 统一资源真源，消掉 `resources` / `inventory_ledger` 双写
3. 别名表或别名索引
4. `schema_version` 与迁移记录

---

## 第 4 轮：预审回归、作用域、欠款验证

### 4.1 修复

| 问题 | 修复 |
|---|---|
| 审计固定用主角账本 | 每个 `change.entity` 单独取快照、余额、库存；主角只用于覆盖检查 |
| `allow_debt=true` 直接豁免超支 | 只有同批抽到该实体的 `kind=debt` 才豁免；否则仍报 `LEDGER_ERROR` |
| 预审丢失硬规则/红线 | 存在 `hard_rules` 或 `redline` 时额外跑语义检查，合并 `HARD_RULE_CONFLICT` / `REDLINE_BREACH` |
| `resources` 与 `inventory_ledger` 冲突 | 读取时 `resources` 优先；`inventory_ledger` 只做兼容 |
| 无回归测试 | 增加实体作用域、欠款验证、硬规则语义检查用例 |

### 4.2 验证

- `test_archive.py` + `test_kb_server_scope.py` **41 passed**
- 全量测试 **1846 passed / 7 skipped**

### 4.3 四轮总结

| 轮次 | 主题 | 主要成果 |
|---|---|---|
| 1 | 服务层/性能 | 故事记录索引、`source_ids` 归属校验 |
| 2 | as-of | 实体/关系/编译按章节取快照；未来章边界 |
| 3 | 默认值/并发 | `profile_defaults.py`、`FXI_KB_MAX_CONCURRENCY` + 503 |
| 4 | 预审 | 实体作用域、欠款验证、硬规则/红线恢复、资源优先级 |

### 4.4 仍未解决

- `resources` / `inventory_ledger` 仍是双写，只统一了读取优先级
- archive 与 graph 仍是两套真源
- 别名解析仍全表扫
- `schema_version` / 迁移记录仍缺
- OpenAPI、README、客户端仍与实现漂移

### 4.5 结论

四轮修的是“现有链路的正确性”，不是“新功能”。  
下一步如果继续，应做架构归位（archive 变 projection、关系/因果归入既有 claim/event），而不是继续加 `kb_*` 字段。
