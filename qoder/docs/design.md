# 设计方案（由 Qoder 生成，2026-10-31）

> 本文件是 Qoder 在「设计阶段」输出的原始方案，原样保留，未做改写。
> 实现阶段以此为准，并按评审意见做了 5 处修正（见 README「已知限制」）。

---

## 设计方案：Obsidian 笔记库带出处检索助手

### 1. 目录结构与模块划分

```
qoder-lab/
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI 入口，挂载路由
│   │   ├── config.py            # 笔记库路径、seekdb 路径、功能开关
│   │   ├── db.py                # seekdb 连接池 + schema 初始化
│   │   ├── ingest/
│   │   │   ├── parser.py        # Markdown + YAML frontmatter 解析
│   │   │   ├── chunker.py       # 按 H2 切块，记录行号
│   │   │   └── pipeline.py      # 编排：扫描→解析→切块→入库
│   │   ├── search/
│   │   │   ├── keyword.py       # ngram 全文检索
│   │   │   ├── filter_builder.py # 结构化过滤 WHERE 构造
│   │   │   └── merger.py        # 关键词 + 过滤叠加 + 排序
│   │   ├── routes/
│   │   │   ├── search_api.py    # /api/search
│   │   │   └── ingest_api.py    # /api/ingest (触发重建)
│   │   └── optional/
│   │       ├── embedding.py     # 可选：本地 embedding
│   │       └── llm_rewrite.py   # 可选：LLM 查询改写
│   ├── requirements.txt
│   └── tests/
│       ├── test_parser.py
│       ├── test_chunker.py
│       └── test_search.py
├── frontend/
│   ├── index.html               # 单页应用
│   ├── app.js                   # 搜索交互 + 结果渲染
│   └── style.css
└── data/
    └── seekdb/                  # 嵌入式数据库文件
```

