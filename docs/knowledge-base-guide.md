# 知识库系统使用文档

适用版本：2026-11-10（提交 `c49fa3d` 之后）
线上地址：<https://kg.zmteams.site>

---

## 1. 系统概览

知识库把小说章节解析成可查询、可回滚、可编译的结构化档案，供写作流水线调用。

| 能力 | 说明 |
|---|---|
| 作品管理 | 连载库 `writing` / 拆书库 `deconstruct` |
| 章节双轨 | `content_raw`（解析用初稿）+ `content_polished`（导出终稿） |
| 实体档案 | A 路硬设定、B 路动态状态、C 路认知与关系 |
| 章节快照 | 按 `chapter_index` 版本化，删除/编辑自动回滚 |
| 检索 | 词法检索、卡片、拆书骨架检索 |
| 解析 | 异步任务，`state_update` / `deconstruct_extract` / `memory` / `chapter_memory` / `archive_update` / `storyline_network` 六种模式 |
| 审计 | 确定性检查 + 可选 LLM 语义审校 |
| 编译 | 一键生成九大模块 Markdown |

### 入口

| 地址 | 内容 |
|---|---|
| https://kg.zmteams.site/ | 知识库搜索页 |
| https://kg.zmteams.site/docs | API 速查 |
| https://kg.zmteams.site/swagger | Swagger UI |
| https://kg.zmteams.site/api/openapi.json | OpenAPI JSON |
| https://kg.zmteams.site/api/openapi.yaml | OpenAPI YAML |
| https://xs.zmteams.site/ | StoryForge 写作工作台（独立服务） |

### 数据流

```text
上传章节（raw + polished）
  → 分块 / FTS 索引
  → /api/parse 解析（抽取、复核、发布）
  → 档案抽取（实体、快照、账本、故事记录）
  → /api/entity、/api/search、/api/compile_archive、/api/audit
```

---

## 2. 快速开始

### 2.1 认证

- 读接口公开
- 写接口需要请求头 `X-API-Key`
- 服务端密钥在 `/home/ubuntu/fxi-bench/.env` 的 `FXI_KB_API_KEY`

```bash
export BASE=https://kg.zmteams.site
export KEY=你的密钥
```

### 2.2 三分钟跑通

```bash
# 1. 健康检查
curl -s $BASE/health

# 2. 创建作品
curl -s -X POST $BASE/api/works \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"title":"示例作品"}'

# 3. 作品画像
curl -s -X POST $BASE/api/work_profile \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"work":"示例作品","work_type":"writing","genre":"玄幻","protagonist_name":"杨凌"}'

# 4. 上传第 1 章（双轨）
curl -s -X POST $BASE/api/sources \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"work":"示例作品","title":"第1章 山门","chapter_index":1,
       "content":"杨凌有一千铜钱。",
       "content_raw":"杨凌有一千铜钱。",
       "content_polished":"杨凌有一千铜钱。"}'

# 5. 解析并写入档案
curl -s -X POST $BASE/api/parse \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"work":"示例作品","mode":"state_update"}'

# 6. 轮询任务
curl -s $BASE/api/parse/<job_id>

# 7. 查询实体
curl -s "$BASE/api/entity?name=杨凌&work=示例作品"

# 8. 编译下一章档案
curl -s -X POST $BASE/api/compile_archive \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"work":"示例作品","chapter_index":2,"outline":"杨凌进山"}'

# 9. 初稿预审
curl -s -X POST $BASE/api/audit \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"work":"示例作品","chapter_index":2,"text":"杨凌花了1200铜钱。","llm":true}'
```

### 2.3 Python 客户端

```python
from examples.kb_client import KnowledgeClient

client = KnowledgeClient(api_key="...")
client.create_work("示例作品")
client.upload_source("示例作品", "第1章", "正文……")
job = client.parse("示例作品")
print(client.job(job["id"])["progress"])
print(client.search("杨凌", work="示例作品"))
print(client.audit("示例作品", llm=True))
```

---

## 3. 核心概念

### 3.1 作品与双库模式

`work_type`：

- `writing`：连载创作库，严管状态、快照与回滚
- `deconstruct`：爆款拆书库，抽取去名化骨架与文风锚点

