# 知识库的定位与硬编码治理

## 1. 知识库到底做什么

它不是写作模型，也不是通用图数据库。它是长篇创作的**记忆与一致性服务**。

### 1.1 五件事

| 能力 | 说明 |
|---|---|
| 存 | 作品、章节、实体、状态快照、场景、线程、长期档案 |
| 查 | 实体档案、场景、线程、词法检索、拆书检索 |
| 编译 | 下一章所需的九模块上下文 |
| 审 | 初稿与硬设定、账本、死亡状态、红线冲突 |
| 回滚 | 删除/编辑第 N 章，自动回到第 N-1 章结算态 |

### 1.2 两类使用方

- **连载写作流水线**：每章开始前取上下文，写完后入库、解析、审校，防止吃书。
- **拆书流水线**：把爆款章节拆成去名化骨架与文风锚点，供新书复用。

### 1.3 不做什么

- 不生成正文（生成在写作层）
- 不做排版、发布、平台对接
- 不做通用知识图谱（只为小说连续性服务）
- 不把模型输出直接当 canon（先 draft，再校验/接受）

### 1.4 成功标准

对任意章节 N，能稳定回答：

1. 谁在场、在哪、什么状态
2. 谁知道什么、误判什么
3. 哪些承诺/危机/伏笔未解决
4. 哪些硬设定不能被改写
5. 下一章需要哪些上下文

---

## 2. 硬编码审计

原则：**代码只认识结构，不认识题材名词。**

### 2.1 已修复

| 位置 | 原硬编码 | 现在 |
|---|---|---|
| `archive_extraction.py` 资源账本 | `{"铜钱","灵石","金币","银两"}` 白名单 | `state_changes.kind="resource"` + 任意资源名 |
| `archive.py` 初稿审计 | 正则只认 `铜钱|灵石` | 从 `work_profile.resource_units` 或账本现有键生成单位 |
| `archive_extraction.py` 提示词示例 | 示例固定“铜钱” | 改为 `<资源名>`，由作品配置决定 |
| `archive.py` 工作画像 | 无资源单位字段 | 新增 `resource_units`（JSON 数组） |
| 记忆提示词实体类型 | 固定 5 类 | 从 `work_profile.entity_types` 注入 |
| 记忆提示词故事分类 | 固定 10 类 | 从 `work_profile.story_categories` 注入 |
| 编译九模块 | `_MODULES` 写死标题/顺序 | 从 `work_profile.archive_modules` 读取 |
| 查询同义词 | `_QUERY_SYNONYMS` 固定玄幻词 | 从 `work_profile.glossary` 注入，按请求作用域生效 |

### 2.2 尚未治理

| 位置 | 硬编码 | 风险 | 建议 |
|---|---|---|---|
| `archive.py` `work_type` | 只允许 `writing`/`deconstruct` | 未来可能有“同人”“剧本”等 | 改成可注册的 workflow 类型 |
| `archive.py` 审计动词 | `花了/花费/花掉/支付/消耗` | 其他表达漏检 | 语言模式配置，或改用结构化 state_changes |
| `archive.py` 拆书占位符 | `[主角]/[反派A]/[配角B]` | 其他拆书模板不同 | 占位符配置 |
| `kb_server.py` 默认空间 | `space="writing"` / `branch="main"` | 多空间时需显式 | 保持默认，但客户端应显式传 |

---

## 3. 资源账本的通用化

### 3.1 数据

作品画像新增：

```json
{
  "work": "示例作品",
  "resource_units": ["灵石", "贡献点", "声望"]
}
```

不配置时，审计从账本已有键推断。

### 3.2 记忆抽取

```json
{
  "state_changes": [
    {
      "entity": "杨凌",
      "kind": "resource",
      "field": "灵石",
      "from": "10",
      "to": "7",
      "evidence": "他花了三块灵石"
    }
  ]
}
```

代码只做两件事：

1. `dynamic.resources[field] = 数值`
2. `dynamic.inventory_ledger.currency[field] = 数值`（兼容旧读取）

不再判断“这是不是货币”。

### 3.3 审计

```text
单位列表 = work_profile.resource_units
       或 账本/资源现有键
支出模式 = 动词 + 数字 + 单位
按单位分别比较余额
```

作品写“仙玉”“贡献点”“信用点”，都不需要改代码。

---

## 4. 推荐的画像字段

```json
{
  "work_type": "writing",
  "genre": "玄幻",
  "world_summary": "...",
  "protagonist_name": "杨凌",
  "resource_units": ["灵石", "贡献点"],
  "entity_types": ["character", "location", "item", "organization", "concept"],
  "story_categories": ["power_and_laws", "map", "faction", "active_storyline", "foreshadow"]
}
```

`resource_units`、`entity_types`、`story_categories`、`archive_modules`、`glossary` 均已实现，`/api/work_profile` 可直接写入。

```json
{
  "glossary": [
    {"term": "灵石", "synonyms": ["仙石", "灵玉"]},
    {"term": "境界", "synonyms": ["修为", "层次"]}
  ]
}
```

`glossary` 同时做两件事：把标准词注册为分词保护词，并在检索时把同义词加入 OR 扩展。

```json
{
  "archive_modules": [
    {"title": "一、世界观", "kind": "meta"},
    {"title": "二、人物", "kind": "characters"},
    {"title": "三、伏笔", "kind": "records", "category": "foreshadow"}
  ]
}
```

`kind` 支持 `meta` / `records` / `characters` / `protagonist`。

---

## 5. 验收

- `tests/test_archive.py::test_audit_uses_configured_resource_units` 通过
- `tests/test_archive.py::test_ledger_number_parses_chinese_and_digits` 通过
- 通用资源名（灵石/贡献点）不再依赖代码白名单

---

## 6. 结论

- 知识库的职责是“记住并守住设定”，不是“写得好”。
- 代码不应认识“铜钱”“灵石”，只应认识“资源名、数值、单位”。
- 已把资源账本从白名单改成作品配置；其余题材词也应逐步移到配置/画像。
