# 四份规格覆盖现状

状态：2026-11-11  
输入规格：

- `提取细纲.txt`：章节级剧情记忆
- `细纲转线网1.txt` / `细纲转线网2.txt`：批次故事线网络与二轮修正
- `长期档案更新.txt`：九模块长期档案增量维护

详细需求状态见 [requirements-status.md](requirements-status.md)，架构约束见 [architecture-contract.md](architecture-contract.md)。

## 1. 覆盖总表

| 规格 | 状态 | 说明 |
|---|---|---|
| 章节边界 | 🟢 | 一章一工件，`chapter:N` scope，回滚按章 |
| 场景因果链 | 🟡 | 有 `causal_paragraph`，无 11 要素逐项校验 |
| 认知与信息差 | 🟡 | 有 JSON 结构并投影到 story_record，未进 graph POV |
| Δ状态 | 🟡 | resource/item/debt 进账本；relation/goal 只进 story_record |
| 未决线程 | 🟡 | 有 new/advanced/blocked/resolved，无线程词典/生命周期 |
| 批次快照 | 🔴 | 没有“截至第 X 章当前状态快照” |
| 线网拆解 | 🟢 生成 / 🔴 消费 | 能生成九节工件，未进 context/compile |
| 二轮修正 | 🟢 生成 / 🟡 审计 | 有第二轮，无结构化证据差异报告 |
| 长期档案增量更新 | 🟡 | 能生成新版本，无模块 diff/审阅 |
| 九模块骨架 | 🟢 | 可配置 |
| 结构化流转 | 🔴 | 角色→关系网、线→归档、地图→拓扑未自动流转 |
| 三类型分治 | 🔴 | 无 rule/dynamic/history 标记 |
| 单一事实源 | 🟡 | story 已 archive-only；模块分类仍并存 |
| 时态/预设隔离 | 🔴 | 无 preset 字段与 as-of 校验 |
| POV 认知边界 | 🟡 | schema 有 holder/audience，graph 未用 |
| 诊断式校验 | 🟡 | 有 warning 分类，无完整判定清单 |

## 2. `提取细纲.txt`

### 已覆盖

- 章节号、章节边界
- 场景、参与者、证据
- 认知类型：judgment/rumor/lie/update/info_gap/unknown
- 状态增量：character/resource/item/debt/relation/goal
- 未决线程与状态转移
- 章节快照
- 工件版本、投影、重放、检索、上下文

### 仍缺

- 场景 11 要素校验
- 3–7 场景硬约束（当前仅 warning）
- 认知 POV 投影到 graph
- relation/goal 进 `StoryRegisterEntry`
- 四本内部账本
- 批次快照
- 对话/心理/战斗/面板专用编码
- 跨章数值与顺序校验

## 3. `细纲转线网1/2.txt`

### 已覆盖

- 第一轮/第二轮
- 九节结构
- 八维出口
- 假设清单
- 批次工件版本
- 展示用事件名

### 仍缺

- context 固定接入
- compile 固定模块
- 结构化线/节点/耦合表
- 与章节记忆交叉校验
- 二轮证据差异报告
- 马甲/并行线/爽点轮换的结构校验

## 4. `长期档案更新.txt`

### 已覆盖

- 原档案 + 新章 → 新版本
- 九模块
- active 唯一
- context 读取
- 回滚按来源章节失效

### 仍缺

- 更新判定草稿
- 模块级 diff
- `info_type=rule/dynamic/history`
- 单一事实源/跨栏去重
- POV/时态/预设校验
- 角色/线/地图自动流转
- 模块四/五/六结构化面板

## 5. 与首次审阅相比的变化

首次审阅时，以下能力完全缺失：

- 章节记忆 JSON
- 认知/信息差
- 线网生成
- 长期档案版本
- 工件层
- 投影与重放
- 模糊兼容
- 跨书抽样

现在这些都已经有最小可用实现。

仍然没有解决的是：

- 语义质量
- 结构化流转
- POV/时态
- 线网消费
- 档案 diff
- graph 结构化投影

## 6. 结论

四份规格不是“全部做完”，而是：

```text
生成端：大部分可用
结构端：章节记忆部分可用
消费端：长期档案可用，线网未接
语义端：未完成
```

下一阶段不应继续加字段，而应按 [architecture-contract.md](architecture-contract.md) 的 Phase 2–5 推进。