### 3.2 章节双轨

| 字段 | 用途 |
|---|---|
| `chapter_index` | 章节游标，快照与回滚的定位键 |
| `content` | 兼容字段，默认等于终稿 |
| `content_raw` | 初稿，供解析和下一章衔接 |
| `content_polished` | 终稿，供导出阅读 |
| `outline_raw` | 本章细纲，用于交叉核验 |

只传 `content` 时，两个轨自动写入相同内容。

### 3.3 实体档案 A/B/C

| 路 | 字段 | 说明 |
|---|---|---|
| A | `hard_rules` | 身份外貌、性格底色、底线弱点、口癖、功法机制；已有实体默认只读 |
| B | `dynamic_state` | 位置、伤势、境界、账本、连续出场/吃瘪计数 |
| C | `cognition_and_relations` | 动机、已知秘密、认知盲区、表面关系与真实态度 |

`hard_rules` 只有显式传 `overwrite_hard_rules: true` 才会改写。

### 3.4 章节快照与回滚

- 每个 `(work, 实体, chapter_index)` 一条快照
- 后一章未写的 `dynamic_state` / `cognition` 自动沿用上一章
- 删除第 N 章或编辑第 N 章，会自动删除 `chapter_index >= N` 的快照与故事记录
- 查询 `at_chapter=N` 返回第 N 章结算态

### 3.5 故事记录类别

| category | 内容 |
|---|---|
| `power_and_laws` | 力量体系、金手指限制、世界铁律 |
| `map` | 地图、区域规则 |
| `faction` | 派系与势力 |
| `active_storyline` | 当前故事线、最后 150 字定格 |
| `foreshadow` | 伏笔（`is_revealed=false` 会格式化为认知盲区锁） |
| `completed_storylines` | 已完结故事线 |
| `trope_skeleton` | 拆书骨架（6–8 步） |
| `style_anchor` | 去名化文风锚点 |
| `timer` | 倒计时（每章递减） |
| `redline` | 写作红线 |

### 3.6 解析任务

- `mode=state_update`：解析后抽取实体/状态/账本，写入档案层
- `mode=deconstruct_extract`：抽取拆书骨架与文风锚点
- `mode=memory`：轻量记忆抽取，只产出实体/事实/状态变化/场景/线程，不跑全量图解析
- `mode=chapter_memory`：章节剧情记忆，生成场景因果链/认知信息差/Δ状态/未决，存入 `kb_document`
- `mode=archive_update`：原长期档案 + 新章节 → 更新判定草稿 + 新档案版本
- `mode=storyline_network`：批次细纲 → 线网工件；`round=2` + `base_scope_key` 做证据修正
- 任务状态落盘 `parse_jobs.json`，重启后未完成任务标为 `interrupted`
- `progress` 每 3 秒刷新阶段计数与每章状态

---

## 4. API 参考

### 4.1 通用约定

| 项 | 说明 |
|---|---|
| Base URL | `https://kg.zmteams.site` |
| 认证 | 写接口 `X-API-Key: <key>` |
| 请求体 | JSON，1 B – 4 MB |
| 错误码 | 400 参数错误；401 密钥错误；503 写接口未启用；500 内部错误 |
| 作品定位 | `work` 可传标题或 id，省略时取第一个作品 |

