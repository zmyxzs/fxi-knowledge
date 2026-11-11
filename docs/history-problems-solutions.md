# 历史问题与解决方案

状态：2026-11-11  
用途：记录已经踩过的坑、当时的修法、留下的副作用，避免后续重复补丁。

关联：

- [架构契约](architecture-contract.md)
- [需求状态](requirements-status.md)
- [补丁债务](patch-debt.md)
- [兼容性矩阵](compatibility-matrix.md)

---

## 1. 时间线

| 阶段 | 主要工作 | 主要问题 |
|---|---|---|
| 初始审计 | 从 Fxi-novel 抽出独立知识库 | 打包、文档、API、测试缺失 |
| 档案层 | 实体、快照、故事记录、账本 | 快照覆盖、关系丢失、死亡复活、timer 错减 |
| 解析链 | 解析批次、候选、发布 | 章节号塌陷、长章截断、JSON 失败、任务卡死 |
| 提示词融入 | chapter_memory / archive_update / storyline_network | 契约过重、强制字段、证据不逐字、投影缺失 |
| 质量与兼容 | fuzzy 校验、golden set、跨书抽样 | 规则过拟合、warning 不可见、模型波动 |
| 统一读取 | `/api/story` archive-only、分页、去重 | 两套源、默认作品、重复记录、分页缺失 |
| 部署与外部 | 生产部署、AI Studio 导入 | 生产版本落后、AI Studio 运行时不匹配 |

---

## 2. 数据正确性问题

| 问题 | 根因 | 修法 | 残留/教训 |
|---|---|---|---|
| 死亡角色被复活 | `apply_output` 默认 `lifecycle_status=active` | 省略时继承上一快照，只有显式值才改 | “未提及”和“活着”必须区分 |
| 关系列表被替换 | `cognition.update()` 浅合并 | relations 按 target 合并 | 删除关系需要显式 tombstone |
| 新建 timer 当章就减 1 | 抽取后立即 `tick_timers` | 记录 `created_chapter`，只减更早的 | 旧数据需要回填 |
| 未来 timer 出现在早期编译 | 编译查询没带 `at_chapter` | 加 as-of 过滤 | 旧 timer 无章节需默认值 |
| 上一章原文取 `N-1` 精确值 | 章节有缺口时取不到 | 改为最近的 `<= N-1` | 缺口语义要定义 |
| 不同币种相加 | 账本只有一个余额 | 按币种分账 | 需要单位表 |
| 中文数字/动词漏检 | 只做字面匹配 | 数字归一化 + 动词表 | 归一化会误判 |
| 只校验主角账本 | 审计固定主角 | 按 `change.entity` 分账 | 非主角账本可能没抽到 |
| 别名子串误报 | `name in text` | 词边界/最长匹配 | 中文边界难定 |
| 资源双写冲突 | `resources` 与 `inventory_ledger` 同时写 | 读取时 `resources` 优先 | 仍是双写，未彻底统一 |

**教训**：账本、快照、关系是“状态”，不是“日志”。状态更新必须区分“未提及、显式清空、显式修改”。

---

## 3. 解析与抽取问题

| 问题 | 根因 | 修法 | 残留/教训 |
|---|---|---|---|
| 存量章节号塌成 1 | 标题解析失败 | 从标题回填，解析后写 meta | 标题不规范会复发 |
| 长章被截断 | `text[:12000]` | 分块抽取 + 合并 | 跨块边界仍可能漏 |
| LLM JSON 失败 | 无重试/Schema | Schema 校验 + 一次修复重试 | 成本上升 |
| 拆书模式跑完整解析 | 无独立轻流程 | 拆书独立任务 | 需要任务类型 |
| 解析失败仍跑档案抽取 | 只看任务启动 | 仅 completed 进入档案 | 部分成功语义 |
| 乱序解析 timer 全减 | 不判断章节顺序 | 按 `created_chapter` 比较 | 旧数据回填 |
| 解析任务 dict/list 混用 | 文件格式变化 | 两种都接受 | 需要版本字段 |
| SQLite 解析栈溢出 | 卡片索引嵌套过深 | 子查询先算 published 内容 | 复杂 SQL 要压平 |
| 来源 ID 跨作品 | 未校验归属 | 解析前校验 | 跨作品需求要显式开关 |

**教训**：解析链的输入、章节号、来源归属、任务状态必须先校验，再调用模型。模型失败不能污染档案。

