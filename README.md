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
| `src/fxi_novel/models/` | 数据模型与模型网关（`ChatModel` / `ModelGateway`） |
| `src/fxi_novel/learning/` | 学习链路（研究与知识库共用 owner） |
| `src/fxi_novel/writing/` | 只保留 3 个被知识库反向引用的模块：`source_scope`、`chapter_author_sources`、`chain_versions` |
| `src/fxi_novel/prompt_loading.py` | 提示词装载（学习链路要用） |
| `src/fxi_novel/project_paths.py` | 定位 `prompts/` 与 `config/`：源码树、editable、wheel（`fxi_novel/_data`）三种布局 |
| `prompts/` | 提示词文件（学习链路按路径读取） |
| `config/writing_chains/` | `chain_versions` 读取的链版本配置 |
| `examples/` | `smoke.py`（内存库冒烟）、`writing_order_reader.py`（独立运行写作空间解析用的 narrative-order reader） |
| `docs/` | 验证记录、发布语义、xfail triage |
| `tests/` | 只依赖上述包的 185 个测试文件（1801 个用例） |

**没有** `assistant/`、`api/`、`workflows/`、`ui/`：这些属于写作系统，不在知识库范围内。

## 当前基线

在 `D:\Projects\Fxi-novel\.venv` 解释器下（`PYTHONPATH` 指向本项目 `src`）：

```
12 xfailed, 1782 passed, 7 skipped in 695s
```

这 12 项在**原项目里用同一批用例跑也全部失败**（已逐条比对），说明抽取本身没有改变行为，失败是抽取前就存在的既有问题。它们登记在 `tests/conftest.py`，以 `xfail(strict=True)` 标记：

- 修好一项会变成 `XPASS`，提醒你从登记表里删掉对应条目；
- 新增回归仍然是普通 `FAILED`，不会被这层标记吞掉。

最初登记时是 22 项（`22 failed, 1766 passed, 7 skipped in 719s`）。其中 8 项属于夹具或断言漂移，已修复；另外 2 项只测 v81/v82 旧库升级，已删除（本项目不支持旧库升级）。逐条分析见 [`docs/xfail-triage.md`](docs/xfail-triage.md)。

## 为什么 import 路径还是 `fxi_novel`

抽取时**没有改写任何 import**，包名保持 `fxi_novel`，这样：

- 代码可以和原项目逐文件 diff；
- 在这里改好的模块，可以整体拷回原项目而不必再改 import；
- 代价是两边不能装进同一个 venv（装进同一个会互相覆盖）。

## 相对原项目的五处适配

1. `src/fxi_novel/writing/__init__.py` 被改写成最小版本——原文件会导入 `chapter_check`，那个模块属于写作系统，没有抽过来。
2. 删除了 39 个测试文件：34 个通过辅助模块间接依赖 `fxi_novel.api` / `fxi_novel.assistant` / `scripts`；另外 5 个是 HTTP 或验收脚本（`test_source_edit_revision`、`test_source_record_history`、`test_source_save_receipts`、`test_d1_query_baseline_run`、`test_style_validation_batches`），按 `scripts/`、`data/validation/` 的绝对路径读文件，属于工具链而不是知识库单元测试。
3. `prompts/` 与 `config/writing_chains/` 整体复制，因为 `learning` 的服务会按路径读取提示词，`chain_versions.selected_prompt` 也会读链配置。
4. 删除了 6 个不再被引用的测试辅助文件（`assistant_turn_api_support`、`chapter_planning_archived_fixture`、`generated_quality_review_fixture`、`knowledge_graph_parse_support`、`parse_queue_support`、`sentence_structure_revision_support`）：它们 import 了未抽取的 `api` / `assistant` / `writing` 模块，而现有测试文件对它们零引用。
5. 新增 `src/fxi_novel/project_paths.py`，并让 `chain_versions.py` / `prompt_loading.py` 用它定位 `prompts/` 与 `config/`；`pyproject.toml` 用 `force-include` 把这两个目录作为包数据打进 wheel（`fxi_novel/_data/`）。这是为「非 editable 安装也能用」做的唯一结构性改动，回迁主项目时注意它和原项目的路径假设不同。

## 怎么跑

```powershell
cd D:\Projects\fxi-knowledge
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q
```

冒烟检查（不需要安装、不需要模型、不写磁盘）：

```powershell
python examples\smoke.py
```

装到别的环境（提示词与链配置会一起装进去）：

```powershell
python -m pip install .
```

wheel 把 `prompts/` 和 `config/writing_chains/` 作为包数据放在 `fxi_novel/_data/`，运行时由 `project_paths.project_root()` 定位，不需要把仓库目录一起带上。

依赖只有 `pydantic`、`numpy`、`jsonschema`、`jieba`、`httpx`；写作系统那套 FastAPI/LangGraph 都不需要。

