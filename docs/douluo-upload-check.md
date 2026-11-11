# 斗罗大陆连续上传验证

## 操作

1. 从 `Fxi-novel` 库取《斗罗大陆》第 1–3 章
2. 在生产 KB 新建作品：`斗罗大陆·连续上传验证`
3. 顺序上传 3 个章节
4. 运行 `mode=memory` 更新档案
5. 检查实体、快照、故事记录、编译档案

全本文本位置：

```text
D:\文本\400+本高质量完本合集\01、玄幻\02、斗罗\斗罗大陆
```

核对结果：前 3 章与 `Fxi-novel` 库文本逐字一致（忽略空白差异），所以上传内容没有版本偏差。

## 结果

| 项 | 结果 |
|---|---|
| 作品 | 斗罗大陆·连续上传验证 |
| 章节 | 3 |
| 解析任务 | completed |
| 实体 | 唐三、唐昊、圣魂村、唐门、玄天功等 |
| 故事记录 | 135 条 |
| 快照 | 唐三 chapter_index=3 |
| 编译档案 | chapter_index=4 可生成 |

故事记录分类：

```text
fact 89
scene 13
thread 11
active_storyline 4
foreshadow 4
map 3
power_and_laws 3
faction 3
style_anchor 2
trope_skeleton 2
timer 1
```

唐三快照示例：

```text
位置：山下
眼眸紫意：收敛
身体结实程度：结实了几分
力量：增大了许多
玄功内力恢复速度：比以前快了一点
锻造锤数：八千余
```

## 发现的问题

### 1. 生产服务版本落后

生产环境：

```text
POST /api/parse mode=chapter_memory
→ 400 mode must be state_update, deconstruct_extract or memory
```

同时：

```text
/api/documents 404
/api/context 404
```

说明最新工件层/上下文层还没有部署到 `kg.zmteams.site`。

### 2. `/api/story` 与 `/api/story_record` 不一致

```text
GET /api/story?work=斗罗大陆·连续上传验证
→ total 0

GET /api/story_record?work=斗罗大陆·连续上传验证
→ 135 条
```

数据已经写进档案，但旧的故事视图读不到，属于两套读取源未统一。

### 3. 状态字段偏自然语言

`memory` 模式抽出的字段包括：

```text
位置、眼眸紫意、身体结实程度、力量、内力消耗
```

这些字段能进快照，但后续做数值比较/对齐会不稳定。属于“可接受但不理想”，先记录，不加规则。

## 结论

- 连续上传和档案更新本身可用
- 生产环境的读取/解析能力与工作区代码不一致
- 当前最大问题不是模型，而是部署版本和两套读取源

## 后续

1. 部署最新代码前，不要在 `kg` 上使用 `chapter_memory`/`archive_update`
2. 统一 `/api/story` 与 `/api/story_record` 的读取源
3. 状态字段先保留自然语言，等跨书重复出现再归一

---

## 2026-11-11 部署记录

已部署最新工作区代码到 `kg.zmteams.site`。

- 备份代码：`/home/ubuntu/fxi-kb-code-20261111-193900.tar.gz`
- 备份数据库：`/home/ubuntu/fxi-bench/data/bench-db.sqlite3.bak-20261111-193900`
- 上传代码包：`/tmp/fxi-kb-deploy.tar.gz`
- `py_compile`：通过
- `fxi-kb`：active
- 原有 `.env` 保留

部署后重新验证《斗罗大陆》前 3 章：

| 项 | 结果 |
|---|---|
| `chapter_memory` | 3 个 active 文档 |
| 投影状态 | 3 个 `done` |
| `archive_update` | 1 个 `long_term_archive/main` active |
| `/api/context` | 3 段，8440 字 |
| `/api/story_record` | 179 条 |

context 顺序：

```text
long_term_archive 5026
chapter_memory 1671
compiled_archive 1743
```

新增记录分类：

```text
fact 89
scene 27
thread 20
cognition 15
state_delta 6
```

### 仍未解决

~~`GET /api/story?work=斗罗大陆·连续上传验证` 返回 0~~

已统一：`/api/story` 只读 `kb_story_record`。斗罗返回 179 条，神域囚徒的 735 条 graph 记录已回填为 `fact` 记录。

回填脚本：

```bash
PYTHONPATH=src python scripts/backfill_graph_story.py \
  --database data/bench-db.sqlite3 --work 神域囚徒
```

结果：

```text
seen 735
inserted 735
skipped 0
```

回填前备份：

```text
/home/ubuntu/fxi-bench/data/bench-db.sqlite3.bak-before-backfill-20261111-200218
```