### 4.2 作品

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/works` | 列表 |
| POST | `/api/works` | 创建 `{title, description}` |
| PUT | `/api/works/{id}` | 修改 `{title, description}` |
| DELETE | `/api/works/{id}` | 删除（含章节、快照、解析记录） |
| GET | `/api/work_types` | 作品类型注册表 |
| POST | `/api/work_types` | 注册类型 `{work_type, label, description}` |
| GET | `/api/work_profile?work=` | 作品画像 |
| POST | `/api/work_profile` | `{work, work_type, genre, world_summary, protagonist_name, resource_units, entity_types, story_categories, archive_modules, glossary, allow_debt, read_layer}` |
| GET | `/api/documents?work=&kind=&scope_key=&version=&status=&limit=&offset=` | 文档工件列表或单件 |
| POST | `/api/documents` | 保存文档工件 `{work, kind, scope_key, content, markdown, source_chapters, status, prompt_name, prompt_version, model, schema_version}` |
| POST | `/api/documents/review` | 接受/拒绝工件 `{work, kind, scope_key, version, action, base_version}`；冲突返回 409 |
| POST | `/api/documents/reproject` | 重新投影章节记忆 `{work, kind, scope_key, version}` |
| GET | `/api/documents/search?work=&q=&kind=&limit=` | 按 FTS 检索 active 工件 |
| GET | `/api/context?work=&chapter=&max_chars=` | 组装长期档案、上一章章节记忆、线网、编译档案 |
| GET | `/api/canonical?work=&module=&info_type=&limit=&offset=` | 读取 canonical 影子层 |
| POST | `/api/canonical/apply` | 应用结构化 changes `{work, chapter_index, changes}` |
| GET | `/api/reconcile?work=&chapter=` | 对账章节证据与 canonical changes |
| GET | `/api/archive_changes?work=&chapter=` | canonical 变更日志 |

工件状态：`draft → reviewed → active`；`active` 只能有一个，新 active 会把旧版置为 `superseded`。编辑/删除第 N 章会 supersede `chapter>=N` 的章节记忆，以及受影响的长期档案、线网。  
投影状态：`pending / done / failed / not_applicable`；失败可用 `/api/documents/reproject` 重放。  
`GET /api/documents?status=draft` 可作为 review 队列。

### 4.3 章节

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/sources?work=` | 章节列表（含 `chapter_index`、`has_raw`、`outline_raw`） |
| POST | `/api/sources` | 上传，见下方字段 |
| PUT | `/api/sources/{id}` | 编辑 `{title, content, expected_edit_revision?}`，自动回滚 |
| DELETE | `/api/sources/{id}` | 删除，自动回滚 |

上传字段：

```json
{
  "work": "示例作品",
  "title": "第1章 山门",
  "chapter_index": 1,
  "content": "正文（兼容字段）",
  "content_raw": "初稿",
  "content_polished": "终稿",
  "outline_raw": "本章细纲"
}
```

返回：`{"id","title","chunks","work","chapter_index"}`

### 4.4 检索

`GET /api/search`

| 参数 | 说明 |
|---|---|
| `q` | 必填 |
| `work` | 作品 |
| `top_k` | 1–20，默认 5 |
| `include_inactive` | `1` 时包含死亡/损毁实体 |
| `scope` | `writing`（默认）或 `deconstruct` |

返回词法命中与卡片：

```json
{
  "work": "示例作品",
  "mode": "lexical",
  "hits": [{"score": 0.0164, "text": "第1章 山门 ……", "cards": ["杨凌 · 力量 · 99点"]}],
  "inactive_entities": []
}
```

`scope=deconstruct` 返回 `trope_skeleton` / `style_anchor` 记录。

### 4.5 实体

`GET /api/entity?name=&work=&at_chapter=`

- `name` 支持标准名或任意别名
- `at_chapter` 使用 `kb_archive_change` 回放到该章，返回当时的 canonical/events/knowledge/threads
- 缺省返回当前状态

```json
{
  "work": "示例作品",
  "archive": true,
  "canonical_name": "马东",
  "aliases": ["马副馆主", "马教习"],
  "hard_rules": {"personality_core": "笑面虎"},
  "snapshot": {"chapter_index": 2, "lifecycle_status": "active",
               "dynamic_state": {"inventory_ledger": {"currency": {"铜钱": 600}}}},
  "related": [{"name": "赵洵", "surface_relation": "师徒", "location": "山门"}]
}
```

`/api/entity` 同时返回：

```text
canonical    当前状态条目
events       该人物参与的场景/事件
relations    该人物作为 from 或 to 的关系
knowledge    该人物的认知/POV
threads      当前未决线程
```

### 4.5.1 关联查询

```text
GET /api/events?work=&chapter=&participant=
GET /api/relations?work=&from=&to=
GET /api/knowledge?work=&holder=&chapter=
```

`/api/events` 返回场景/事件：

```json
{
  "name": "一万锤的资格考验",
  "chapter": 4,
  "participants": ["唐三", "唐昊"],
  "cause": "...",
  "outcome": "...",
  "evidence": "..."
}
```

