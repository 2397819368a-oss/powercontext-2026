# powercontext 技术说明

## 1. 要解决的问题

做一个迷你知识库，同时满足三条硬约束：

1. **向量必须在浏览器本地算**，不允许调用任何云端推理服务；
2. **混合检索必须由 seekdb 完成**，不是在应用层拼结果；
3. **要给出 INT8 / FP16 量化的实测收益**（体积、压缩比、质量）。

难点不在"能不能跑起来"，而在于怎么让这三条约束**可被验证**，而不是口头承诺。

## 2. 架构与数据流

```
┌──────────────────────── 浏览器 ────────────────────────┐
│  用户输入文本                                            │
│      ↓ tokenizer (tokenizer.json)                       │
│      ↓ ONNX Runtime Web 前向推理                         │
│         └─ WebGPU(优先) / WASM(降级，并在 UI 标注原因)       │
│         └─ FP32 / FP16 / INT8 可选                       │
│      ↓ mean pooling + L2 normalize                      │
│  384 维向量                                              │
└──────────────────────────┬──────────────────────────────┘
                           │ HTTPS(JSON)：文本 + 向量一起提交
┌──────────────────────────▼──────────────────────────────┐
│  Express（server/）                                      │
│    · 不引入任何 embedding 模型                            │
│    · 缺 embedding 直接 400                               │
└──────────────────────────┬──────────────────────────────┘
                           │ MySQL 协议 over TCP
┌──────────────────────────▼──────────────────────────────┐
│  seekdb 本地实例（@oceanbase/seekdb-node 拉起）            │
│    ├─ VECTOR INDEX idx_vec  (cosine, hnsw, lib=vsag)     │
│    ├─ FULLTEXT INDEX idx_ft (WITH PARSER ik)            │
│    └─ RRF 融合：score = Σ 1/(60 + rank)                  │
└─────────────────────────────────────────────────────────┘
```

**为什么服务端是这样**：整个服务端依赖里没有任何 embedding 模型，
物理上没有"代算向量"的能力。这比在文档里写"我们不会上传数据"更可靠。

## 3. 关键技术选型及其依据

### 3.1 为什么不用 seekdb 的嵌入式模式

`seekdb@1.4.0` 的嵌入式模式依赖原生插件 `@seekdb/js-bindings`。
官方文档的系统支持矩阵明确写着 **Windows 不支持嵌入式**（Linux/macOS 支持）。
本机实测症状与文档一致，且极具迷惑性：

- 原生库能加载、数据目录能正常创建、telemetry 里也会写入 `bootstraped` 事件；
- 但只要执行**第一条真实 SQL** 就永久阻塞；
- 该调用是**同步阻塞**的，连 `Promise.race` 里埋的 30 秒超时都不会触发，
  进程也无法被正常杀掉 —— 表现为"卡死 + CPU 占用 0"。

定位过程保留了完整证据链：不传 embedding function 时 `createCollection`
会在 5ms 内快速返回错误（`default-embed is not registered`），
说明慢的不是 `createCollection` 本身；一旦走到第一条 SQL 才卡住。

改用 `@oceanbase/seekdb-node`（官方文档写明 Windows 11 x64 已测试、走 TCP）
拉起本地实例后，全链路 6.3 秒跑通。

> 澄清：这里的"服务器模式"指的是**本机进程 + 本机 TCP**，
> 与"云端推理"无关，不违反赛题约束。

### 3.2 建表语法来源

向量表 DDL 直接取自 seekdb SDK 自身的 `SQLBuilder.buildCreateTable`，而非手写猜测。
三个容易错的点：

- `VECTOR INDEX idx_vec (embedding) WITH(...)` —— 列名在括号里，**没有 `ON`**；
- 必须带 `lib=vsag`；
- 表尾要有 `ORGANIZATION = HEAP`。

### 3.3 为什么用 RRF 而不是加权求和

向量路返回的是 cosine 距离（0~2），全文路返回的是 BM25 分数（无量纲上限），
两者尺度不可比，归一化本身就是个额外假设。
RRF 只用**名次**，`score(d) = Σ 1/(k + rank_i(d))`，不需要对齐尺度，k=60 是常用取值。

实现上是在**库内**用一条 SQL 完成（CTE + `ROW_NUMBER()` + `UNION` 去重），
不是应用层拼接：

```sql
WITH ranked_vec AS (
  SELECT id, ROW_NUMBER() OVER (ORDER BY cosine_distance(embedding, ?) ASC) AS rk FROM docs
),
ranked_ft AS (
  SELECT id, ROW_NUMBER() OVER (ORDER BY MATCH(content) AGAINST(? ...) DESC) AS rk
    FROM docs WHERE MATCH(content) AGAINST(? ...)
),
fused AS ( ... LEFT JOIN / RIGHT JOIN 后 UNION ... )
SELECT ... FROM fused JOIN docs ON fused.id = docs.id
ORDER BY fused.rrf DESC LIMIT ?
```

