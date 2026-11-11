# 跨书抽样结果（6 本 × 2 章）

## 样本

从 `D:\文本\素材` 中选 6 本有 `raw` 章节的书，每本取第 1 章与中间章节：

| 书 | 章节 |
|---|---|
| 十日终焉 | ch001 / ch320 |
| 大王饶命 | ch001 / ch335 |
| 我在精神病院学斩神 | ch001 / ch1829 |
| 网游之天谴修罗 | 0001 / 0427 |
| 超神机械师 | ch001 / ch268 |
| 逆天邪神 | 0001 / 1022 |

## 结果

模型：`global:deepseek-v4.1-flash`，prompt v4（模糊兼容）。

| 指标 | 结果 |
|---|---|
| books | 6 |
| chapters | 12 |
| valid | 12 |
| hard_failures | 0 |
| projection_failures | 0 |
| warnings_total | 1 |

唯一 warning：

```text
逆天邪神 ch1022
scenes[1] 的 evidence 未逐字出现：我从不会让自己的女人受委屈
```

处理方式：文档仍为 active，投影成功，warning 记录在 `warnings_json`。

## 结论

- 模糊兼容层在 6 本不同题材上都能入库、投影
- 没有出现“换个题材就必须加字段”的情况
- evidence 改写仍是唯一 warning，但不再阻断
- 不建议按书加规则

## 限制

- 每本只取 2 章，覆盖不了全书
- 没有做人工语义评分
- 只跑了一次，没有分布
- 没测超长章节、系统面板密集章节、多视角章节

## 复现

```bash
PYTHONPATH=src python scripts/sample_materials.py \
  --root "D:\文本\素材" --per-book 2 \
  --base-url ... --api-key ... --model ... \
  --report docs/material-sampling.json
```

## 后续用法

- 每轮只抽样，不跑全书
- 同一 warning 在 ≥2 本书重复出现，才考虑改通用层
- 不在单本书上连续调关键词
