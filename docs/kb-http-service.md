# 知识库 HTTP 服务（kg.zmteams.site）

HTTP 服务：读接口公开，写接口需要一个 API key。检索、故事查询、实体 dossier 之外，也支持创建作品、上传章节和编辑章节。

## 端点

### 读

| 路径 | 说明 |
|---|---|
| `/` | 极简搜索页（输入关键词，展示命中、卡片和来源） |
| `/health` | 健康检查，返回作品列表 |
| `/api/works` | 作品列表 |
| `/api/sources?work=神域囚徒` | 章节列表 |
| `/api/search?q=杨凌&top_k=5&work=神域囚徒` | 词法检索 + 卡片 |
| `/api/story?work=神域囚徒&category=relationship&page_size=20` | 结构化故事记录 |
| `/api/entity?name=杨凌&work=神域囚徒` | 实体 dossier（身份组、计数、关系、事件） |
| `/api/audit?work=神域囚徒` | 设定一致性检查（同章冲突、跨章变化、待复核、类别/悬空引用） |
| `/api/audit?work=神域囚徒&llm=1` | 额外用 LLM 复核 `needs_review`（需要 LLM 环境变量） |

### 写（需要 `X-API-Key`）

| 方法 | 路径 | Body |
|---|---|---|
| POST | `/api/works` | `{"title":"作品名","description":"可选"}` |
| PUT | `/api/works/{id}` | `{"title":"新标题","description":"..."}` |
| DELETE | `/api/works/{id}` | — |
| POST | `/api/sources` | `{"work":"作品名","title":"001 第一章","content":"正文"}` |
| PUT | `/api/sources/{id}` | `{"title":"新标题","content":"新正文"}` |
| DELETE | `/api/sources/{id}` | — |
| POST | `/api/parse` | `{"work":"作品名","source_ids":["..."]}`（省略则全部来源）→ 202 任务 |
| GET | `/api/parse/{job_id}` | 任务状态与阶段摘要 |

上传章节只做导入和分块；解析通过 `/api/parse` 异步执行，使用 `FXI_KB_LLM_*` 配置的 OpenAI 兼容端点。单章通常 30–60 秒。

任务状态写入数据库同目录的 `parse_jobs.json`（保留最近 100 条）；服务重启时，未完成的 `queued`/`running` 任务会标为 `interrupted`。并发由 `FXI_KB_PARSE_CONCURRENCY` 控制（默认 1，最大 4）。

`GET /api/parse/{job_id}` 的 `progress` 字段每 3 秒刷新，包含各阶段计数和每个来源的 `extraction` / `check` / `resolution` / `publish` 状态，可直接做进度条。

## 鉴权

服务启动时读取环境变量 `FXI_KB_API_KEY`：

- 未设置：写接口返回 503，读接口正常
- 已设置：写接口要求请求头 `X-API-Key` 与它一致，否则 401

服务器上的 key 放在 `/home/ubuntu/fxi-bench/.env`（权限 600），systemd 通过 `EnvironmentFile` 注入。

LLM 复核（`/api/audit?llm=1`）另需三个变量，指向任意 OpenAI 兼容端点：

```ini
FXI_KB_LLM_BASE_URL=https://wd.zmteams.site/v1
FXI_KB_LLM_API_KEY=sk-...
FXI_KB_LLM_MODEL=global:deepseek-v4.1-flash
```

未配置时 `llm=1` 返回 400；普通 `/api/audit` 不受影响。

```bash
curl -X POST https://kg.zmteams.site/api/works \
  -H "X-API-Key: $FXI_KB_API_KEY" -H "Content-Type: application/json" \
  -d '{"title":"新作品"}'
```

## 部署

服务器路径：`/home/ubuntu/fxi-bench`

```bash
# 数据库
/home/ubuntu/fxi-bench/data/bench-db.sqlite3

# 启动
FXI_KB_API_KEY=... /home/ubuntu/fxi-bench/.venv/bin/python scripts/kb_server.py \
  --database /home/ubuntu/fxi-bench/data/bench-db.sqlite3 \
  --host 127.0.0.1 --port 8766
```

systemd 单元 `/etc/systemd/system/fxi-kb.service`：

```ini
[Unit]
Description=fxi knowledge base HTTP service
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/fxi-bench
EnvironmentFile=/home/ubuntu/fxi-bench/.env
ExecStart=/home/ubuntu/fxi-bench/.venv/bin/python scripts/kb_server.py --database /home/ubuntu/fxi-bench/data/bench-db.sqlite3 --host 127.0.0.1 --port 8766
Restart=on-failure
MemoryMax=600M

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now fxi-kb
sudo systemctl status fxi-kb
```

## Nginx

`/etc/nginx/sites-enabled/kg.zmteams.site`：

- 80 → 301 HTTPS
- 443 → `proxy_pass http://127.0.0.1:8766`
- 证书：`/etc/letsencrypt/live/kg.zmteams.site/`（2027-01-27 到期，cron 自动续期）

其他域名（`xs`、`ai`、`deai`、`wd`）保持原样。

## 验证

```bash
curl -s https://kg.zmteams.site/health
curl -s 'https://kg.zmteams.site/api/search?q=杨凌&top_k=3'
curl -s 'https://kg.zmteams.site/api/entity?name=杨凌'
curl -s 'https://xs.zmteams.site/' | head -c 100   # 应仍是 StoryForge
```

2026-10-29 实测：`/health` 返回 `神域囚徒`，首页标题「知识库」，`/api/entity?name=杨凌` 返回 209 条记录；`xs.zmteams.site` 仍是 StoryForge。
