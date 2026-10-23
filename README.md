# fxi-knowledge

从 `D:\Projects\Fxi-novel` 抽出的**独立知识库项目**，用于和写作系统并行开发、对比效果。

- 抽取时间：2026-10-23
- 来源：`D:\Projects\Fxi-novel` 工作区（含未提交改动）在抽取时的状态
- 原则：**只读复制，不改动原项目**；原项目的任何文件都没有被修改或移动

## 这里有什么

| 目录 | 内容 |
|---|---|
| `src/fxi_novel/knowledge/` | 知识库主体：来源、解析、检索、方法、状态、资产、登记册、查询 |
| `src/fxi_novel/storage/` | SQLite 访问、迁移、运行与对话存储 |
| `src/fxi_novel/models/` | 数据模型 |
| `src/fxi_novel/learning/` | 学习链路（研究与知识库共用 owner） |
| `src/fxi_novel/writing/` | 只保留 3 个被知识库反向引用的模块：`source_scope`、`chapter_author_sources`、`chain_versions` |
| `src/fxi_novel/prompt_loading.py` | 提示词装载（学习链路要用） |
| `prompts/` | 提示词文件（学习链路按路径读取） |
| `config/writing_chains/` | `chain_versions` 读取的链版本配置 |
| `tests/` | 只依赖上述包的 183 个测试文件（1795 个用例） |

**没有** `assistant/`、`api/`、`workflows/`、`ui/`：这些属于写作系统，不在知识库范围内。

## 当前基线

在 `D:\Projects\Fxi-novel\.venv` 解释器下（`PYTHONPATH` 指向本项目 `src`）：

```
22 failed, 1766 passed, 7 skipped in 719s
```

这 22 项在**原项目里用同一批用例跑也全部失败**（已逐条比对），说明抽取本身没有改变行为，失败是抽取前就存在的既有问题。

## 为什么 import 路径还是 `fxi_novel`

抽取时**没有改写任何 import**，包名保持 `fxi_novel`，这样：

- 代码可以和原项目逐文件 diff；
- 在这里改好的模块，可以整体拷回原项目而不必再改 import；
- 代价是两边不能装进同一个 venv（装进同一个会互相覆盖）。

## 相对原项目的三处适配

1. `src/fxi_novel/writing/__init__.py` 被改写成最小版本——原文件会导入 `chapter_check`，那个模块属于写作系统，没有抽过来。
2. 删除了 39 个测试文件：34 个通过辅助模块间接依赖 `fxi_novel.api` / `fxi_novel.assistant` / `scripts`；另外 5 个是 HTTP 或验收脚本（`test_source_edit_revision`、`test_source_record_history`、`test_source_save_receipts`、`test_d1_query_baseline_run`、`test_style_validation_batches`），按 `scripts/`、`data/validation/` 的绝对路径读文件，属于工具链而不是知识库单元测试。
3. `prompts/` 与 `config/writing_chains/` 整体复制，因为 `learning` 的服务会按路径读取提示词，`chain_versions.selected_prompt` 也会读链配置。

## 怎么跑

```powershell
cd D:\Projects\fxi-knowledge
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q
```

依赖只有 `pydantic`、`numpy`、`jsonschema`、`jieba`、`httpx`；写作系统那套 FastAPI/LangGraph 都不需要。

如果暂时不想建 venv，也可以用原项目的解释器加 `PYTHONPATH` 验证（`PYTHONPATH` 优先于原项目的 editable 安装）：

```powershell
$env:PYTHONPATH='D:\Projects\fxi-knowledge\src'
D:\Projects\Fxi-novel\.venv\Scripts\python.exe -m pytest -q
```

## 并行开发时的边界约定

- **不要**在这里新增对 `assistant` / `api` / `workflows` / 写作业务的依赖，否则就失去了独立对比的意义。
- 知识库的写入仍然只有一个 owner：`knowledge` 与 `storage` 里的仓储；FTS 与向量是可重建投影，不是权威。
- 任何跨作品召回、隐式范围扩展都不要在抽取版里"顺手放宽"——原项目靠这些边界保证作者数据不串。
- 改动尽量按模块提交，便于之后和主项目逐文件比较、择优回迁。

## 与主项目对比时的建议

1. 先跑这里的 `pytest -q`，记下通过率作为基线（主项目同一批测试的结果可以按同一份用例清单对照）。
2. 比较时固定数据：用同一个 `fxi_novel.sqlite3` 的**副本**，不要两边同时写主库。
3. 判断"哪边效果好"要落在可观察结果上：同一批查询/学习任务的产物、调用次数、失败恢复，而不是代码行数或测试数量。
