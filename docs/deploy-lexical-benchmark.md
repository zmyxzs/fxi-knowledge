# 在 2C/2G 服务器上跑纯词法基准

目标：不装模型、不建向量索引，只验证「SQLite FTS + 卡片 + dossier」在服务器上的实际延迟与内存占用。

## 0. 前置检查

```bash
ssh -i ~/.ssh/codex_deploy_ai_proxy ubuntu@43.155.187.124
free -h
nproc
python3 --version   # 需要 3.11+
```

服务器现状（2026-10-29）：2 vCPU、1.9 GB 内存、约 770 MB 可用、swap 已用约 845 MB，已运行 `storyforge` 等常驻服务。

## 1. 上传代码与数据库

在 Windows 本机：

```powershell
$key = "$env:USERPROFILE\.ssh\codex_deploy_ai_proxy"
ssh -i $key ubuntu@43.155.187.124 "mkdir -p /opt/fxi-bench/data"

# 只传运行所需内容
scp -i $key -r src scripts prompts pyproject.toml ubuntu@43.155.187.124:/opt/fxi-bench/

# 数据库先压缩再传（约 150 MB -> 数十 MB）
sqlite3 data/verify-2026-10-25-ds7.sqlite3 "VACUUM;"
Compress-Archive data/verify-2026-10-25-ds7.sqlite3 data/db.zip -Force
scp -i $key data/db.zip ubuntu@43.155.187.124:/opt/fxi-bench/data/
```

服务器上解压：

```bash
cd /opt/fxi-bench/data && unzip -o db.zip
```

## 2. 建独立 venv（不动 storyforge）

```bash
cd /opt/fxi-bench
python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -e .
```

`pyproject.toml` 已声明 jieba 等依赖；不需要 torch / embedding 相关包。

## 3. 重建 FTS（已有库升级一次）

```bash
cd /opt/fxi-bench
.venv/bin/python scripts/rebuild_entity_fts.py data/verify-2026-10-25-ds7.sqlite3 --graph
```

预期输出：`sources=10 chunks=43 cards=734`（或相近数字），耗时约 5 秒。

## 4. 跑基准

```bash
cd /opt/fxi-bench
/usr/bin/time -v .venv/bin/python scripts/benchmark_lexical.py \
  data/verify-2026-10-25-ds7.sqlite3 \
  --work 神域囚徒 \
  --output data/benchmark-server.json
```

关注：

- `Maximum resident set size`：预期 250–400 MB
- 每条查询的 `ms`：冷启动首条 5s 左右，热查询 1–2s
- `dossier.total`：应为 209

## 5. 避免影响 storyforge

服务器内存偏紧，建议限制内存并降低优先级：

```bash
systemd-run --scope -p MemoryMax=700M -p CPUQuota=150% \
  nice -n 10 .venv/bin/python scripts/benchmark_lexical.py ...
```

跑之前和跑之后各看一次 `free -h`，确认没有把 swap 打满。

## 6. 结果回收

```powershell
scp -i $key ubuntu@43.155.187.124:/opt/fxi-bench/data/benchmark-server.json data/
```

把 `benchmark-server.json` 与本地 `data/benchmark-2026-10-29.json` 对比：

| 指标 | 本机 | 服务器预期 |
|---|---|---|
| 热查询 | 0.4–0.8s | 1–2s |
| 冷启动首条 | 4.9s | 5–8s |
| RSS | ~300MB | 250–400MB |
| dossier 杨凌 | 209 | 209 |

## 7. 清理

```bash
rm -f ~/fxi-bench/data/bench-db.zip
# 不再需要时
rm -rf ~/fxi-bench
```

## 实测结果（2026-10-29）

服务器：2 vCPU / 1.9 GB，Python 3.12.3，独立 venv，`nice -n 10`，数据库 10 章（ds7）。

| 查询 | 命中 | 耗时 | 首条 |
|---|---:|---:|---|
| 杨凌 | 5 | 1739 ms（冷启动） | 杨凌 · 力量 · 149 |
| 风神金刚腿 | 5 | 127 ms | 熟练度暴涨原文 |
| 大品青龙劲 | 5 | 137 ms | 大品青龙劲 |
| 提升身体数值的功法 | 5 | 441 ms | **杨凌力量敏捷提升** 卡片 |
| 杨凌 等级 | 5 | 751 ms | 原文段 |

- dossier 杨凌：209 条、1 个身份组
- 最大 RSS：**181 MB**
- 总耗时：**9.93 秒**（5 条查询 + 6 类计数 + dossier）
- 无 swap，未影响 storyforge

热查询实际比本机还快（0.13–0.15s），冷启动 1.7s；同义词扩展后「提升身体数值的功法」从泛化对话变为卡片命中。

## 备注

- 不需要 GPU、不需要 Ollama、不需要 embedding 服务
- 如果服务器 Python 低于 3.11，用 `uv python install 3.11` 或 deadsnakes PPA
- 数据库只读查询，跑基准不会修改业务库；重建 FTS 会写库，建议用副本
- `/opt` 需要 root，普通用户用 `~/fxi-bench` 即可
