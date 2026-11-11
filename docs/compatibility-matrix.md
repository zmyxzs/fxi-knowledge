# 兼容性矩阵

状态：2026-11-11  
目标：明确哪些东西必须兼容、哪些可以破坏、破坏时怎么迁移。

关联：[架构契约](architecture-contract.md) / [历史问题](history-problems-solutions.md)

---

## 1. 兼容维度

| 维度 | 当前支持 | 破坏风险 | 策略 |
|---|---|---|---|
| Python | 3.11 / 3.12 | AI Studio 可能不是 3.11 | 声明 `requires-python`；外部运行先验版本 |
| 运行平台 | Linux/Windows CLI、HTTP 服务 | AI Studio Build 是 Node 前端 | 不把 Python 服务塞进 AI Studio |
| 数据库 | SQLite，`ensure_schema` 加列 | 老库缺列 | 只加列、迁移前备份、启动时补列 |
| 读取源 | `kb_story_record` | graph-only 老作品查不到 | 先回填，再 archive-only |
| 工件 | `kb_document` v1 | 旧工件缺 `warnings_json` | 启动迁移补列 |
| Prompt | v1–v4 | 字段名/枚举漂移 | 别名归一 + prompt_version |
| 模型 | DeepSeek/OpenAI-compatible | 输出格式变化 | 最小 schema + 归一 + 重试 |
| API 客户端 | `examples/kb_client.py` | 新接口缺失 | 契约测试 |
| 多作品 | 必须带 `work` | 老客户端 400 | 版本化/弃用期 |
| 分类 | 核心 + 模块分类 | 过滤语义不一致 | 核心五种 + `module` |
| 证据 | 逐字/归一化/warning | 模型改写 | 运行时 warning，评估严格 |
| 来源归属 | `source_ids` 校验 work | 跨作品需求 | 显式开关 |
| 分页 | offset/next_offset | 写入期间翻页不稳定 | 大数据改 cursor |
| 部署 | 手工 scp + systemd | 生产落后 | 版本号 + smoke + 备份 |
| 测试数据 | 合成 fixture 入库，真实章节本地 | CI 覆盖不足 | 合成必跑，真实抽样 |

---

## 2. 必须保持兼容

### 2.1 数据库

- 只加列，不删列
- 新列必须有默认值
- 启动时 `ensure_schema` 自动补列
- 迁移前备份
- 迁移后校验计数

已加列：

```text
projection_status
projection_error
warnings_json
```

### 2.2 工件

- `kb_document` 的 `content_json` 保留未知字段
- 旧工件缺 `warnings` 时按空数组处理
- `schema_version` 保留，用于后续升级

### 2.3 Prompt 输出

允许别名：

```text
场景/scene_list/events → scenes
认知/cognitions → cognition
状态变化/state_changes → state_deltas
未决/threads → unresolved
判断 → judgment
道具 → item
新 → new
```

别名归一后仍不合法，只 warning，不 hard fail。

### 2.4 API

- 单作品库允许省略 `work`
- 多作品库必须带 `work`
- 新字段只增不减
- `source` 字段标识 archive
- 分页返回 `total/offset/next_offset`

---

## 3. 可以破坏，但要迁移

### 3.1 graph → archive

已执行：

```text
神域囚徒 735 条 graph → fact
```

迁移脚本：

```text
scripts/backfill_graph_story.py
```

规则：

1. 先 dry-run
2. 再写入
3. 校验 total
4. 切换 `/api/story` archive-only

### 3.2 旧 memory 章节记录

已执行：

```text
斗罗移除 10 条旧 memory 章节记录
```

规则：

- 只有存在 active `chapter_memory` 的章节才清理
- 只清 `scene/cognition/thread/state_delta`
- `fact` 保留

### 3.3 多作品默认

旧行为：

```text
不传 work → 第一个作品
```

新行为：

```text
多作品不传 work → 400
```

兼容建议：

- 客户端升级
- 或增加 `default_work` 配置，但只允许单作品库

---

## 4. 不能兼容的情况

| 情况 | 原因 | 处理 |
|---|---|---|
| AI Studio Build 直接跑 Python 服务 | 运行时是 Node | 用前端 + 远程 API，或 Cloud Run |
| Python 3.10 跑本项目 | `requires-python >=3.11` | 升级运行时 |
| graph 与 archive 同时作为真源 | 会分叉 | 只保留 archive |
| 一个 prompt 同时做多类工件 | 注意力不够 | 拆调用 |
| 无 `scenes` 的章节记忆 | 无法投影 | hard fail 进 draft |

---

## 5. AI Studio 兼容性

### 能做的

- 导入 GitHub 仓库
- 读取文档、代码、prompt
- 做架构评审、找问题、生成测试建议

### 不能做的

- 直接运行 Python 包
- 启动 `scripts/kb_server.py`
- 安装 `pyproject.toml` 依赖
- 访问 SQLite 数据库
- 执行 pytest

### 结论

AI Studio 适合做“评审者”，不适合做“运行环境”。

要运行：

- Python 服务继续放 `kg.zmteams.site` / Cloud Run
- AI Studio 只做前端或评审

---

## 6. 兼容性测试

| 测试 | 覆盖 |
|---|---|
| 老库启动补列 | `ensure_schema` |
| 旧工件缺 warnings | `_document_row` |
| 别名输出 | `normalize_chapter_memory_output` |
| 单作品不带 work | `resolve_work` |
| 多作品不带 work | 400 |
| graph 回填 | `backfill_graph_story --dry-run` |
| 去重 | `dedupe_story_records` |
| 分页 | `offset/next_offset` |
| 分类过滤 | `graph_category` |
| 部署 smoke | `/health`、`/api/story`、`/api/context` |

---

## 7. 版本策略

建议增加：

```text
GET /health
{
  "status": "ok",
  "version": "0.1.0",
  "schema_version": 1,
  "works": [...]
}
```

部署流程：

```text
备份 DB
→ 上传代码
→ py_compile
→ 启动
→ /health 版本校验
→ /api/story smoke
→ /api/context smoke
```

---

## 8. 原则

1. 数据只加不删。
2. 读取先回填再切换。
3. 输出先归一再校验。
4. 运行时只对“无法投影”hard fail。
5. 兼容性破坏必须有迁移脚本和回滚。
6. AI Studio 只做评审，不做 Python 运行时。
