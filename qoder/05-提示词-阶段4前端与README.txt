阶段 3 我复核通过了两件关键事：
- 行号修对了：chunk 1000012 的 T4 块 = 99-116；我随机抽 5 个块，按 line_start-line_end
  从原文件截取，5/5 与库内 content 完全一致。带出处这个卖点现在是真的。
- 22 passed 我自己跑过，不是看你的报告。

但我发现 3 个问题，其中第 1 个会直接误导评审：

【问题 1，必须修】index 的输出自相矛盾且误导。
现在跑 `python -m backend.app.cli index` 打印：
    处理文件: 1 / 跳过文件: 26 / 总块数: 0
而库里实际是 26 篇 / 219 块，完全正常。"总块数 0" 会被理解成"索引是空的"。
根因：stats['total_chunks'] 统计的是"本次新增块数"，标签却写成"总块数"；
且那个"处理文件 1"是 0 字节的 未命名.md —— ingest_file 在 chunks 为空时返回 (0, 0)，
被当成"处理成功但 0 块"，于是每次运行都会重复出现。

改成：
- 统计口径写清楚：本次新增块 / hash 未变跳过 / 无内容跳过（列出文件名，超过 3 个省略）
- 结尾必须打印库内合计：`库内合计: 26 篇 / 219 块`（从表里 COUNT 出来，不是累加）
- 0 块文件不再计入"处理文件"

【问题 2，必须补】你上一轮说新增了 verify_chunks.py，但项目目录里找不到这个文件
（我 find 过根目录和 backend/）。自检必须能复现，请落成 tools/verify_chunks.py：
- 参数 --sample N（默认 5），随机抽 N 块
- 按 line_start-line_end 从原文件截取，与库内 content 逐字比对（strip 后）
- 逐块打印 OK/BAD + 文件 + 行号，末尾汇总；有 BAD 时退出码 1
- 顺带检查三类异常并报告：行号越界（超过文件总行数）、line_start > line_end、行号 <= 0

【问题 3】还没有 Web 界面，这是赛道要求的"作品网站 / 体验入口"。

做完下面 A、B、C 三件事后停下等我确认（一次做完，不要分次问）：

A. 修 index 统计 + 新增 tools/verify_chunks.py（按上面要求）

B. Web 前端：FastAPI + 单页，不引入 node/打包工具/任何 CDN（离线必须能跑）
   - GET /                    返回单页 HTML（CSS/JS 内联在这一个文件里，或用 Jinja 模板放 templates/）
   - GET /api/search          q, type, year, tag, top_k → JSON 结果
   - GET /api/chunk/{id}      返回该块原文 + 前后各 2 行上下文（带行号），供展开面板用
   - GET /api/stats           库内 notes / chunks 数量
   页面必须有：
   1) 顶部显示库内统计（26 篇 / 219 块）
   2) 搜索框 + 目录类型下拉（10-MOC与索引 / 11-赛题归档 / 12-原子卡片 / 13-代码片段 / 14-文献卡片 / 全部）+ 年份 + top_k
   3) 结果卡片：score、文件名、小节、行号范围、摘要；点击卡片展开原文
   4) 展开面板里关键词高亮
   安全与正确性要求（这条别偷懒）：
   - 原文插入 DOM 前必须做 HTML 转义，然后才把命中的关键词包成 <mark>；
     直接 innerHTML 拼原文会让 Markdown 里的 <、& 破坏渲染，也是 XSS 口子
   - 行号在展开面板左侧对齐显示，等宽字体
   - 中文窄屏（1366 宽）不能出现横向滚动条

C. README.md（项目根）：
   - 一句话定位 + 真实痛点（不要写"能理解同义词"，那是假的）
   - 数据流：md 文件 → ingest(按 H2 切块 + frontmatter 解析) → seekdb(ngram 全文索引) → search(关键词 + 结构化过滤) → Web
   - 快速开始：pip install -r requirements.txt；建索引；CLI 搜索；启动 Web（uvicorn 命令写清楚）
   - 设计取舍（每条约 2 句）：为什么必须 ngram；为什么用单列 MATCH(content)；为什么行号要加 frontmatter offset
   - 已知限制（诚实写，别美化）：
     1) ngram 是字符级可检索，不是语义检索。搜"重新排序"能出结果只是因为与"跨批次排序"共享 bigram「排序」，换个说法（如"打乱顺序"）就召回不到
     2) 没有向量检索 / 重排
     3) 只按 H2 切块，超长小节不二次切分
     4) Windows 下 seekdb 库目录不可删（文件锁），重建只能清空表数据
   - 验证证据：pytest 22 passed；随机 5 块 5/5 与原文一致；ngram 对照实验（默认分词 0 命中 vs ngram 19 命中）

注意：数据库目录固定在 C:/Temp/mmkb_db，不要删这个目录，重建用清表。

做完停下，贴给我：
(a) index 的新输出（要有"库内合计"那行）
(b) tools/verify_chunks.py 的真实运行输出
(c) pytest 全量结果
(d) 启动 Web 后 curl /api/search 的一次真实 JSON（截前 2 条即可）+ 首页截图