---

## 4. API 与契约问题

| 问题 | 根因 | 修法 | 残留/教训 |
|---|---|---|---|
| 未知 `scope` 静默走 writing | 无枚举校验 | 非法 400 | 客户端要改 |
| 同名作品可创建 | 无唯一约束 | 唯一约束/强制 id | 旧数据重名 |
| 别名冲突取第一条 | `LIMIT 1` | 冲突检测 + 人工确认 | 需要评审 |
| entity 两种返回结构 | archive vs legacy | 统一 schema | 兼容旧客户端 |
| sources/story_record 无分页 | 未设计 | `limit/offset` | 客户端适配 |
| `source_ids` 不校验归属 | 只查存在 | 校验 work_id | 跨作品需求 |
| OpenAPI 缺新接口 | 手写 spec | 补 spec + 契约测试 | 仍可能漂移 |
| README 测试数过期 | 手写数字 | 定期更新 | 应自动生成 |
| 客户端缺新接口 | 手写客户端 | 补方法 + 契约测试 | 兼容旧版本 |
| 多作品默认第一个 | `resolve_work(None)` | 多作品必须带 work | 老客户端会 400 |
| `/api/story` 只能看前 100 | 无分页 | offset/next_offset | offset 不稳定 |

**教训**：API 契约要由测试锁住；默认行为在多作品场景必须显式。

---

## 5. 提示词与工件问题

| 问题 | 根因 | 修法 | 残留/教训 |
|---|---|---|---|
| 契约过重，模型漏项 | 一个 prompt 同时要场景/认知/状态/线程/档案 | 拆成 chapter_memory / archive_update / storyline_network | 仍有一次调用做多类工件 |
| 强制 cognition 非空 | 为了满足规则 | 改为“有则写，没有可空” | 已删除硬规则 |
| 强制 unresolved 至少一条 | 为了不漏线程 | 改为“有跨章影响才写” | 已删除硬规则 |
| 场景数必须 3–8 | 用样例调出来的硬约束 | 降为 warning | 过碎/过粗仍不可阻断 |
| evidence 必须逐字 | 防幻觉 | 运行时 warning，评估严格 | 模型仍会改写 |
| 分类/字段缺失 | 模型输出漂移 | 别名归一 + warning | 核心分类仍未完全统一 |
| 工件生成但投影失败 | 没有状态 | `projection_status` + reproject | 没有自动重放队列 |
| 重跑产生重复记录 | 没有幂等键 | 去重键带章节 | 语义重复仍可能漏 |
| 旧 memory 记录混入 | 旧模式与新投影并存 | 清理无 `source_document_id` 的章节记录 | 事实记录仍保留旧来源 |
| 线网生成后没人用 | 没有消费链路 | 待接入 context/compile | 仍是工件 |
| 长期档案无 diff | 只输出最终档案 | 待做结构化 diff | 可能悄悄覆盖 |

**教训**：提示词不是越严越好。硬规则会把“没有”逼成“编造”；运行时只应对“无法投影”hard fail。

---

## 6. 运维与部署问题

| 问题 | 根因 | 修法 | 残留/教训 |
|---|---|---|---|
| 503 裸 socket RST | 并发上限在 server 层直接写 socket | Handler 层返回 JSON 503 | POST body 未读完仍可能 RST |
| 任务文件全量重写 | 每次进度都写 100 条 | 只状态变化写 | 未落 SQLite |
| 删除作品 FK 失败 | 删除顺序 | 先删 parse unit attempt/receipts | 事务边界 |
| `purge_work` 分两个事务 | 主数据/派生数据 | 先删主数据，再清理派生 | 可能留孤儿 |
| schema 无版本 | 只 `CREATE IF NOT EXISTS` | 加列 + 迁移记录 | 仍无正式 migration 框架 |
| 生产版本落后 | 手工 scp | py_compile + 备份 + smoke | 需要版本号 |
| scp 部分文件损坏 | 上传中断 | 重新上传 + 编译校验 | 需要 checksum |
| AI Studio 导入失败 | 运行时是 Node，项目是 Python | 不能直接跑 | 需要前端/Cloud Run 方案 |

**教训**：部署必须可验证、可回滚；生产与工作区不能靠记忆同步。

---

## 7. 补丁反模式

### 7.1 每加一个需求就加字段

例子：