之所以写两次 JOIN 再 UNION：左连接会漏掉"只在全文命中"的文档，
右连接补回来，两侧 UNION 完成去重。

## 4. 量化方案

三种精度通过 Transformers.js 的 `dtype` 切换：

| 展示名 | dtype 取值 | 权重文件 |
|---|---|---|
| FP32 | `fp32` | `onnx/model.onnx` |
| FP16 | `fp16` | `onnx/model_fp16.onnx` |
| INT8 | `q8` | `onnx/model_quantized.onnx` |

### 4.1 体积（真实磁盘字节数）

数字由 `tools/record-model-sizes.mjs` 在构建前直接 stat 磁盘生成 `sizes.json`，页面读它。

> **为什么不读 HTTP 的 Content-Length**：静态服务器一旦开了 gzip，它给的是**压缩后**大小。
> 实测 INT8 权重值域小、压缩率高，21.96 MB 被报成 16.32 MB；而 FP32/FP16 几乎压不动，
> 数字又恰好是对的。这种"部分正确"最难发现，所以干脆绕开传输编码。

| 精度 | 字节数 | 体积 | 相对 FP32 |
|---|---|---|---|
| FP32 | 90,405,214 | 86.22 MB | 基准 |
| FP16 | 45,212,349 | 43.12 MB | 为 FP32 的 50.0%，省 50.0% |
| INT8 | 23,026,053 | 21.96 MB | 为 FP32 的 25.5%，省 74.5% |

### 4.2 质量（句向量余弦相似度，以 FP32 为基准）

评测文本 8 条（短句、技术术语、中英夹杂各若干），两次前向都固定跑在 WASM 上以保证基线一致。

| 文本 | FP16 vs FP32 | INT8 vs FP32 |
|---|---|---|
| 混合检索融合了向量召回与关键词召回 | 0.999997 | 0.979867 |
| seekdb 支持 HNSW 向量索引与 ik 中文全文索引 | 0.999999 | 0.992363 |
| INT8 量化可以显著减小模型体积 | 0.999997 | 0.992370 |
| 浏览器端推理使用 Transformers.js 与 onnxruntime-web | 0.999998 | 0.990808 |
| WebGPU 不可用时降级到 WASM 后端 | 0.999998 | 0.982038 |
| 混合检索的结果在库内用 RRF 融合排序 | 0.999997 | 0.986846 |
| Hybrid search combines dense retrieval and sparse retrieval | 0.999998 | 0.991307 |
| 知识库检索要求低延迟与高精度 | 0.999998 | 0.982682 |
| **平均** | **0.999998** | **0.987285** |

**交叉验证**：换个运行时独立复算一遍（`tools/verify-onnx-dtypes.py`，Python ORT 1.31，
不走浏览器），得到 FP16 `cos=0.99999660`、INT8 `cos=0.98966122` —— 与浏览器侧同量级，
说明这些数字不是某个运行时的偶发结果。

**结论**：INT8 把 86.22 MB 压到 21.96 MB（-74.5%），句向量相似度仍保持在 0.987 以上，
是体积敏感场景的首选；FP16 体积只省一半但几乎无损（≥0.9999），适合既要精度又要减半体积。

### 4.3 数据来源说明（如实标注）

权重来自 **ModelScope（`AI-ModelScope/all-MiniLM-L6-v2`）** 而非 HuggingFace 官方仓库，
原因是本机无法访问境外站点（DNS 可解析但 TCP 443 不通，详见 §7）。两者是同一模型
（`config.json` 完全一致：bert / hidden_size 384 / 6 层），但需说明：

- FP32、INT8 为仓库内的官方 sentence-transformers 导出版本，语义与 `Xenova/all-MiniLM-L6-v2` 对应；
- FP16 该仓库未提供同名文件，取的是档内的 ORT O4 优化版本（`model_O4.onnx`）。
  其体积恰为 FP32 的一半（45,212,349 ≈ 90,405,214/2），且前向结果与 FP32 有 ~1e-4 量级差异、
  与 INT8 差异明显不同，**确认是真实半精度权重而非原样副本**（若为 FP32 副本则差异应严格为 0）。

## 5. 验收证据（均为实跑结果）