如果暂时不想建 venv，也可以用原项目的解释器加 `PYTHONPATH` 验证（`PYTHONPATH` 优先于原项目的 editable 安装）：

```powershell
$env:PYTHONPATH='D:\Projects\fxi-knowledge\src'
D:\Projects\Fxi-novel\.venv\Scripts\python.exe -m pytest -q
```

## 持续集成

`.github/workflows/tests.yml` 在 push / PR 时执行：

- `ruff check . --select E9,F63,F7,F82`：只挡语法错误和未定义名；存量 `F401` / `F403` / `F405` 暂不作为门禁
- `pytest -q`：全量 1803 个用例

## 程序 API

知识库对外只有一条主链路：`Database` → `KnowledgeRepository` → `KnowledgeService`。
`KnowledgeService` **不调用模型**，可以完全离线使用；需要模型的是 `LearningService` 和 `ModelGateway`。

```python
import asyncio

from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.knowledge.service import KnowledgeService
from fxi_novel.storage.database import Database

database = Database(":memory:")  # 换成 "data/fxi_novel.sqlite3" 就是持久库
database.initialize()            # 显式建表并跑迁移，失败会直接抛异常

knowledge = KnowledgeService(
    KnowledgeRepository(database, embedding_indexing_enabled=False)
)

work = knowledge.create_project(space="writing", title="示例作品")
source = knowledge.add_text_source(
    title="示例来源",
    content="第一段：主角走进铺子，先问价。\n\n第二段：掌柜报了价，他改了主意。",
    space="writing",
    work_id=work["id"],
)

hits, notes, coverage = asyncio.run(
    knowledge.search_planned(
        ["掌柜 报价"],
        "writing",
        work["id"],
        branch="main",
        top_k=3,
        source_ids=[source.source_id],
        coverage_mode="focused",
        coverage_source_limit=1,
        include_linked_scopes=False,
    )
)
for hit in hits:
    print(hit.score, hit.text)

database.close()
```

几个容易踩的点：

- `Database.initialize()` 不会自动执行；不调用就直接查询会得到空库或 `no such table`。
- `space` 只接受 `"writing"` 和 `"learning"`；`writing` 必须带 `work_id`，跨 space 的读写会被仓储拒绝。
- `embedding_indexing_enabled=False` 时只建 FTS 投影，检索走词法；要走向量就把参数设为 `True`，并把一个 `embedding_projection` 传给 `KnowledgeService`。
- `search_planned` 是 `async` 方法，返回 `(命中列表, 诊断信息, 覆盖度)`；`coverage_mode` 目前支持 `"focused"` 和 `"each_source_ending"`。
- `writing` 空间检索要传 `branch`（如 `"main"`）：不传时 `knowledge_cards` 会被分支过滤掉，只剩原文命中。
- 判断是否发布看 `knowledge_item.published_graph_version_id` 指针，不看 `status`（解析后仍是 `draft` 属正常）；详见 [`docs/publication-semantics.md`](docs/publication-semantics.md)。
- 写入只有一个 owner：`knowledge` / `storage` 里的仓储；FTS 与向量都是可重建投影，`rebuild_search_index()` 可以从权威文本重建 FTS。
- wheel 安装时提示词目录用 `project_root() / "prompts"`（`from fxi_novel.project_paths import project_root`）；源码/editable 安装会自动解析到仓库根。

### 写作空间解析

`writing` 空间的 parse 需要一个 narrative-order reader（完整产品里由写作系统提供）。独立使用时显式接上示例实现：

```python
from examples.writing_order_reader import SourceOrderManifest

knowledge.set_writing_narrative_order_reader(
    SourceOrderManifest(work_id=work["id"], branch_id="main")
)
```

它按来源导入顺序编号并生成 verified manifest；`examples/` 不在安装包里，实际工程里把类拷进去、或换成自己的 reader 即可。

## 接模型（可选）

`LearningService` 需要一个 `ChatModel`。仓库自带 `ModelGateway`，按 OpenAI 兼容协议请求
`{base_url}/chat/completions` 和 `{base_url}/embeddings`，配置从 TOML 读取：

```toml
# gateway.toml
[chat]
enabled = true
base_url = "https://api.deepseek.com/v1"
model = "deepseek-chat"
token_env = "DEEPSEEK_API_KEY"
timeout_seconds = 90

[embedding]
enabled = false
base_url = "https://api.openai.com/v1"
model = "text-embedding-3-small"
token_env = "OPENAI_API_KEY"
dimensions = 1536
```

```python
from pathlib import Path

from fxi_novel.learning.service import LearningService
from fxi_novel.models.gateway import GatewayConfig, ModelGateway
from fxi_novel.project_paths import project_root

config = GatewayConfig.from_toml(Path("gateway.toml"))
gateway = ModelGateway(config)
learning = LearningService(knowledge, gateway, project_root() / "prompts" / "learning")
```

