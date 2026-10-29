# 档案层改造（第一里程碑）

对应《知识图谱与记忆库系统升级规范》的第一批落地：作品画像、双轨章节、实体 A/B/C 档案、章节快照回滚、生命周期过滤、一键编译、初稿预审。

## 已实现

### 数据表（`src/fxi_novel/knowledge/archive.py`）

| 表 | 内容 |
|---|---|
| `kb_work_profile` | `work_type`（writing/deconstruct）、`genre`、`world_summary`、`protagonist_name` |
| `kb_source_meta` | `chapter_index`、`outline_raw`、`content_raw`、`content_polished` |
| `kb_entity` | 标准名、`aliases`、`entity_type`、`role_tier`、`hard_rules` |
| `kb_entity_snapshot` | 按 `chapter_index` 的 `lifecycle_status`、`dynamic_state`、`cognition` |
| `kb_story_record` | `power_and_laws` / `map` / `faction` / `active_storyline` / `foreshadow` / `completed_storylines` / `trope_skeleton` / `style_anchor` |

### API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST | `/api/work_profile` | 作品画像 |
| POST | `/api/entity` | 创建/更新实体；带 `chapter_index` 时同时写快照 |
| GET | `/api/entity?name=&work=&at_chapter=` | 别名解析 + 指定章节快照 |
| POST | `/api/sources` | 支持 `chapter_index`、`outline_raw`、`content_raw`、`content_polished` |
| DELETE | `/api/sources/{id}` | 删除后自动回滚 `chapter_index >= N` 的快照与故事记录 |
| POST | `/api/story_record` | 写入结构化故事记录 |
| GET | `/api/story_record?category=&at_chapter=` | 读取记录 |
| POST | `/api/compile_archive` | 生成九大模块 Markdown |
| POST | `/api/audit` | 入库前初稿预审（当前检查已死亡/已毁实体出场） |
| GET | `/api/search` | 支持 `include_inactive`；死亡/损毁实体默认不召回 |

### 验收结果（本地实测）

| 验收项 | 结果 |
|---|---|
| 别名归并 | `GET /api/entity?name=马副馆主` → 返回「马东」同一实体，`aliases` 含「马副馆主」 |
| A 路只读 | `hard_rules` 只在显式 `overwrite_hard_rules=true` 时改写；快照写入不影响 |
| 时间机回滚 | 第 2 章删除后，`at_chapter=2` 返回第 1 章快照（铜钱 1000），删除接口返回 `deleted_snapshots=2` |
| 死人屏蔽 | 默认 `q=赵昆` 返回 0 条并提示；`include_inactive=1` 返回结果并列出 `inactive_entities` |
| 一键编译 | `POST /api/compile_archive` 返回模块一至模块九，含上一章 `content_raw` |

单元测试：`tests/test_archive.py`（别名、回滚、编译、初稿预审）。

## 解析写入档案层（已实现）

`POST /api/parse` 新增 `mode`：

```json
{"work": "神域囚徒", "source_ids": ["..."], "mode": "state_update"}
```

解析批次完成后，服务会读取该章的 `content_raw`，调用 LLM 抽取：

- 实体与别名（新称呼追加到 `aliases`）
- `lifecycle_status` 与 `dynamic_state`
- `ledger_delta`（货币增减、物品增删），由后端做确定性加减
- `cognition_and_relations`
- `story_records`

结果写入 `kb_entity_snapshot` / `kb_story_record`，任务状态的 `archive` 字段给出每章应用结果。实测：

```text
status=completed archive=completed
chapter=1 entities=2 records=1
杨凌 ledger={"currency":{"铜钱":-400},"items":["灵米"]}
```

`mode=deconstruct_extract` 目前返回 `not_implemented`。

`mode=deconstruct_extract` 已实现：把章节还原为 6–8 步去名化剧情骨架和 200–350 字文风锚点，写入 `trope_skeleton` / `style_anchor` 故事记录。

实测（拆书测试章）：

```text
steps=6  anchor_chars=119
tags=打脸,扮猪吃虎,深藏不露,以弱胜强
【剧情1】[反派A]背手踱步，以指导者姿态当众否定[主角]的姿势……
anchor=[反派A]背着手踱步，冷笑道：“习武贵在意志，你这姿势错了。”[主角]没有争辩……
```

## 本轮补齐

- **1 度关系召回**：`GET /api/entity` 返回 `related`；`compile_archive` 模块四自动加入主角的 1 度关系
- **章节编辑回滚**：`PUT /api/sources/{id}` 会回滚 `chapter_index >= N` 的快照，返回 `deleted_snapshots`
- **初稿预审扩展**：除死亡角色外，新增 `LEDGER_ERROR`（正文支出超过账面余额）
- **拆书检索**：`GET /api/search?scope=deconstruct` 直接检索 `trope_skeleton` / `style_anchor`
- **生命周期过滤**：词法命中文本中出现死亡/损毁实体且查询未点名时，默认跳过
- **最后 150 字定格**：`state_update` 每章写入 `active_storyline` 的 `latest_node_snapshot`
- **倒计时**：`timer` 故事记录每章自动递减，`compile_archive` 模块七展示剩余章数

## 统一验收结果（2026-10-29，kg.zmteams.site）

| 验收项 | 结果 |
|---|---|
| 别名归并（马副馆主 → 马东） | ✅ |
| A 路只读（hard_rules 不被快照覆盖） | ✅ |
| 章节快照（at_chapter=2 铜钱 600） | ✅ |
| 1 度关系（杨凌 → 赵洵） | ✅ |
| 死人屏蔽（默认 0 条） | ✅ |
| 九模块编译（9 个标题 + 上一章） | ✅ |
| 账本校验（超支报 LEDGER_ERROR） | ✅ |
| 编辑回滚（PUT 后回到 1000） | ✅ |
| 拆书检索（scope=deconstruct） | ✅ |

9/9 通过；测试作品已清理。

## 尚未实现（下一里程碑）

1. `hard_rules` / 红线的语义冲突检查（当前只做死亡与账本确定性校验）
2. 未揭穿阴谋的自动格式化（`[反派表象 + 主角认知盲区锁]`）

## 使用示例

```bash
# 作品画像
curl -X POST https://kg.zmteams.site/api/work_profile \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"work":"神域囚徒","work_type":"writing","genre":"玄幻","protagonist_name":"杨凌"}'

# 实体 + 第 1 章快照
curl -X POST https://kg.zmteams.site/api/entity \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"work":"神域囚徒","name":"马东","aliases":["马副馆主"],
       "hard_rules":{"personality_core":"笑面虎"},
       "chapter_index":1,"dynamic_state":{"current_location":"武馆"}}'

# 按别名查历史快照
curl "https://kg.zmteams.site/api/entity?name=马副馆主&work=神域囚徒&at_chapter=1"

# 一键编译
curl -X POST https://kg.zmteams.site/api/compile_archive \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"work":"神域囚徒","chapter_index":12,"outline":"【剧情1】..."}'
```