**模块职责：**
- **ingest/parser.py**：读取单个 .md 文件，分离 frontmatter 和正文，提取 title/date/tags/status/dir_type
- **ingest/chunker.py**：按 `^## ` 正则切分正文为 H2 块，记录每块的起止行号
- **ingest/pipeline.py**：遍历笔记库根目录，对每个文件调用 parser→chunker，写入 seekdb；支持增量（按 file_hash 跳过未变更文件）
- **search/keyword.py**：封装 MATCH AGAINST 查询，使用 ngram 分词
- **search/filter_builder.py**：根据 dir_type/tags/year 构造 WHERE 子句
- **search/merger.py**：合并关键词得分与过滤条件，返回排序后的结果
- **optional/**：embedding 和 LLM 作为可选增强，默认关闭不影响核心功能

---

### 2. 数据表设计

**表 1：`notes`（文件级元数据）**

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BIGINT AUTO_INCREMENT | 主键 |
| file_path | VARCHAR(500) | 笔记文件绝对路径 |
| file_hash | VARCHAR(64) | SHA-256，用于增量更新判断 |
| dir_type | VARCHAR(50) | 所属顶层目录类型（如 "12-原子卡片"） |
| title | VARCHAR(200) | frontmatter 中的 title |
| date | DATE | frontmatter 中的 date |
| year | INT | 从 date 提取的年份，便于按年过滤 |
| tags | VARCHAR(500) | frontmatter 中的 tags，逗号拼接 |
| status | VARCHAR(50) | frontmatter 中的 status |
| created_at | TIMESTAMP | 入库时间 |
| updated_at | TIMESTAMP | 更新时间 |

索引：
- PRIMARY KEY (id)
- UNIQUE KEY uk_file_path (file_path)
- KEY idx_dir_type (dir_type)
- KEY idx_year (year)
- KEY idx_tags (tags(100))

**表 2：`note_chunks`（H2 级别的块）**

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BIGINT AUTO_INCREMENT | 主键 |
| note_id | BIGINT | 外键关联 notes.id |
| chunk_index | INT | 块在文件内的顺序（0, 1, 2...） |
| heading | VARCHAR(200) | 该块的 H2 标题文本 |
| line_start | INT | 块起始行号（含 H2 行） |
| line_end | INT | 块结束行号 |
| content | LONGTEXT | 块的正文内容 |
| dir_type | VARCHAR(50) | 冗余自 notes，便于独立过滤 |
| year | INT | 冗余自 notes |
| tags | VARCHAR(500) | 冗余自 notes |

索引：
- PRIMARY KEY (id)
- KEY idx_note_id (note_id)
- **FULLTEXT KEY ft_content (content) WITH PARSER ngram**
- **FULLTEXT KEY ft_heading (heading) WITH PARSER ngram**
- KEY idx_dir_type (dir_type)
- KEY idx_year (year)

**为什么必须用 ngram：**
默认分词器按空格/标点切词，对中文无效——"重新排序"会被当作一个完整 token，而笔记里写的是"重排"，两者 token 不同，MATCH AGAINST 返回 0 命中。ngram 分词器将文本切为重叠的 n-gram（默认 bigram），"重排"会被切为"重排"本身，而包含"重排"的更长文本也会被切出"重排"这个 ngram，从而实现字符级的部分匹配，解决中文同义不同词搜不到的问题。

---

### 3. Ingest 流程

**步骤 1：扫描文件**
- 遍历 `D:/新建文件夹/Obsidian/吉米` 下所有 `.md` 文件
- 对每个文件计算 SHA-256，与 notes.file_hash 比对；若一致则跳过（增量更新）

**步骤 2：解析 frontmatter**
- 读取文件内容，用正则 `^---\n(.*?)\n---` 提取 YAML 块
- 使用 PyYAML 解析：
  - `title` → 直接取值
  - `date` → 解析为 datetime.date 对象，提取 year
  - `tags` → 若为列表则 join(',')；若为字符串则直接使用
  - `status` → 直接取值
- `dir_type` → 从文件路径推断：取相对于笔记库根目录的第一层目录名

**步骤 3：按 H2 切块**
- 将正文按行分割
- 用正则 `^## .+` 匹配 H2 标题行
- 切块逻辑：
  - chunk 0：从文件开头到第一个 H2 之前（若无 H2 则整文件为一个块）
  - chunk N：从第 N 个 H2 行开始，到下一个 H2 行之前或文件结束
- 记录行号：
  - `line_start` = 该块第一行的行号（1-based）
  - `line_end` = 该块最后一行的行号（1-based）
  - `heading` = H2 标题去掉 `## ` 前缀后的文本

**步骤 4：写入数据库**
- 先 INSERT/UPDATE notes 表（ON DUPLICATE KEY UPDATE）
- 删除该 note_id 的旧 chunks，INSERT 新的 chunks
- 每条 chunk 继承 notes 的 dir_type、year、tags

**步骤 5：验证**
- 输出统计：处理文件数、跳过文件数、总 chunk 数
- 可选：打印前 3 个 chunk 的 heading + line_start-line_end 供人工核对

---

### 4. 检索流程

**输入：**
- `query`：自然语言描述（如"重新排序"）
- `filters`（可选）：`{dir_type, tags, year_from, year_to}`

**步骤 1：构造过滤条件**
```python
where_clauses = []
params = []

if filters.dir_type:
    where_clauses.append("dir_type = %s")
    params.append(filters.dir_type)

if filters.tags:
    where_clauses.append("tags LIKE %s")
    params.append(f"%{filters.tags}%")

if filters.year_from and filters.year_to:
    where_clauses.append("year BETWEEN %s AND %s")
    params.extend([filters.year_from, filters.year_to])

filter_sql = " AND ".join(where_clauses) if where_clauses else "1=1"
```

**步骤 2：关键词检索**
```sql
SELECT id, note_id, heading, line_start, line_end, content,
       MATCH(content) AGAINST(%s IN BOOLEAN MODE) AS score
FROM note_chunks
WHERE {filter_sql}
  AND MATCH(content, heading) AGAINST(%s IN BOOLEAN MODE)
ORDER BY score DESC
LIMIT 20
```

**步骤 3：叠加逻辑**
- 若只有 query：执行步骤 2，filter_sql 为 "1=1"
- 若只有 filters：省略 MATCH 条件，按 updated_at DESC 排序
- 若两者都有：WHERE 中同时包含 filter 条件和 MATCH 条件，按 relevance score 排序

**步骤 4：结果组装**
每条结果返回：
```json
{
  "file_path": "D:/新建文件夹/Obsidian/吉米/12-原子卡片/xxx.md",
  "heading": "## 重排策略",
  "line_start": 45,
  "line_end": 62,
  "content_snippet": "...本文讨论了重排的几种常见方法...",
  "score": 3.14,
  "tags": ["算法/排序", "赛题/2024xxx"],
  "date": "2024-03-15",
  "obsidian_uri": "obsidian://open?path=..."
}
```

**验证"结果确实相关"：**
1. **前端高亮**：在 content_snippet 中将 query 的 ngram 匹配项用 `<mark>` 高亮，用户一眼可见匹配依据
2. **出处跳转**：点击结果调用 Obsidian URI scheme 打开原文件并跳转到对应行
3. **人工抽检**：对已知查询（如"重新排序"）手动验证返回结果包含"重排"相关笔记
4. **反馈机制（可选）**：每条结果旁加"相关/不相关"按钮，收集数据用于后续优化

---

### 技术选型总结

| 层 | 选型 | 理由 |
|----|------|------|
| 后端框架 | FastAPI | 异步支持好，自动生成 API 文档 |
| 数据层 | pylibseekdb (seekdb) | 用户指定，嵌入式无需额外服务 |
| 中文分词 | ngram | 用户指定，解决默认分词器中文 0 命中问题 |
| 前端 | 原生 HTML/JS | 轻量，无需构建工具 |
| YAML 解析 | PyYAML | 标准库级别稳定 |
| 可选增强 | sentence-transformers + 本地 LLM | 默认关闭，不影响核心功能 |

---

请确认此设计方案是否符合预期，确认后我将进入实现阶段。