`/api/relations` 返回人物关系：

```json
{
  "from": "唐昊",
  "to": "唐三",
  "relation": "父子亲近",
  "value": "首次紧紧拥抱",
  "chapter": 9
}
```

查两个人之间的双向关系：

```text
GET /api/relations?work=&between=唐昊,唐三
```

`between` 一次返回 `A->B` 和 `B->A`，每条带 `direction`。

`/api/knowledge` 返回角色认知：

```json
{
  "holder": "唐三",
  "type": "judgment",
  "content": "唐昊不知道唐三修炼玄玉手",
  "audience": ["唐三"],
  "chapter": 9
}
```

这些数据来自 `chapter_memory` 的 `scenes/cognition/state_deltas/unresolved` 投影，不额外调用模型。

`/api/entity` 的 `events/relations/knowledge/threads` 默认各返回 20 条，`counts` 给出总数。  
`/api/events`、`/api/knowledge`、`/api/relations` 支持 `limit/offset`，默认 `limit=50`。

`POST /api/entity`：

```json
{
  "work": "示例作品",
  "name": "马东",
  "aliases": ["马副馆主"],
  "entity_type": "character",
  "role_tier": "core",
  "hard_rules": {"identity_and_appearance": "铁掌武馆副馆主"},
  "chapter_index": 1,
  "lifecycle_status": "active",
  "dynamic_state": {"current_location": "武馆"},
  "cognition_and_relations": {"relations": [{"target": "杨凌", "surface_relation": "师徒"}]},
  "overwrite_hard_rules": false
}
```

### 4.6 故事记录

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/story_record` | `{work, category, chapter_index, payload}` |
| GET | `/api/story_record?work=&category=&at_chapter=&limit=&offset=` | 读取 |

`/api/story` 是统一读取入口：只支持 `work/subject/module/page_size/offset`，读取层由作品画像 `read_layer` 决定。  
`/api/canonical` 是内部/高级接口，支持 `subject/module/info_type/entry_key` 过滤；`/api/canonical/apply` 应用结构化 changes。  
canonical 的 `entry_key` 恒为 `主体:字段`，不带模块号前缀；模块写在 `module` 列。`七:唐三:境界`、`模块五、唐三:境界` 这类写法在写入时会被归一化为 `唐三:境界`，不可解析为 `主体:字段` 的 change 会被跳过并记 warning，不会中断整章。  
`/api/entity` 会同时返回实体的 `canonical` 条目，便于从人物直接关联当前状态。  
`/api/story_record` 是档案原始记录与调试入口，支持 `limit/offset`。  
`/api/canonical`、`/api/reconcile`、`/api/archive_changes`、`/api/story_record` 是管理/调试接口，读取也需要 `X-API-Key`。  
多作品库中查询接口必须带 `work`，否则返回 400。

### 4.7 编译

`POST /api/compile_archive`

```json
{"work": "示例作品", "chapter_index": 12, "outline": "【剧情1】……"}
```

返回：

```json
{
  "work": "示例作品",
  "genre": "玄幻",
  "chapter_index": 12,
  "previous_chapter_raw": "上一章初稿……",
  "compiled_archive_markdown": "### 模块一……\n### 模块九……",
  "matched_style_anchors": ["……"]
}
```

模块四自动包含主角 1 度关系；模块七附倒计时；模块八把未揭穿伏笔格式化为 `[反派表象行为：…；主角认知盲区锁：…]`。

### 4.8 审计

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/audit?work=&llm=` | 全书一致性检查 |
| POST | `/api/audit` | 初稿预审 |

预审请求：

```json
{"work": "示例作品", "chapter_index": 12, "text": "正文……", "llm": true}
```

返回：

```json
{"passed": false, "violations": [
  {"type": "HARD_RULE_CONFLICT", "entity": "马东", "detail": "……"},
  {"type": "REDLINE_BREACH", "detail": "……"},
  {"type": "LEDGER_ERROR", "entity": "杨凌", "detail": "本章支出 1200，当前账面合计 1000"}
]}
```

`type` 取值：`HARD_RULE_CONFLICT`、`REDLINE_BREACH`、`DEAD_CHARACTER_ACTIVE`、`LEDGER_ERROR`、`ITEM_ERROR`、`EVIDENCE_MISMATCH`、`NEEDS_REVIEW`。

