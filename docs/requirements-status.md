# 需求与问题状态

状态：2026-11-11  
关联：[架构契约](architecture-contract.md) / [补丁债务](patch-debt.md) / [四份规格覆盖](spec-coverage.md)

## 1. 当前作品

| 作品 | 记录 | 工件 | 说明 |
|---|---:|---|---|
| 神域囚徒 | 735 | 无 chapter_memory | 旧 graph 回填为 fact |
| 斗罗大陆·连续上传验证 | 155 | 3 个 chapter_memory + 1 个 long_term_archive | 前 3 章连续上传 |

查询必须带 `work`：

```text
/api/story?work=神域囚徒
/api/story?work=斗罗大陆·连续上传验证
```

不带 `work`：

```text
400 work is required when multiple works exist
```

---

## 2. 四份提示词需求

### 2.1 `提取细纲.txt` → `chapter_memory`

状态：**生成端可用，语义与结构化投影未完全到位。**

已实现：

- 章节边界
- 场景 + 因果段
- 认知/信息差结构
- Δ状态
- 未决线程
- 章节快照
- 工件版本、active/superseded
- 投影到 `kb_story_record`
- 投影重放
- FTS 检索
- 上下文组装
- 模糊兼容与 warning
- golden set 评估器

未实现：

| 需求 | 现状 | 影响 |
|---|---|---|
| 场景 11 要素逐项校验 | 只有段落长度 warning | 因果段可能缺项 |
| 3–7 场景硬约束 | 3–8 仅 warning | 过碎/过粗无法阻断 |
| 认知 POV 投影 | 只进 story_record | graph 无 holder/audience |
| 关系/目标结构化 | 只进 story_record | 未进 StoryRegisterEntry |
| 四本内部账本 | 无 | 事件/状态/认知/线程未分账 |
| 批次当前快照 | 无 | 下一批无法直接接续 |
| 对话/心理/战斗/面板编码规则 | 无 | 特殊内容被压成普通场景 |
| 跨章数值/顺序校验 | 无 | 冲突只能人工发现 |

验收标准：

- 场景因果段要素缺失可定位
- cognition 有 holder/audience
- relation/goal 进入结构化 register
- 连续 5 章无需人工修字段

### 2.2 `细纲转线网1/2.txt` → `storyline_network`

状态：**生成端可用，消费端未接入。**

已实现：

- 第一轮/第二轮
- 九节结构
- 八维出口
- 假设清单
- 批次工件版本
- 事件名展示，不强制章节号

未实现：

| 需求 | 现状 | 影响 |
|---|---|---|
| 线网进入 `/api/context` | 未固定接入 | 下一章生成用不到线网 |
| 线网进入 `compile_archive` | 未固定模块 | 编译档案缺线网 |
| 结构化 `kb_storyline*` | 只有文档 | 无法按线查询 |
| 线与章节记忆交叉校验 | 无 | 线网可能与事实冲突 |
| 二轮证据差异报告 | 只有假设清单 | 修正理由不可审计 |
| 马甲/并行线/耦合/爽点轮换 | 依赖 prompt | 无结构校验 |

验收标准：

- 批次线网可被下一章 context 读取
- 线网节点能关联章节记忆
- 二轮修正能给出证据差异

### 2.3 `长期档案更新.txt` → `archive_update`

状态：**能生成新版本，但不是完整维护器。**

已实现：

- 原档案 + 新章 → 新版本
- 九模块
- active 唯一
- context 读取
- 回滚时按来源章节失效

未实现：

| 需求 | 现状 | 影响 |
|---|---|---|
| 更新判定草稿 | 只有最终档案 | 无法审阅改了什么 |
| 模块级 diff | 无 | 不知道哪条被覆盖 |
| `info_type=rule/dynamic/history` | 无 | 动态状态可能叠加 |
| 单一事实源/跨栏去重 | 无 | 同一事实多模块重复 |
| POV 认知边界 | 无 | 作者/角色已知混用 |
| 时态/预设隔离 | 无 | 未发生信息可能提前写入 |
| 角色/线/地图自动流转 | 无 | 需要手工维护 |
| 模块四/五/六结构化面板 | 只有记录 | 关系网/角色面板不完整 |

