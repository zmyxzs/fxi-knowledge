# 用 AI Studio 做评审，而不是运行环境

状态：2026-11-11

## 1. 为什么导入后跑不起来

AI Studio 的 GitHub 导入/Build 运行时是 **Node/前端应用**：

```text
package.json
npm install
npm run build
启动前端
```

本项目是 Python：

```text
pyproject.toml
src/fxi_novel
scripts/kb_server.py
SQLite
```

所以会出现：

```text
请检查您的 package.json
依赖安装失败
运行入口错误
```

这不是代码 bug，是运行时类型不匹配。

补 `package.json` 不能解决，因为：

- 没有 Node 启动入口
- Python 依赖不会按 `requirements.txt` 安装
- `fxi_novel` 不会自动进入 `PYTHONPATH`
- SQLite/本地文件路径也不在 AI Studio 运行时

## 2. AI Studio 适合做什么

适合：

- 架构评审
- 找设计冲突
- 找测试缺口
- 检查兼容性
- 审阅 prompt 契约
- 把历史问题归纳成规则

不适合：

- 执行 pytest
- 启动 HTTP 服务
- 连接 SQLite
- 调本地脚本
- 复现真实模型调用

结论：

```text
AI Studio = 评审者
本地/服务器 = 执行者
```

## 3. 推荐工作流

### 第一步：生成评审包

不要一次把 657 个文件全塞进去。按子系统打包：

```text
评审包 A：架构与需求
  docs/architecture-contract.md
  docs/requirements-status.md
  docs/spec-coverage.md
  docs/patch-debt.md
  docs/history-problems-solutions.md

评审包 B：章节记忆
  prompts/learning/chapter-memory.md
  src/fxi_novel/knowledge/archive_extraction.py（相关函数）
  tests/test_archive.py（章节记忆部分）

评审包 C：服务与查询
  scripts/kb_server.py（路由与 story/context）
  tests/test_kb_server_scope.py
  docs/knowledge-base-guide.md

评审包 D：兼容与迁移
  docs/compatibility-matrix.md
  scripts/backfill_graph_story.py
  scripts/dedupe_story_records.py
```

每个包控制在模型上下文可读范围，不要把整个仓库复制进去。

### 第二步：一次只评审一个维度

不要问：

```text
帮我看看这个项目有什么问题
```

要问：

```text
只评审“章节记忆投影到 kb_story_record”这一条链路：
1. 数据会不会重复
2. 分类会不会漂移
3. 投影失败会不会污染读取
4. 旧记录迁移会不会丢数据
5. 哪些测试缺失
输出：问题、证据、建议、风险等级。
```

### 第三步：要求证据

要求 AI Studio：

- 引用文件名和函数名
- 指出具体字段/分支
- 区分“确定问题”和“猜测”
- 不要直接给大补丁

### 第四步：本地验证

AI Studio 的建议只是假设。必须：

1. 本地跑测试
2. 用真实数据抽样
3. 跨书复现
4. 再决定是否改

### 第五步：记录结论

建议维护：

```text
docs/ai-review-log.md
```

记录：

- 评审日期
- 评审包
- 采纳建议
- 拒绝建议及原因
- 后续验证结果

## 4. 评审 Prompt 模板

### 4.1 架构评审

```text
你是知识库架构评审员。只基于我给的文档，不要假设仓库里还有别的实现。

目标：找出会导致“两套真源、重复记录、兼容性破坏”的设计问题。

输入：
- 架构契约
- 需求状态
- 历史问题
- 当前数据流

请输出：
1. 违反不变量的点
2. 可能复发的历史问题
3. 兼容性破坏
4. 缺失的测试
5. 不建议做的补丁

每条给出：文件/章节、证据、影响、建议。
```

### 4.2 数据链路评审

```text
只评审 chapter_memory → kb_story_record → /api/story 这条链路。

检查：
- 幂等键是否覆盖章节
- 旧 memory 记录是否会重复
- 分类是否稳定
- 投影失败是否会被读取
- 回滚是否覆盖
- 分页是否完整

输出：问题清单 + 证据 + 测试建议。
```

### 4.3 Prompt 评审

```text
只评审 chapter-memory.md 和 archive-update.md。

检查：
- 是否一次要求太多
- 是否有硬规则会逼模型编造
- 是否缺少 POV/时态约束
- 输出是否可被代码归一
- 是否适合中等能力模型

输出：保留、删除、拆分建议。
```

### 4.4 兼容性评审

```text
只评审兼容性矩阵。

检查：
- 老库升级
- 老工件读取
- 老客户端不带 work
- graph-only 作品
- prompt 版本漂移
- AI Studio/Node 与 Python 运行时

输出：破坏点、迁移步骤、回滚方案。
```

## 5. AI Studio 的局限

| 局限 | 影响 | 对策 |
|---|---|---|
| 不能执行代码 | 无法验证 | 本地 pytest + 真实抽样 |
| 看不到数据库 | 不知道真实数据分布 | 提供计数、样本、warning 统计 |
| 上下文有限 | 大仓库读不全 | 按子系统打包 |
| 会幻觉 API | 建议可能不存在 | 要求引用文件/函数 |
| 倾向给大改 | 容易过度设计 | 限制“只评审一个链路” |
| 不知道生产状态 | 可能建议已做过的事 | 提供当前状态和已完成项 |

## 6. 建议的评审节奏

```text
每周一次：
  1 个架构问题
  1 个数据链路
  1 个 prompt
  1 个兼容性点

每次评审后：
  本地验证
  跨书抽样
  记录采纳/拒绝
  只改通用层
```

## 7. 如果确实要在 AI Studio 里运行

只有两条路：

### A. 前端 + 远程 Python 服务

- Python 留在 `kg.zmteams.site`
- AI Studio 只放 React/Node 前端
- 前端调用只读 API
- 写接口走服务端代理

### B. 把 Python 服务搬到 Cloud Run

- Cloud Run 跑 `scripts/kb_server.py`
- AI Studio 前端调用 Cloud Run
- 需要处理 SQLite 持久化/对象存储

不建议把 Python 包硬塞进 AI Studio Build。

## 8. 结论

AI Studio 对这个项目最大的价值不是“运行”，而是：

```text
用它的长上下文和推理能力做代码/架构评审
```

执行仍然在：

```text
本地 pytest
生产服务器
真实材料抽样
```

评审结果必须经过：

```text
证据 → 本地验证 → 跨书复现 → 通用层修改
```

不能直接采纳为补丁。