`llm=true` 时：

- `method=structured`：先抽取 `state_changes`，再由代码对账，返回 `extracted_changes`
- 存在 `hard_rules` 或 `redline` 时额外跑语义检查，返回 `hard_rule_check=checked`
- 没有硬规则/红线时 `hard_rule_check=skipped_no_rules`
- 抽取失败：`method=regex_fallback`，退回正则扫描
- `llm=false`：`method=regex`

账本按 `change.entity` 取各自余额；`allow_debt=true` 只有在同批抽到 `kind=debt` 时才豁免超支。

### 4.9 解析

`POST /api/parse`

```json
{"work": "示例作品"}
```

默认 `mode=chapter_memory`，不传 `source_ids` 时解析该作品全部来源。

```json
{"work": "示例作品", "mode": "archive_update", "source_ids": ["可选"]}
```

- `chapter_memory`：默认模式，章节剧情记忆（场景因果/认知信息差/Δ状态/未决），写入 `kb_document`
- `archive_update`：长期档案增量更新，生成 changes 与 canonical
- `storyline_network`：批次线网拆解；二轮用 `round=2` + `base_scope_key`
- `state_update`：旧档案状态更新
- `deconstruct_extract`：拆书骨架与锚点
- `memory`：旧轻量记忆抽取

返回 202：

```json
{"id": "job_id", "status": "queued", "work": "示例作品", "mode": "state_update"}
```

`GET /api/parse/{job_id}`：

```json
{
  "id": "job_id",
  "status": "running",
  "batch_id": "…",
  "progress": {
    "status": "running",
    "stages": {"check": {"running": 1}, "resolution": {"queued": 1}, "publish": {"queued": 1}},
    "sources": [{"source_id": "…", "extraction": "completed",
                 "stages": {"check": "running", "resolution": "queued", "publish": "queued"}}]
  },
  "archive": {"status": "running", "mode": "state_update"},
  "summary": null,
  "error": null
}
```

终态：`completed` / `partial` / `failed` / `interrupted`。

`mode=chapter_memory` 的章节并发见 `FXI_KB_MEMORY_CONCURRENCY`。单章失败（模型返回非法 JSON、重试后仍失败、投影失败）不会中断整批：该章记为 `status=failed` 并留在 `summary`，其余章节继续，任务终态为 `partial`。

### 4.10 文档端点

| 路径 | 内容 |
|---|---|
| `/docs` | 速查页 |
| `/swagger` | Swagger UI |
| `/api/openapi.json` | OpenAPI JSON |
| `/api/openapi.yaml` | OpenAPI YAML |

---

## 5. 典型工作流

### 5.1 连载一章

```text
1. POST /api/compile_archive（chapter_index=N，outline=细纲）
2. 写作模型依据九模块生成 content_raw
3. POST /api/sources（chapter_index=N，content_raw，content_polished）
4. POST /api/parse {"mode":"state_update"}
5. 轮询 /api/parse/{id} 直到 completed
6. POST /api/audit（llm=true）预审
7. 通过后进入下一章；不通过按 violations 修补
```

### 5.2 拆书入库

```text
1. POST /api/works + POST /api/work_profile {"work_type":"deconstruct"}
2. POST /api/sources（章节）
3. POST /api/parse {"mode":"deconstruct_extract"}
4. GET /api/search?scope=deconstruct&q=打脸
5. GET /api/story_record?category=style_anchor
```

### 5.3 状态回滚

```text
删除第 N 章：DELETE /api/sources/{id}
修改第 N 章：PUT /api/sources/{id}
两者都会删除 chapter_index >= N 的快照，返回 deleted_snapshots
```

### 5.4 入库前预审

```text
POST /api/audit {"text": 初稿, "llm": true}
→ passed / violations
```

---

## 6. 运维

### 6.1 服务器

| 项 | 值 |
|---|---|
| 主机 | `43.155.187.124`（腾讯云 2C / 1.9 GB） |
| 代码 | `/home/ubuntu/fxi-bench` |
| 数据库 | `/home/ubuntu/fxi-bench/data/bench-db.sqlite3` |
| 服务 | `systemd` 单元 `fxi-kb`，监听 `127.0.0.1:8766` |
| 反代 | nginx `kg.zmteams.site` → `127.0.0.1:8766` |
| 证书 | `/etc/letsencrypt/live/kg.zmteams.site/` |

