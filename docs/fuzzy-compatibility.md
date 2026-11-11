# 模糊兼容策略

目标不是把每一种模型输出都改成固定格式，而是让知识库**能接收、能降级、能重放**。

## 两层校验

### Hard（阻止入库）

只保留真正无法使用的情况：

- 输出不是对象
- `scenes` 为空
- JSON 无法解析

### Soft（入库但记 warning）

其余都降级为 warning：

- 场景数不在 3–8
- `causal_paragraph` 过短
- evidence 缺失、标点差异、逐字不匹配
- cognition type / state category / thread transition 非标准
- 缺 `holder`、`field`、`quantity`、`entity`
- 章节号缺失或不一致

warning 存在 `kb_document.warnings_json`，不阻止文档 active，也不阻止投影。

## 别名兼容

模型返回不同命名时自动归一：

| 标准 | 兼容 |
|---|---|
| `scenes` | `scene_list` / `events` / `场景` |
| `cognition` | `cognitions` / `knowledge` / `认知` |
| `state_deltas` | `state_changes` / `deltas` / `changes` / `状态变化` |
| `unresolved` | `threads` / `open_threads` / `未决` |
| `name` | `title` / `scene` |
| `causal_paragraph` | `causal` / `summary` / `description` |
| `judgment` | `判断` / `judgement` |
| `item` | `道具` / `物品` |
| `new` | `新` / `新增` / `开始` |

## 为什么不再强制

之前的规则要求：

- 有怀疑就必须有 cognition
- 有异常就必须有 unresolved

这会导致模型为了满足规则而虚构。现在改成：

- 有明确内容时优先写
- 没有明确内容可空
- 不得为了凑数虚构

## 适用性

- 短章节允许少于 3 个场景
- 没有状态变化允许空 `state_deltas`
- 没有跨章线程允许空 `unresolved`
- 不同题材不要求固定模块齐全
- 旧版本文档仍可读取，缺失字段按默认值处理

## 兼容边界

- 投影仍要求至少一个 scene
- evidence 不匹配不会阻止投影，但会记录 warning
- 严重结构错误仍进 `draft`，等待人工审核或重放
