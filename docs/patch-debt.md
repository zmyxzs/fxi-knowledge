# 补丁债务与停止条件

> 目的：把“遇到一个失败就加一条规则”改成“先分层，再决定是否值得改”。

## 2026-11-11 已清理

| 债务 | 处理 |
|---|---|
| 多作品默认查第一个 | 多作品必须带 `work` |
| `/api/story` 与 graph 双读 | 回填 735 条，`/api/story` archive-only |
| 列表只能拿前 100 条 | 增加 `offset/next_offset` |
| 斗罗重复记录 | 去重移除 14 条 |
| 旧 memory 章节记录混入 | 移除 10 条 |
| graph 分类查不到 | 增加 `graph_category` 过滤 |
| 0 值被当空 | `_first_present` 保留 0/False |
| 失败投影仍可读 | `only_projected` 只读 active+done |
| 去重保留旧记录 | 优先保留有 `source_document_id` 的记录 |
| 失败投影时清理旧记录 | 要求 `projection_status='done'` |

## 仍需跟踪

| 债务 | 当前状态 | 下一步 |
|---|---|---|
| 核心分类未完全统一 | 模块分类仍存在 | 核心五种 + `module` 字段 |
| `archive_update` 无 diff | 只有最终档案 | Phase 2 |
| 线网未进 context | 只有工件 | Phase 3 |
| POV/时态/预设未校验 | 只有 schema | Phase 4 |
| offset 分页不稳定 | 已可用 | 后续 cursor |
| 真实章节 fixture 不进仓库 | 本地保留 | CI 用合成用例 |
| prompt v4 未基线 | v2 有数据 | Phase 5 |

## 一、已有补丁怎么处理

| 补丁 | 性质 | 处理 |
|---|---|---|
| `kb_document` 工件层、版本、review、409 | 通用基础设施 | 保留 |
| `projection_status` / `warnings_json` | 通用失败隔离 | 保留 |
| 别名归一（场景/认知/状态变化/未决） | 兼容层 | 保留，继续扩展 |
| Hard/Soft 两层校验 | 通用降级 | 保留，作为唯一运行时策略 |
| `kb_document_fts` | 通用检索 | 保留 |
| `/api/context` 组装 | 通用读取 | 保留 |
| `--repeat` 评估与 golden set | 质量门禁 | 保留，只做抽样，不追求全覆盖 |
| 严格 `validate_chapter_memory` | 评估 oracle | 只用于评估，不再决定入库 |
| 强制 cognition 非空 | 过拟合规则 | 已删除（prompt v4） |
| 强制 unresolved 至少一条 | 过拟合规则 | 已删除（prompt v4） |
| 场景数必须 3–8 | 过拟合规则 | 已降为 warning |
| resource 必须 field/amount | 过拟合规则 | 已降为 warning |
| evidence 必须逐字 | 过拟合规则 | 运行时降为 warning，评估仍严格 |
| chapter_index 必须一致 | 过拟合规则 | 运行时降为 warning |
| 单书样例调关键词 | 数据特定 | 不进入运行时，只留在 golden set |

## 二、哪些是“真问题”，哪些只是样本差异

### 真问题（跨书复现才修）

- 投影失败且无法重放
- 同一实体别名反复分裂
- 数值方向/单位系统性错误
- 长章节截断导致尾部完全丢失
- JSON 无法解析
- 审核/回滚后 active 工件不一致

### 样本差异（记录，不立刻改）

- 某一章场景数只有 2
- 某一章没有 cognition
- 某一章线程命名不同
- 某一章 evidence 改写
- 某一章 state category 用了近义分类

处理方式：记 warning，进抽样报告；只有同类 warning 在 **≥2 本书**重复出现，才考虑改通用层。

## 三、停止条件

满足以下任一条，不再加补丁：

1. 问题只在一本书、一个章节出现
2. 修法需要新增字段，但现有 warning 已能表达
3. 修法需要再加一条 prompt 硬规则
4. 修法只对某个题材有效
5. 修法会让“空输出”变成“必须编造”
6. 修法需要模型一次处理更多内容

## 四、未来可能出现的问题

| 风险 | 现在的处理 | 还需要 |
|---|---|---|
| 模型换版本，字段名漂移 | 别名层 | 每次换模型跑 6 本抽样 |
| 长章节 >30k 字 | 截断 + warning | 分段抽取或摘要前置 |
| 系统面板/数值文 | 现有 state_deltas | 面板专用 adapter，不加新表 |
| 对话密集/多视角 | cognition/audience | POV 语义评估 |
| 战斗/追杀 | scenes | 事件链评估 |
| 投影产生重复实体 | resolve_entity | 别名合并与人工确认 |
| warning 越积越多 | warnings_json | 按类型聚合的周报 |
| FTS 旧版本残留 | join active 过滤 | 定期 vacuum/重建 |
| 审核队列无人看 | status=draft | 审核 UI/提醒 |
| 档案与 graph 两套源 | 735 条已回填，`/api/story` 只读档案 | 保留回填脚本，不再新增 graph 读取 |

## 五、后续节奏

- 每轮只做 **一个通用层改动**
- 每轮抽样 **6 本书 × 2 章**，不跑全书
- 每轮只允许 **1 条 prompt 规则变化**
- 新规则先在 1 本书试，不在 6 本同时改
- 评估报告保留 `warnings`，不只报 `valid`

## 六、当前结论

之前的大量具体处理，保留为：

- 通用基础设施
- 兼容层
- 评估 oracle

删除/降级的：

- 强制字段规则
- 强制数量规则
- 强制 evidence 逐字

后续不再按“每方面都做具体处理”推进，而是按“能否入库、能否投影、能否重放”推进。
