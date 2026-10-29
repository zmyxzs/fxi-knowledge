# 真实调用示例

服务地址：

| 地址 | 内容 |
|---|---|
| https://kg.zmteams.site/ | 知识库搜索页 |
| https://kg.zmteams.site/docs | API 速查 |
| https://kg.zmteams.site/swagger | Swagger UI（可直接调试） |
| https://kg.zmteams.site/api/openapi.json | OpenAPI JSON |
| https://xs.zmteams.site/ | StoryForge 写作工作台（独立服务） |

写接口需要 `X-API-Key`；读接口公开。

## 一次完整生命周期（curl）

```bash
BASE=https://kg.zmteams.site
KEY=你的密钥

# 1. 健康检查
curl -s $BASE/health

# 2. 创建作品
curl -s -X POST $BASE/api/works \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"title":"真实调用演示"}'

# 3. 上传章节
curl -s -X POST $BASE/api/sources \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"work":"真实调用演示","title":"001 山门","content":"杨凌站在青云山门前……"}'

# 4. 启动解析（异步）
curl -s -X POST $BASE/api/parse \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"work":"真实调用演示"}'

# 5. 轮询进度
curl -s $BASE/api/parse/<job_id>

# 6. 检索
curl -s "$BASE/api/search?q=杨凌&work=真实调用演示&top_k=3"

# 7. 实体 dossier
curl -s "$BASE/api/entity?name=杨凌&work=真实调用演示"

# 8. 一致性检查（带 LLM 复核）
curl -s "$BASE/api/audit?work=真实调用演示&llm=1"

# 9. 结构化记录
curl -s "$BASE/api/story?work=真实调用演示&category=attribute&page_size=3"

# 10. 删除作品
curl -s -X DELETE "$BASE/api/works/<work_id>" -H "X-API-Key: $KEY"
```

## 2026-10-29 实测输出

```text
1) create work: 4ac436f6...   （作品 id）
2) upload source: 62fb534c... chunks=1
3) parse job: 36db85be...
   status=running   check 1 running
   status=running   check completed, resolution completed, publish running
   status=completed

4) search hits=1
   [0.0164] 第001章 山门 杨凌站在青云山门前……
   cards=令牌 · 持有人 · 杨凌, 杨凌 · 力量 · 99点

5) entity total=5
   counts={"prop":1,"character":1,"temporal_state":1,"attribute":2}

6) audit records=8 conflicts=0 review=0

7) story attribute total=2

8) deleted: 4ac436f6...
```

## Python 客户端

```python
from examples.kb_client import KnowledgeClient

client = KnowledgeClient(api_key="...")
print(client.health())
client.create_work("新作品")
client.upload_source("新作品", "001 第一章", "正文……")
job = client.parse("新作品")
while True:
    status = client.job(job["id"])
    print(status["status"], status.get("progress"))
    if status["status"] in {"completed", "partial", "failed"}:
        break
    time.sleep(3)
print(client.search("杨凌", work="新作品"))
print(client.audit("新作品", llm=True))
```

## 解析任务状态

```json
{
  "id": "36db85be...",
  "status": "running",
  "batch_id": "…",
  "progress": {
    "status": "running",
    "stages": {
      "check": {"running": 1},
      "resolution": {"queued": 1},
      "publish": {"queued": 1}
    },
    "sources": [
      {"source_id": "…", "extraction": "completed",
       "stages": {"check": "running", "resolution": "queued", "publish": "queued"}}
    ]
  }
}
```

终态：`completed` / `partial` / `failed`；服务重启会把未完成任务标为 `interrupted`。

## 常用查询参数

| 参数 | 说明 |
|---|---|
| `work` | 作品标题或 id；省略时用第一个作品 |
| `top_k` | 检索条数，1–20 |
| `category` | `character` / `event` / `relationship` / `attribute` / `temporal_state` 等 |
| `llm=1` | `/api/audit` 额外做 LLM 复核 |
| `source_ids` | `/api/parse` 只解析指定章节 |