| 项 | 命令 | 结果 |
|---|---|---|
| db 层自检 | `cd server && npm run smoke` | 11 passed / 0 failed（DIM=8 与 384 双跑通过） |
| HTTP 链路 | `node tools/http-check.mjs` | 10 passed / 0 failed |
| 前端构建 | `cd web && npm run build` | 通过，主包 566.60 kB（gzip 168.83 kB） |
| 页面连通 | CDP 抓 DOM | `seekdb 已连接`、示例 8 条、库内 `N 条 · 384 维` |
| 降级标注 | CDP 抓 DOM | 「WebGPU 存在但拿不到 adapter…已降级到 WASM」 |
| 全链路自动跑 | `node tools/cdp-run.mjs <url>` | 加载模型 → 写入 → 检索 → 出量化两张表，无报错 |
| 量化体积/质量 | 同上 + `tools/verify-onnx-dtypes.py` | 见 §4.1 / §4.2，两套运行时互相印证 |

> 无头验证**不用** `--virtual-time-budget`：它会把 `setTimeout` 快进，
> 页面里那些超时提示会被瞬间触发，跑出来的失败是假的。改用 CDP 轮询页面挂的
> `window.__AUTO_STATE__`（`tools/cdp-run.mjs`），"跑完了没有"是确定的，不靠猜时长。

检索实测（4 条文档，Top-3）：混合检索 `rrf=0.03252`（双命中项居首）、
纯向量 `cosine=0.00000`、纯全文 `score=0.8834`。

## 6. 已处理的工程风险

| 风险 | 处理 |
|---|---|
| seekdb native 在非 ASCII 路径下起不来 | 自动检测并降级到 `C:\powercontext-data`，可 `SEEKDB_DATA_DIR` 指定 |
| `CREATE TABLE IF NOT EXISTS` 静默跳过导致维度漂移 | 启动时比对 `information_schema`，冲突则报明确指引 |
| seekdb 把 DECIMAL 结果返回成字符串，前端变 `NaN` | 统一 `Number()` 转换，并有断言防回归 |
| 自检与正式库互相污染 | 自检强制使用独立数据目录 |
| npm 在 Windows 漏装原生 optional 包 | 显式按版本号补装 `@rollup/rollup-win32-x64-msvc`、`@esbuild/win32-x64` |
| 离线环境下模型加载静默卡死 | 加载超过 30 秒给出明确提示与离线方案指引 |
| seekdb 本地进程未优雅关闭会留下文件锁 | 服务端注册 SIGINT/SIGTERM 优雅关闭 |
| **v2 的 `dtype` 参数被静默忽略，量化对比全是假数据** | `@xenova/transformers@2.17.2` 不支持该选项，三个精度其实都加载了同一个 `model.onnx`，结果相似度恰好 `1.000000`。改用 `@huggingface/transformers@4`（`dtype` 生效），并交叉验证兜底 |
| ORT 1.14 加载不了新版 INT8 图（`protobuf parsing failed`） | 随 v4 一起升到 onnxruntime-web 1.31；文件本身没问题（Python ORT 1.31 能正常加载） |
| ORT 的 wasm 默认从 jsdelivr CDN 拉，离线永远加载不出来 | `tools/sync-ort-wasm.mjs` 同步到本地并设 `wasmPaths`；且**全量**同步（曾漏 asyncify 变体，Edge 156 直接报取不到模块） |
| 静态服务器开 gzip 使 `Content-Length` 给出压缩后大小 | 构建前 stat 磁盘生成 `sizes.json`，页面优先读它（§4.1） |
| 用 `--virtual-time-budget` 做无头验证会得到假失败 | 它快进 `setTimeout`，页面超时提示被瞬间触发；改为 CDP 轮询 `window.__AUTO_STATE__` |

## 7. 权重获取与离线复现

**为什么需要这一段**：本机到 HuggingFace / hf-mirror 均不可达。
DNS 能解析（`huggingface.co` → 31.13.68.169），但 **TCP 443 握手超时**，
而百度、npmmirror、ModelScope 都正常 200 —— 属于"国内网通、境外站被挡"，不是没网。
因此权重改从 **ModelScope** 获取（详见 §4.3 的来源标注）。

```bash
cd web
node tools/fetch-model.mjs --host https://modelscope.cn   # 拉取三个精度 + tokenizer
npm run build                                             # 内含 ort-wasm 同步与 sizes.json 生成
cd ../server && npm start                                 # 后端 8787，自动拉起 seekdb
node ../../stepfun/tools/cdp-run.mjs "http://127.0.0.1:5177/?auto=1&quant=1&dtype=int8" 600000
```

一键自动演示（人工演示用这个）：

```
http://127.0.0.1:5173/?auto=1&quant=1&dtype=int8&q=混合检索
```

会自动完成「加载模型 → 写库建索引 → 混合检索 → 出体积表/质量表」并把结果留在页面上，
不需要点任何按钮。
