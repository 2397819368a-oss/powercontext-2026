# powercontext · 浏览器本地向量化 + seekdb 混合检索

一个最小可运行的知识库：**向量在浏览器里算**，**检索在 seekdb 里做**。
服务端没有任何推理能力（连"顺手帮你算一下"都不行），这条边界写进了代码并有测试守着。

```
浏览器 (Transformers.js)              服务端                 seekdb
─────────────────────                ───────               ──────
文本 → tok → ONNX forward  ──向量──▶  Express  ──SQL──▶  本地实例
       ↑ 本地完成，不联网                 ↑ 只转发        ↑ 混合检索(RRF)在库内完成
```

## 环境要求

| 项 | 要求 | 本机实测 |
|---|---|---|
| Node.js | ≥ 22 | v22.22.2 |
| 系统 | Windows 11 x64 / Linux / macOS | Windows 11 家庭版 26200 (AMD64) |
| seekdb | 由 `@oceanbase/seekdb-node` 自带 | `5.7.25-OceanBase seekdb-v1.4.0.0` |

## 快速开始

```bash
# 1) 服务端（会拉起本地 seekdb 实例，首次约 5 秒）
cd server
npm install
npm start                 # http://127.0.0.1:8787

# 2) 前端（另开一个终端）
cd web
npm install
npm run dev               # http://127.0.0.1:5173

# 3) 打开页面 → 点「加载模型」→「写入示例文档」→ 输入查询 →「检索」
```

> **首次会自动下载约 25~90MB 模型权重**（视所选精度）。若所在环境访问 HuggingFace 困难，
> 可以先把权重抓到本地实现完全离线运行：
> ```bash
> node tools/fetch-model.mjs                      # 或 --host https://hf-mirror.com
> node tools/fetch-model.mjs --only int8          # 只下量化版，体积最小
> ```
> 权重落在 `web/public/models/` 后，页面会**优先读本地**，不再需要外网。

## 验证（本机实测结果）

```bash
cd server && npm run smoke        # 直接打 db 层
node tools/http-check.mjs         # 打完整 HTTP 链路（需先 npm start）
```

| 验证 | 结果 |
|---|---|
| 服务端自检 `npm run smoke` | **11 passed / 0 failed**（DIM=8 与 DIM=384 各跑一遍均通过） |
| HTTP 链路 `http-check.mjs` | **10 passed / 0 failed** |
| 前端生产构建 `vite build` | 38 modules，`822.60 kB`（gzip 202.54 kB） |
| 页面渲染与服务连接 | 无头 Edge 抓 DOM 确认：`seekdb 已连接` / 示例 8 条 / 库内 `4 条 · 384 维` |

实测检索样例（4 条文档，Top-3）：

- 混合检索：`top1=seekdb 支持 HNSW…`，`rrf=0.03252`，`rank=rrf(k=60)`
- 纯向量：`cosine=0.00000`（查询与自身）
- 纯全文（ik 分词）：`score=0.8834`

## 三个关键设计（都有测试守着）

### 1. 服务端不会、也不能替你算向量

`server/src/db.mjs` 的 `insertDocs()` 对每条记录强制检查 `embedding`，
缺失或维度不符直接 `400`，报错文案明确写"向量必须在浏览器端计算后传入"。
`/api/search` 同样：既不给 `embedding` 也不给 `text` → `400`。

这不是文档里的承诺，而是 `smoke.mjs` 里的两个用例：「拒绝无向量的写入」「拒绝维度不符」。

### 2. WebGPU 优先，降级必须看得见

`web/src/embed.js` 里 `detectWebGPU()` 不只检查 `navigator.gpu` 是否存在，
还要真的 `requestAdapter()` 成功——很多机器上前者存在、后者返回 null。
一旦降级，页面顶部黄色警示条会写明**具体原因**（实测抓到的原话：
「WebGPU 存在但拿不到 adapter（无可用显卡或被禁用）」）。

### 3. 混合检索发生在 seekdb 内部