### 6.2 环境变量

`/home/ubuntu/fxi-bench/.env`（权限 600）：

```ini
FXI_KB_API_KEY=...
FXI_KB_LLM_BASE_URL=https://wd.zmteams.site/v1
FXI_KB_LLM_API_KEY=sk-...
FXI_KB_LLM_MODEL=global:deepseek-v4.1-flash
FXI_KB_PARSE_CONCURRENCY=1
FXI_KB_MEMORY_CONCURRENCY=4
FXI_KB_MEMORY_RETRIES=2
```

`FXI_KB_MEMORY_CONCURRENCY` 只影响 `chapter_memory` 的模型调用并发（1–8，默认 4），数据库写入仍按章节顺序串行；`FXI_KB_MEMORY_RETRIES` 是单章传输层失败（429/5xx/超时）的重试次数（0–5，默认 2），退避 1s、2s。

### 6.3 常用命令

```bash
sudo systemctl restart fxi-kb
systemctl is-active fxi-kb
sudo journalctl -u fxi-kb -n 200 --no-pager
curl -s http://127.0.0.1:8766/health
```

### 6.4 备份

```bash
sqlite3 ~/fxi-bench/data/bench-db.sqlite3 ".backup /tmp/kg-$(date +%F).sqlite3"
```

（服务器未装 sqlite3 CLI 时用 `python3 -c "import sqlite3; ..."`。）

---

## 7. 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| 401 invalid api key | 密钥不匹配 | 检查 `X-API-Key` 与 `.env` |
| 503 write API disabled | 未配置 `FXI_KB_API_KEY` | 写入 `.env` 并重启 |
| 400 mode must be … | parse mode 非法 | 用 `state_update` / `deconstruct_extract` / `memory` / `chapter_memory` / `archive_update` / `storyline_network` |
| 400 LLM … not configured | 未配置 LLM | 补齐三个 `FXI_KB_LLM_*` |
| 500 FOREIGN KEY constraint failed | 历史删除 bug | 已在 `repository_projects.py` 修复 |
| parse partial，publish failed | 解析阶段失败 | 查 `journalctl` 与 batch item diagnostic |
| `parser stack overflow` | SQLite 3.45 表达式过深 | 已在卡片索引改为子查询 |
| archive failed | 模型输出非法 JSON | 查 job.archive.error，重跑解析 |

---

## 8. 验收清单

2026-10-29 在 `kg.zmteams.site` 实测：

| 验收项 | 结果 |
|---|---|
| 别名归并 | ✅ |
| A 路只读 | ✅ |
| 章节快照 | ✅ |
| 1 度关系 | ✅ |
| 死人屏蔽 | ✅ |
| 九模块编译 | ✅ |
| 伏笔格式化 | ✅ |
| 倒计时 | ✅ |
| 账本校验 | ✅ |
| 语义审校 | ✅ |
| 拆书检索 | ✅ |
| 编辑回滚 | ✅ |

测试：`pytest` 全量 **1813 passed / 7 skipped**。

---

## 9. 已知限制

1. 语义审校依赖 LLM，结果有模型波动
2. 红线用 `story_record` 维护，没有独立管理界面
3. 拆书检索是字面匹配，不是向量语义检索
4. 解析任务状态存 `parse_jobs.json`，仅保留最近 100 条
5. 默认解析并发 1，2C/2G 机器不建议调高
6. `chapter_memory` 并发只省模型等待时间，SQLite 写入仍串行，吞吐上限受单章最长调用限制
7. `archive_update` 仍按章串行且每章重写整本档案，是当前最大的吞吐瓶颈

---

## 10. 相关文档

- [API 速查与真实调用](api-usage.md)
- [档案层改造](archive-upgrade.md)
- [一致性审计](consistency-audit.md)
- [HTTP 服务说明](kb-http-service.md)
- [词法检索设计](lexical-retrieval.md)
- [OpenAPI](openapi.yaml)
