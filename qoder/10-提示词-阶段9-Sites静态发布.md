这轮先说结论：你的判断我认同，选方案 A（静态站点 + 预构建 JSON），但要做扎实。

【为什么是 A，不是 B/C】
- B 要公网服务器，且不是 Qoder Sites，第一项材料直接不满足，排除。
- C 把后端重写成 TypeScript + 云数据库，等于让 seekdb 完全退出链路。这是 OceanBase 大赛，
  技术主体是 seekdb，重写后反而丢分，而且工作量最大，排除。
- A 的唯一代价是"在线版不是 seekdb 在跑"，只要在页面上**如实标注**，并把完整实现留在仓库，
  就不算虚假陈述。评审看到的是"作者理解平台限制并给出了正确降级方案"，这是加分不是减分。

【关键数据（我已经算过，你按这个来设计）】
- 全部 219 个 chunk 的 content 合计 109,535 字符，导出成 JSON 约 0.25 MB。
- 最长单块 5,779 字符。
- 结论：**不需要倒排索引，不需要分片，浏览器一次性 fetch 后在内存里做匹配即可**。
  不要给我设计一个"构建倒排索引"的复杂方案，0.25 MB 的数据扛得住线性扫描。

【任务一：导出脚本 tools/export_static.py】
从 seekdb 读出全部 chunk，写到 site/data/chunks.json 和 site/data/meta.json。

chunks.json 每条形如：
  {
    "id": 2000012,
    "file_name": "2026-10-29-PowerContext-11题攻关手册.md",
    "file_path": "D:\\新建文件夹\\Obsidian\\吉米\\11-赛题归档\\2026-10-29-PowerContext-11题攻关手册.md",
    "dir_type": "11-赛题归档",
    "year": 2026,
    "tags": ["赛道/PowerContext"],
    "heading": "T4 · 检索重排",
    "line_start": 99,
    "line_end": 116,
    "content": "## T4 · 检索重排\n\n**考什么**：..."
  }

meta.json：
  { "generated_at": "...", "notes_count": 26, "chunks_count": 219, "source_root": "D:/新建文件夹/Obsidian/吉米" }

要求：
- 按 id 升序输出，保证每次导出结果稳定（便于 git diff）
- ensure_ascii=False，缩进 2；不要输出 BOM
- 打印一行统计：导出了多少块、文件多大（字节）
- 不包含任何密钥、路径以外的个人标识；路径里的用户名可以保留（本地路径本来就是出处信息）

【任务二：静态站点 site/】
只新增文件，不要动 backend/。结构：
  site/index.html
  site/app.js
  site/style.css
  site/data/chunks.json
  site/data/meta.json

硬要求：
1. **零外部依赖**：不引 CDN、不用框架、不加载外部字体和图标。比赛评审环境可能网络受限，
   页面必须在断网状态下也能完整工作（只允许 fetch 自己的 data/*.json）。
2. 功能与本地版对齐：
   - 关键词搜索（中文直接子串匹配）
   - 过滤：目录类型（多选）、年份、TopK
   - 结果卡片：文件名（只显示文件名，不显示全路径；title 属性放全路径）、小节、行号区间、score、标签
   - 关键词高亮：先转义再包 <mark>，属性值用 escapeAttr（额外转义 " 和 '）
   - 点击卡片展开原文，带行号；展开后从 line_start 渲染到 line_end
   - URL 参数可复现：?q=&type=&year=&top_k=，进入时自动回填并搜索，用 replaceState 更新地址栏
3. **页面顶部必须有一条醒目的说明**（不可折叠、不可关闭），大意是：
   「这是静态镜像版：检索在浏览器内完成，数据由本机 seekdb 导出的快照生成。
     完整实现（seekdb ngram 全文索引 + SQL 过滤）见 GitHub 仓库，可本地复现。」
   要给出仓库链接 https://github.com/2397819368a-oss/powercontext-2026/tree/2026-competition-ecosystem/qoder
4. **结果区上方加一个「检索条件」折叠面板**（替代本地版的 SQL 面板）：
   显示本次检索的等价条件，例如
     「关键词 = 重排；目录 = 11-赛题归档；年份不限；Top 3；候选池 219 块 → 命中 19 块」
   并明确标注一句：「静态版无 SQL 执行，以上为等价检索条件；本地版对应语句见仓库 docs/样例.md」。
   不要伪造一条 SQL 显示出来。

【任务三：打分公式必须可解释、可对照】
浏览器里的相关性打分自己定义，但必须满足：
- 公式简单到能写进页面说明，例如
    score = 2.0 × 关键词出现次数 + 3.0 × (标题命中 ? 1 : 0) − 0.001 × content 长度
  （你可以调整，但必须固定下来并写进页面）
- 中文匹配用"子串包含"，命中位置用 indexOf 找，不要试图在 JS 里复刻 seekdb 的 ngram 切分。
- 排序：score 降序；score 相同时按 id 升序，保证结果稳定可复现。

同时新增 tools/check_static_parity.py：
- 用 Python 按**完全相同的公式**对 chunks.json 打分
- 对同一组查询，分别取 seekdb Top-5 和静态版 Top-5，比较 id 集合
- 至少覆盖这几组：
    重排 / 无过滤 / top5
    重排 / dir_type=11-赛题归档 / top5
    缓存 / 无过滤 / top5
    混合检索 / 无过滤 / top5
    ngram / 无过滤 / top5
  策略 / 无过滤 / top5
- 输出每组的重合数（如 5/5、4/5）和总重合率，最后打印一行"平均 Top-5 重合率: xx%"
- 如果某组重合率低于 3/5，把两边的结果并排打出来，方便我判断是公式问题还是数据问题

【任务四：发布】
- 把 site/ 用 Qoder Sites 发布为静态站点，访问策略设为**公开（任何人可访问）**
- 发布前确认：站点根目录是 site/，首页能直接打开，data/ 下的 json 能被 fetch 到（不要 404）
- 发布后给我完整访问地址
- 如果是写操作需要我确认，先说明会改动什么，等我确认

【任务五：文档】
- README 新增「在线演示版」一节，写清三件事：
  1. 在线版地址（发布后填）
  2. 为什么不能直接部署完整版（Sites 只支持 TypeScript/Node 运行时、无 pip、无持久化磁盘、数据库是云托管，
     四条限制逐条写）
  3. 在线版与本地版的差异（检索在浏览器内、无 SQL、打分公式不同但 Top-5 重合率 xx%），
     以及本地复现命令
- docs/样例.md 增补一节「在线版如何验证」：给出在线地址 + 同样几组输入 + 预期结果

【不要做的事】
- 不要改 backend/ 下任何文件，不要动表结构，不要重新灌数据
- 不要把 seekdb 从项目里拿掉，也不要在文档里暗示"在线版也用了 seekdb"
- 不要引任何 CDN、不要把页面做成需要构建的（不要 webpack/vite/npm build），
  site/ 必须是可以直接静态托管的纯文件
- 不要伪造 SQL 面板

【改完贴给我】
(a) tools/export_static.py 的运行输出（块数 + 文件大小）
(b) tools/check_static_parity.py 的完整输出（每组重合数 + 平均重合率）
(c) 站点发布后的完整地址，以及你实际打开确认过首页和 data/chunks.json 都能访问的结果
(d) README「在线演示版」那一节的截图或文字