验收标准：

- 每次更新有结构化 diff
- 动态状态以新换旧
- 跨模块无重复事实
- 连续 5 章档案可回滚

### 2.4 `chapter-memory.md` 运行时提示词

状态：**运行链路可用，v4 未重新基线。**

- v2 有 3 次稳定性数据
- v4 改为“不强制 cognition/unresolved”，允许空
- 模糊兼容已接入
- 还没有 v4 的 3 次重跑

---

## 3. 用户操作需求

| 需求 | 状态 | 说明 |
|---|---|---|
| 双模式作品（writing/learning） | 已有 | space 区分 |
| 实体别名 | 已有 | alias/identity |
| A/B/C 实体路径 | 部分 | 需要继续校验 |
| 章节快照/回滚 | 已有 | `rollback_after_chapter` |
| 生命周期过滤 + 1-hop recall | 已有 | snapshot/cognition |
| 账本/计时器 | 已有 | resource/item/debt/timer |
| 双轨来源 | 已有 | source/snapshot |
| 六种解析模式 | 已有 | state_update/deconstruct_extract/memory/chapter_memory/archive_update/storyline_network |
| 审计 | 已有 | structured + semantic hard rule |
| 写接口 + 单 API key | 已有 | X-API-Key |
| `kg` 只指向知识库 | 已有 | nginx 独立 |
| 两个作品分开查询 | 已修 | 多作品必须带 work |
| 一套读取源 | 已修 | `/api/story` archive-only |
| 避免重复记录 | 已修 | 去重 + 旧记录清理 |
| 分页完整 | 已修 | offset/next_offset |
| 避免无限补丁 | 部分 | 有 patch-debt 停止条件 |

---

## 4. 当前问题清单

### P0

1. **`archive_update` 没有 diff/审阅**
   - 影响：长期档案可能悄悄覆盖
   - 处理：Phase 2

2. **线网未进入 context/compile**
   - 影响：线网生成后没人用
   - 处理：Phase 3

3. **核心分类未完全统一**
   - 现状：`active_storyline/map/faction` 等模块分类仍在
   - 影响：分类过滤语义不一致
   - 处理：加 `module` 字段，核心分类固定五种

### P1

4. **神域没有章节记忆**
   - 只有 735 条回填 fact
   - 影响：context 没有上一章记忆

5. **斗罗只有 3 章**
   - 影响：无法验证连续更新

6. **POV/时态/预设未校验**
   - 影响：认知越界、未来信息泄漏

7. **offset 分页不稳定**
   - 影响：写入期间翻页可能重复/漏
   - 处理：后续 cursor/keyset

8. **prompt v4 未基线**
   - 影响：不知道模糊兼容是否降低质量

### P2

9. **真实章节 fixture 本地保留**
   - 影响：CI 只能跑合成用例

10. **warning 没有聚合看板**
    - 影响：问题不可见

11. **部署版本没有显式版本号**
    - 影响：生产/工作区漂移只能靠人工发现

---

## 5. 冲突与决策

| 决策 | 建议 | 状态 |
|---|---|---|
| 多作品是否必须带 work | 必须 | 已实施 |
| `/api/story` 是否只读档案 | 是 | 已实施 |
| 旧 graph 是否在线回退 | 否 | 已实施 |
| `archive_update` 是否人工 accept | 是 | 待确认 |
| 核心分类是否只保留五种 | 是 | 待确认 |
| 去重是否带章节号 | 是 | 已实施 |
| 真实章节是否进公开仓库 | 否 | 已实施 |

---

## 6. 完成定义

一个需求算完成，必须同时满足：

1. 有生成路径
2. 有结构校验
3. 有投影或明确“不投影”
4. 有读取路径
5. 有回滚/重放
6. 有测试或抽样证据
7. 有文档

只满足“模型能输出”不算完成。
