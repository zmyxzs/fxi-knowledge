# 发布语义：看指针，不看 status

`knowledge_item.status` 和 `knowledge_version.status` 都是**生命周期**字段，不是「是否发布」。
解析完成后条目仍然是 `draft` 是预期行为，不代表没有对外可见。

## 三个字段

| 字段 | 含义 |
|---|---|
| `knowledge_item.status` | 生命周期：`draft`（默认）、`deleted`、`rejected`、`superseded`；不表示是否发布 |
| `knowledge_item.published_graph_version_id` | 当前对外发布的版本指针；`NULL` 表示未发布 |
| `knowledge_version.status` | 版本自身的生命周期；解析产生的版本同样是 `draft` |

`knowledge_version` 是不可变版本；发布只是把 `knowledge_item.published_graph_version_id`
指向其中一个版本。

## 判定「已发布」

查询与投影的口径是：

```sql
SELECT COUNT(*)
FROM knowledge_item AS item
JOIN knowledge_version AS version
  ON version.id = item.published_graph_version_id
 AND version.item_id = item.id
WHERE item.status NOT IN ('deleted', 'rejected', 'superseded');
```

也就是 `published_graph_version_id = version.id`，且 item 没有被删除、拒绝或取代。

## 谁写指针

- 解析候选（`origin_parse_receipt_id IS NOT NULL`）：解析发布 owner 在
  check / resolution / publish 门禁通过后写指针；
- 手工创建的候选（`origin_parse_receipt_id IS NULL`）：追加版本时直接写指针
  （`repository_graph_candidate_writes.py`）；
- 删除、拒绝、取代：指针被清空（`published_graph_version_id = NULL`）。

## 验证数据（2026-10-24，3 章）

- `knowledge_item`：155 条，`status` 全部是 `draft`
- `published_graph_version_id`：155 条全部非空，且都指向同一 item 的版本
- `knowledge_graph_projection` / `knowledge_graph_fts`：各 155 行

也就是说，「155 条 draft」和「155 条已发布」同时成立，并不矛盾。

## 常见误读

- 「status 还是 draft，说明没发布」——错，看指针。
- 「`list_knowledge_items` 返回 draft 就是草稿」——它返回的是生命周期状态；
  检索和枚举会按指针与 status 排除规则过滤。
- 「`knowledge_version.status` 是 published 才可见」——版本状态与发布无关，
  可见性由 item 的指针决定。