- `base_url` 只写到版本前缀（如 `/v1`），不要自己拼 `/chat/completions`。
- token 从 `token_env` 指向的环境变量读取；也可以直接写 `api_key`，但不要提交。
- 可选路由：`[[chat_fallbacks]]`（数组，按顺序降级）、`[knowledge_query]`、`[creative]`。
- `embedding.enabled = false` 时向量调用会抛 `ModelUnavailable`，词法检索不受影响。
- 提示词目录用 `project_root() / "prompts" / "learning"`；`load_prompt()` 会展开 `{{decision_method}}`，并按链版本快照取覆盖文本。
- DeepSeek 路由要把 `reasoning_effort` 设为 `"none"`（网关会发 `thinking: {"type": "disabled"}`）或显著提高 `max_output_tokens`：推理会先吃掉输出预算，预算耗尽时 `finish_reason=length`、正文为空，抽取会以 `review_incomplete` 失败。

## 环境变量

| 变量 | 用途 | 默认值 |
|---|---|---|
| `FXI_NOVEL_DB_PATH` | `Database()` 不传路径时使用的库文件 | `data/fxi_novel.sqlite3` |
| `FXI_NOVEL_ROOT` | 数据根：包含 `prompts/` 与 `config/` 时优先使用；维护锁/恢复日志也用它 | wheel 用包内 `_data`，源码用仓库根 |
| `<token_env>` | 网关鉴权 token，变量名由路由的 `token_env` 指定 | 空（不鉴权） |

## 已知限制

1. **12 个既有失败**：都继承自原项目，已在 `tests/conftest.py` 登记为 `xfail(strict=True)`；分类、根因和修复风险见 [`docs/xfail-triage.md`](docs/xfail-triage.md)。大致分三类：
   - 解析与发布：候选清理时机、替换依赖、身份恢复；
   - 查询与学习：源/文件夹选择器 OR 语义、缓存编译、材料共享、场景窗口预算、观察范围、规划复用；
   - 其余为夹具/断言漂移，可修。
2. **不支持旧库升级**：只支持从空库初始化；v81/v82 等旧库升级用例已删除（当前 `add_text_source` 依赖 v97 才加的 `source.edit_revision`）。
3. **7 个 skip**：需要 `data/` 下的审计库或验证快照，这些数据没有抽进来；skip 消息里写了缺什么。
4. **写作空间解析需要外部 narrative-order reader**：独立 checkout 没有 writer-owned manifest，`writing` 空间的 parse 会抛 `writing narrative-order reader is not configured`；需要自己实现并 `set_writing_narrative_order_reader`（`examples/writing_order_reader.py` 提供了一个按导入顺序编号的最小实现，可作起点）。
5. **事件整理阶段强制 DeepSeek**：`event_reconciliation_review_execution` 校验 `provider_model` 必须含 `deepseek`；用其它模型时 resolution 阶段会失败。
6. **DeepSeek 抽取要关闭推理或提高输出预算**：默认 16000 输出 token 会被 reasoning 全部占用（`finish_reason=length`、正文为空），表现为 `review_incomplete`；把 `reasoning_effort` 设为 `"none"` 后单章抽取可完成（见验证记录第 5 节）。
7. **没有 HTTP / CLI 入口**：这是知识库而不是写作系统；需要对外服务时在上层自行封装（原项目的 `api/` 不在本仓库）。

## 验证记录

2026-10-24 用《神域囚徒》313–315 章做了一次端到端验证：导入 3 章（2736 / 2602 / 3030 字）→ 解析（155 条知识、75 个实体候选、63 个身份组、3/3 来源发布完成）→ 查询（自然语言问题规划出 2 条 requirement，均 supported，21 条证据）。完整数据、耗时和问题清单见 [`docs/verification-2026-10-24.md`](docs/verification-2026-10-24.md)。

## 并行开发时的边界约定

- **不要**在这里新增对 `assistant` / `api` / `workflows` / 写作业务的依赖，否则就失去了独立对比的意义。
- 知识库的写入仍然只有一个 owner：`knowledge` 与 `storage` 里的仓储；FTS 与向量是可重建投影，不是权威。
- 任何跨作品召回、隐式范围扩展都不要在抽取版里"顺手放宽"——原项目靠这些边界保证作者数据不串。
- 改动尽量按模块提交，便于之后和主项目逐文件比较、择优回迁。

## 与主项目对比时的建议

1. 先跑这里的 `pytest -q`，记下通过率作为基线（主项目同一批测试的结果可以按同一份用例清单对照）。
2. 比较时固定数据：用同一个 `fxi_novel.sqlite3` 的**副本**，不要两边同时写主库。
3. 判断"哪边效果好"要落在可观察结果上：同一批查询/学习任务的产物、调用次数、失败恢复，而不是代码行数或测试数量。

## License

MIT，见 [LICENSE](LICENSE)。