`db.mjs` 的 `hybridSearch()` 用一条 SQL 在库内完成「向量排序 + 全文排序 + RRF 融合」，
不是把两路结果拉到 Node 里再拼。RRF 取 k=60：

```
score(d) = Σ  1 / (60 + rank_i(d))
```

全文检索走 seekdb 的 **ik 中文分词器**，比默认分词器在中文上的召回质量更好
（实测「全文索引 分词器」能正确命中）。

## 目录结构

```
stepfun/
├── server/src/
│   ├── index.mjs     HTTP 入口：/api/{health,stats,ingest,search,docs}
│   ├── db.mjs        实例管理 + 全部 SQL（建表/写入/三种检索）
│   └── smoke.mjs     db 层自检（11 项）
├── web/
│   ├── src/embed.js  浏览器端模型（WebGPU/WASM + FP32/FP16/INT8）
│   ├── src/api.js    只调自建服务端，没有任何第三方推理 API
│   ├── src/main.js   页面逻辑，支持 ?auto=1 自动演示
│   └── index.html
├── tools/
│   ├── fetch-model.mjs    离线权重下载
│   └── http-check.mjs     HTTP 层端到端验证（10 项）
└── docs/TECHNICAL.md      技术说明（含量化报告）
```

## 踩过的坑（都已在代码里处理）

**seekdb 嵌入式模式在 Windows 上不可用。** 官方文档的系统支持矩阵写得很明确：

| 系统 | 嵌入式 seekdb | 服务器模式 |
|---|---|---|
| Linux | 支持 | 支持 |
| macOS | 支持 | 支持 |
| **Windows** | **暂不支持** | 支持 |

实测症状很有迷惑性：native 库能加载、数据目录能建出来、telemetry 里也会写
`bootstraped`，但只要执行第一条 SQL 就永久阻塞，且是**同步阻塞**——
连 `Promise.race` 埋的超时都不会触发，进程还杀不掉。
换成 `@oceanbase/seekdb-node` 拉起本地实例 + MySQL 协议 over TCP 后一切正常。

对应的几个处理：

- **非 ASCII 路径自动降级**：seekdb 的 native 层在中文目录下起不来。
  检测到项目路径含非 ASCII（本项目就放在 `D:\OceanBase大赛\...`）时，
  数据目录自动改用 `C:\powercontext-data\seekdb.db` 并在日志里说明，
  也可显式 `SEEKDB_DATA_DIR=<ASCII路径>` 指定。
- **维度漂移防护**：`CREATE TABLE IF NOT EXISTS` 在表已存在时会静默跳过。
  若先用 `EMBED_DIM=8` 跑过自检，再以 384 维启动就会撞上一个莫名其妙的维度错误。
  启动时主动比对 `information_schema`，不一致就直接报清楚该怎么办。
- **自检用独立数据目录**，不污染正式库。
- **DECIMAL 结果是字符串**：seekdb 把 `1.0/(60+rank)` 以字符串返回，
  不转 Number 会让前端悄悄显示 `NaN`，已统一处理并有断言。
- **npm 的 optional dependencies bug**：Windows 上可能漏装平台原生包
  （`@rollup/rollup-win32-x64-msvc`、`@esbuild/win32-x64`）。
  报错里会提示；本项目用显式安装版本号的方式补上。

## 已知限制

- **浏览器端真实推理未在本机验证**：当前环境无法访问外网（HuggingFace 与镜像均不可达），
  所以"下载权重 → 浏览器内 forward"这一步没能实跑。
  代码层面已通过 `vite build` 与页面加载（确认服务连通、降级逻辑正确）验证；
  联网环境下按上方「离线权重」准备后即可完整运行。
- 混合检索的召回质量依赖 ik 分词与 HNSW 索引构建；数据量很小（数十条）时，
  向量路基本等价于暴力搜索，这是正常的。
- 单次写入上限 32MB / 每批 200 条；更大规模需要分片。
