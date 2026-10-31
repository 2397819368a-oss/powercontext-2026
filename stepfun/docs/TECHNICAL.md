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

**体积**：页面运行时从 HuggingFace 仓库 API 读取三个文件的真实字节数并算压缩比，
不在代码里写死数字（避免文档与实际不一致）。也可以离线取：

```bash
node tools/fetch-model.mjs          # 下载后终端会直接打印三种精度的体积与压缩比
```

**质量**：同一批文本分别用参考精度（FP32）与待测精度各算一次向量，
比较余弦相似度，越接近 1 说明损失越小。页面「3 · 量化评估」面板即为此。
注意两次前向都固定跑在 WASM 上，保证基线一致。

**本机未能产出的部分**：当前环境无外网访问权限，权重无法下载，
因此体积与质量的实测数字**留待联网环境补齐**，此处不以估算值填充。

## 5. 验收证据（均为实跑结果）

| 项 | 命令 | 结果 |
|---|---|---|
| db 层自检 | `cd server && npm run smoke` | 11 passed / 0 failed（DIM=8 与 384 双跑通过） |
| HTTP 链路 | `node tools/http-check.mjs` | 10 passed / 0 failed |
| 前端构建 | `cd web && npx vite build` | 38 modules，822.60 kB（gzip 202.54 kB） |
| 页面连通 | 无头浏览器抓 DOM | `seekdb 已连接`、示例 8 条、库内 `4 条 · 384 维` |
| 降级标注 | 无头浏览器抓 DOM | 「WebGPU 存在但拿不到 adapter…已降级到 WASM」 |

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

## 7. 联网环境下补齐验证的一步清单

1. `node tools/fetch-model.mjs`（或 `--only int8`）下载权重；
2. `cd server && npm start`；
3. `cd web && npm run dev`；
4. 打开 `http://127.0.0.1:5173/?auto=1&dtype=int8&q=混合检索`
   即可自动完成「加载模型 → 建索引 → 检索」并留在页面上；
5. 点「读取各精度体积」「跑质量对比」取得量化报告；
6. 把实测数字回填到本文档第 4 节。
