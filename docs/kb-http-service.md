# 知识库 HTTP 服务（kg.zmteams.site）

只读 HTTP 服务，把 `fxi-knowledge` 的检索、故事查询和实体 dossier 暴露给浏览器。

## 端点

| 路径 | 说明 |
|---|---|
| `/` | 极简搜索页（输入关键词，展示命中、卡片和来源） |
| `/health` | 健康检查，返回作品列表 |
| `/api/works` | 作品列表 |
| `/api/search?q=杨凌&top_k=5&work=神域囚徒` | 词法检索 + 卡片 |
| `/api/story?work=神域囚徒&category=relationship&page_size=20` | 结构化故事记录 |
| `/api/entity?name=杨凌&work=神域囚徒` | 实体 dossier（身份组、计数、关系、事件） |

服务是只读的：不写库、不触发解析、不重建索引。

## 部署

服务器路径：`/home/ubuntu/fxi-bench`

```bash
# 数据库
/home/ubuntu/fxi-bench/data/bench-db.sqlite3

# 启动
/home/ubuntu/fxi-bench/.venv/bin/python scripts/kb_server.py \
  --database /home/ubuntu/fxi-bench/data/bench-db.sqlite3 \
  --host 127.0.0.1 --port 8766
```

systemd 单元 `/etc/systemd/system/fxi-kb.service`：

```ini
[Unit]
Description=fxi knowledge base read-only HTTP service
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/fxi-bench
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