- `kb_relation` 曾经想单独建关系表
- `kb_document` 之后又想加 `kb_storyline*`
- 资源双写 `resources` / `inventory_ledger`

结果：字段越多，读取路径越多，最后不知道哪套是真源。

规则：先看现有 claim/register/event 能不能表达；不能才加。

### 7.2 用硬校验修样例

例子：

- 场景数必须 3–8
- evidence 必须逐字
- cognition 不能为空
- unresolved 至少一条

结果：模型为了过校验开始编造；跨书表现变差。

规则：运行时 hard fail 只保留“无法投影”；其余 warning。

### 7.3 用回退读取掩盖迁移

例子：

- `/api/story` 先读 archive，空则读 graph

结果：两个作品一个读 archive、一个读 graph，用户看到两套结果。

规则：先回填，再切换；不做永久双读。

### 7.4 用去重键掩盖语义重复

例子：

- 只按 `payload_json` 精确去重
- 旧 memory 的 `summary` 和新 chapter_memory 的 `causal_paragraph` 不是同一个 JSON

结果：重复还在，只是换了一种形式。

规则：去重键按核心字段 + 章节；旧记录要有清理策略。

### 7.5 用 offset 分页撑大数据

例子：

- `offset=100` 期间有新写入，翻页可能重复/漏

规则：小数据可用 offset；数据量大改 cursor/keyset。

### 7.6 用“默认第一个作品”保持兼容

例子：

- `resolve_work(None)` 取第一个
- 多作品时查错

规则：多作品必须显式；单作品才允许默认。

---

## 8. 解决方案的副作用

| 解决方案 | 副作用 | 缓解 |
|---|---|---|
| 模糊校验 | warning 太多，质量问题被掩盖 | 按类型聚合、周报、评估器严格 |
| 去重 | 可能删掉合法重复 | 键带章节；先 dry-run |
| archive-only | graph-only 老作品查不到 | 先回填再切换 |
| work 必填 | 老客户端 400 | 版本化 API/弃用期 |
| 自动 active | canon 污染 | 长期档案人工 accept |
| 回填 735 条 | evidence 很长、分类单一 | 后续补章节记忆/结构化 |
| offset 分页 | 翻页不稳定 | 后续 cursor |
| 别名归一 | 别名冲突被掩盖 | 冲突检测 + 人工确认 |

---

## 8. 2026-11-11 复审补充

这一轮针对统一后的查询层又发现并修了 4 个问题：

| 问题 | 根因 | 修法 | 测试 |
|---|---|---|---|
| `value=0` 被当成空 | `payload.get("value") or ...` | 用 `_first_present` 保留 0/False | `test_story_preserves_zero_value` |
| 失败投影仍可被 `/api/story` 读到 | 只按记录查，不看文档状态 | `only_projected=True`，只读 active+done 工件 | `test_story_hides_failed_projection_records` |
| 去重保留旧 memory 记录 | 去重排序按创建时间 | 有 `source_document_id` 的记录优先保留 | `test_dedupe_prefers_artifact_projection` |
| 失败投影时清理旧记录 | 只看 active 文档 | 要求 `projection_status='done'` 才清理 | `test_drop_legacy_skips_failed_projection` |

仍存在的结构性问题：

- `_project_chapter_memory` 还不是单事务，投影中途失败可能留下部分记录
- `documents` 列表没有分页
- OpenAPI 中部分查询接口的 `work` 仍写成可选
- offset 分页在写入期间不稳定
- 核心分类与模块分类仍未完全统一

## 9. 后续原则

1. 先定义契约，再写代码。
2. 先回填，再切换读取源。
3. 先 warning，再考虑 hard fail。
4. 先跨书复现，再改通用规则。
5. 一次只让模型做一类工件。
6. 一次只改一个通用层。
7. 部署必须版本化、可回滚、可 smoke。
8. 任何“补丁”都要写进 patch-debt，并说明何时删除。

---

## 10. 当前仍然存在的历史遗留

- `resources` / `inventory_ledger` 双写
- 核心分类与模块分类并存
- 别名解析全表扫
- 无正式 schema migration 框架
- 神域没有章节记忆
- 斗罗只有 3 章
- 线网未进 context/compile
- 长期档案无 diff
- POV/时态/预设未校验
- offset 分页
- 生产版本号未暴露

这些在 [requirements-status.md](requirements-status.md) 里有优先级和验收标准。
