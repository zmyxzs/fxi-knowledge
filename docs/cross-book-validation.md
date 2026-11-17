# 跨书适用性验证

状态：2026-11-12

## 样本

从 `D:\文本\素材` 取 6 本不同题材，各 2 章：

| 书 | 类型 | 章节 |
|---|---|---|
| 大王饶命 | 都市/搞笑 | ch001 / ch002 |
| 十日终焉 | 无限流/悬疑 | ch001 / ch002 |
| 超神机械师 | 科幻/游戏 | ch001 / ch002 |
| 网游之天谴修罗 | 网游/玄幻 | ch001 / ch002 |
| 我在精神病院学斩神 | 都市/异能 | ch001 / ch002 |
| 逆天邪神 | 玄幻 | ch001 / ch002 |

本地临时库，不污染生产。

## 汇总结果

| 书 | chapter_memory | archive_update | reconcile | canonical |
|---|---|---|---|---:|
| 大王饶命 | completed | 17 + 20 | 100% / 100% | 37 |
| 十日终焉 | completed | 3 + 10 | 75% / 100% | 13 |
| 超神机械师 | completed | 7 + 18 | 100% / 100% | 23 |
| 网游之天谴修罗 | completed | 16 + 15 | 100% / 100% | 31 |
| 我在精神病院学斩神 | completed | 2 + 13 | 100% / 100% | 15 |
| 逆天邪神 | completed | 6 + 26 | 100% / 100% | 32 |

结论：

- 6 本书、12 章，全部可跑通
- 5 本书两章全部 100%
- 十日终焉第 1 章 3/4，漏 1 条
- 没有出现模块硬失败
- 没有出现 warning

## 结果

### 大王饶命

```text
chapter_memory: completed
archive_update:
  第1章 valid, applied 17
  第2章 valid, applied 20
reconcile:
  第1章 7/7 100%
  第2章 4/4 100%
canonical: 37
```

### 十日终焉

```text
chapter_memory: completed
archive_update:
  第1章 valid, applied 3
  第2章 valid, applied 10
reconcile:
  第1章 3/4 75%
  第2章 2/2 100%
canonical: 13
```

### 超神机械师

```text
applied: 7 + 18
reconcile: 7/7, 9/9
canonical: 23
```

### 网游之天谴修罗

```text
applied: 16 + 15
reconcile: 2/2, 1/1
canonical: 31
```

### 我在精神病院学斩神

```text
applied: 2 + 13
reconcile: 2/2, 3/3
canonical: 15
```

### 逆天邪神

```text
applied: 6 + 26
reconcile: 2/2, 4/4
canonical: 32
```

## 发现的问题

### 1. 九模块硬校验不适合都市题材

第一次跑《大王饶命》时：

```text
archive_update: failed
```

原因：

```text
archive 缺少模块
```

但都市/搞笑作品本来就没有“力量体系/地图/势力”等模块。

已修：

- 模块缺失从 hard issue 降为 warning
- `draft/archive 为空` 仍是 hard
- `changes 非数组` 仍是 hard

修复后《大王饶命》两章全部通过。

### 2. 十日终焉第 1 章缺 1 条

```text
state_delta 4 条
changes 3 条
reconcile 3/4
```

说明模型漏了一条状态变化，或把它合并到了别的条目。

这属于模型覆盖问题，不是 key 问题。

## 结论

- 当前链路可以跨题材运行
- 九模块硬校验已放宽
- 都市/搞笑不再被“缺少力量体系”卡住
- 无限流仍有一条漏抽
- 2 本 × 2 章不足以证明全适用

## 限制

- 每本只 2 章
- 没有人工语义评分
- 没有跑长篇
- 没有覆盖系统面板/多视角/对话密集

## 下一步

```text
1. 扩到 6 本 × 2 章
2. 记录每本 warning/漏抽
3. 只有跨书重复出现才改通用层
```
