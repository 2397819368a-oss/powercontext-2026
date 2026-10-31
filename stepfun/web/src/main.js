/**
 * main.js —— 页面逻辑
 *
 * 三条主线：
 *   1. 加载模型（如实展示 WebGPU/WASM 与降级原因）
 *   2. 文档在浏览器算向量后写入 seekdb
 *   3. 查询同样在浏览器算向量，retrieval 交给 seekdb（混合检索在其内部做 RRF）
 */

import {
  loadModel,
  embed,
  ready,
  session,
  MODEL_ID,
  DTYPES,
  fetchModelSizes,
  compareQuality,
} from "./embed.js";
import * as api from "./api.js";

const $ = (id) => document.getElementById(id);

// URL 参数：?auto=1&q=xxx&mode=hybrid&dtype=int8&device=auto
const params = new URLSearchParams(location.search);

/** 示例知识库：内容刻意让"语义相近"与"关键词命中"部分错开，
 *  这样混合检索和单路检索的结果差异才看得出来 */
const SAMPLE_DOCS = [
  "seekdb 是 OceanBase 推出的 AI 原生搜索数据库，内置向量索引与全文索引。",
  "混合检索把向量召回和关键词召回的结果用 RRF 融合，兼顾语义与精确匹配。",
  "RRF 的公式是 1/(k+rank)，k 通常取 60，好处是不需要归一化两路的分数。",
  "全文索引使用 ik 分词器，中文按词切分，比默认分词器的召回质量更好。",
  "浏览器端可以用 Transformers.js 跑 embedding，数据不出本机，满足隐私要求。",
  "INT8 量化把权重从 32 位压到 8 位，模型体积大约能缩小到原来的四分之一。",
  "WebGPU 可以利用显卡做推理，速度通常比 WASM 快，但需要浏览器和驱动支持。",
  "HNSW 是一种近似最近邻图索引，seekdb 的向量索引用它来加速检索。",
];

function log(msg, kind = "info") {
  const el = $("log");
  const line = document.createElement("div");
  line.className = `log-line log-${kind}`;
  line.textContent = msg;
  el.appendChild(line);
  el.scrollTop = el.scrollHeight;
}

function updateRuntime() {
  $("rt-device").textContent = session.loaded
    ? session.device === "webgpu"
      ? "WebGPU（GPU）"
      : "WASM（CPU）"
    : "—";
  $("rt-dtype").textContent = session.loaded
    ? (session.dtypeLabel ?? session.dtype)
    : "—";
  $("rt-load").textContent = session.loaded ? `${session.loadMs} ms` : "—";

  const warn = $("rt-warn");
  if (session.degraded && session.degradedReason) {
    warn.hidden = false;
    warn.textContent = `已降级到 WASM：${session.degradedReason}`;
  } else {
    warn.hidden = true;
  }

  $("chip-model").textContent = session.loaded ? `模型已就绪 · ${session.device}` : "模型未加载";
  $("chip-model").className = session.loaded ? "chip chip-ok" : "chip chip-idle";
}

async function refreshCount() {
  try {
    const s = await api.stats();
    $("rt-count").textContent = `${s.count} 条 · ${s.dim} 维`;
  } catch {
    $("rt-count").textContent = "（读取失败）";
  }
}

/**
 * 慢加载提示。
 * 首次运行要从 HuggingFace 拉 25~90MB 权重，网络受限时会一直卡住且毫无反馈，
 * 很容易被误判成"页面挂了"。这里到点给一句明确指引，而不是让页面沉默。
 */
function withSlowHint(promise, ms = 30000) {
  const timer = setTimeout(() => {
    log(
      "模型加载超过 30 秒仍未完成。首次运行需要下载约 25~90MB 权重；" +
        "若长时间无进展，通常是网络访问受限，请改用离线权重：node tools/fetch-model.mjs",
      "err"
    );
  }, ms);
  return promise.finally(() => clearTimeout(timer));
}

/* ---------------------------- 1. 加载模型 ---------------------------- */

$("btn-load").addEventListener("click", async () => {
  const btn = $("btn-load");
  btn.disabled = true;
  log("开始加载模型…");
  try {
    const dtype = $("sel-dtype").value;
    const device = $("sel-device").value;
    await withSlowHint(loadModel(dtype, device, (m) => log(m)));
  } catch (e) {
    log(`加载失败：${e.message}`, "err");
  } finally {
    updateRuntime();
    btn.disabled = false;
    $("btn-index").disabled = !ready();
    $("btn-search").disabled = !ready();
  }
});

/* ---------------------------- 2. 建索引 ---------------------------- */

$("btn-index").addEventListener("click", async () => {
  const btn = $("btn-index");
  btn.disabled = true;
  try {
    const docs = $("txt-docs").value
      .split("\n")
      .map((s) => s.trim())
      .filter(Boolean);
    if (docs.length === 0) {
      log("没有可写入的文档", "err");
      return;
    }
    log(`浏览器端计算 ${docs.length} 条向量…`);
    const t0 = performance.now();
    const vecs = await embed(docs);
    const ms = Math.round(performance.now() - t0);
    log(`向量化完成，${ms}ms（平均每条约 ${Math.round(ms / docs.length)}ms）`);

    const items = docs.map((content, i) => ({
      id: `doc-${Date.now()}-${i}`,
      content,
      embedding: vecs[i],
      meta: { idx: i },
    }));

    const r = await api.ingest(items);
    log(`已写入 seekdb：${r.inserted} 条`, "ok");
    await refreshCount();
  } catch (e) {
    log(`写入失败：${e.message}`, "err");
  } finally {
    btn.disabled = false;
  }
});

$("btn-clear").addEventListener("click", async () => {
  try {
    await api.clearDocs();
    log("已清空", "ok");
    await refreshCount();
  } catch (e) {
    log(`清空失败：${e.message}`, "err");
  }
});

/* ---------------------------- 3. 检索 ---------------------------- */

function renderResults(data) {
  const meta = $("meta");
  meta.innerHTML = "";
  const addChip = (label, cls = "") => {
    const s = document.createElement("span");
    s.className = `chip ${cls}`;
    s.textContent = label;
    meta.appendChild(s);
  };
  addChip(`模式：${data.mode}`, "chip-ok");
  if (data.rank) addChip(`融合策略：${data.rank}`);
  addChip(`命中 ${data.rows.length} 条`);

  const ol = $("results");
  ol.innerHTML = "";
  for (const r of data.rows) {
    const li = document.createElement("li");
    li.className = "res";

    const body = document.createElement("div");
    body.className = "res-body";
    body.textContent = r.content;

    const foot = document.createElement("div");
    foot.className = "res-foot";
    const parts = [];
    if (typeof r.rrf === "number") parts.push(`RRF ${r.rrf.toFixed(5)}`);
    if (typeof r.distance === "number") parts.push(`cosine ${r.distance.toFixed(5)}`);
    if (typeof r.score === "number") parts.push(`全文分 ${r.score.toFixed(4)}`);
    foot.textContent = parts.join(" · ");

    li.appendChild(body);
    li.appendChild(foot);
    ol.appendChild(li);
  }
  if (data.rows.length === 0) {
    const li = document.createElement("li");
    li.className = "res res-empty";
    li.textContent = "未找到结果";
    ol.appendChild(li);
  }
}

async function doSearch() {
  const q = $("inp-query").value.trim();
  if (!q) return;
  const mode = $("sel-mode").value;
  const topK = Number($("inp-topk").value) || 5;
  const btn = $("btn-search");
  btn.disabled = true;
  try {
    let payload = { topK };
    // 关键词路不需要向量；混合/向量路必须在本地先把查询算成向量
    if (mode === "hybrid" || mode === "vector") {
      const t0 = performance.now();
      const v = (await embed([q]))[0];
      payload.embedding = v;
      log(`查询向量化 ${Math.round(performance.now() - t0)}ms`);
    }
    if (mode === "hybrid" || mode === "fulltext") {
      payload.text = q;
    }
    const data = await api.search(payload);
    renderResults(data);
  } catch (e) {
    log(`检索失败：${e.message}`, "err");
  } finally {
    btn.disabled = false;
  }
}

$("btn-search").addEventListener("click", doSearch);
$("inp-query").addEventListener("keydown", (e) => {
  if (e.key === "Enter") doSearch();
});

/* ---------------------------- 4. 量化评估 ---------------------------- */

$("btn-size").addEventListener("click", async () => {
  const btn = $("btn-size");
  btn.disabled = true;
  try {
    const sizes = await fetchModelSizes();
    const base = sizes[DTYPES.fp32.file];
    const tb = $("tbl-size").querySelector("tbody");
    tb.innerHTML = "";
    for (const [key, spec] of Object.entries(DTYPES)) {
      const size = sizes[spec.file];
      const tr = document.createElement("tr");
      if (size == null) {
        tr.innerHTML = `<td>${spec.label}</td><td colspan="3">未获取到（仓库中没有 ${spec.file}）</td>`;
      } else {
        const ratio = base ? (1 - size / base) : 0;
        tr.innerHTML =
          `<td>${spec.label}</td>` +
          `<td><code>${spec.file}</code></td>` +
          `<td>${(size / 1024 / 1024).toFixed(2)} MB</td>` +
          `<td>${key === "fp32" ? "基准" : `体积 ${(size / base * 100).toFixed(1)}%，省 ${(ratio * 100).toFixed(1)}%`}</td>`;
      }
      tb.appendChild(tr);
    }
    $("tbl-size").hidden = false;
    log("体积数据已从 HuggingFace 读取", "ok");
  } catch (e) {
    log(`读取体积失败：${e.message}`, "err");
  } finally {
    btn.disabled = false;
  }
});

$("btn-quality").addEventListener("click", async () => {
  const btn = $("btn-quality");
  btn.disabled = true;
  try {
    const texts = [
      "混合检索融合了向量召回与关键词召回",
      "INT8 量化可以显著减小模型体积",
      " seekdb 支持 HNSW 向量索引与 ik 全文索引",
    ];
    const rows = await compareQuality(texts, "fp32", "int8", (m) => log(m));
    const tb = $("tbl-quality").querySelector("tbody");
    tb.innerHTML = "";
    for (const r of rows) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${r.text.trim()}</td><td>${r.sim}</td>`;
      tb.appendChild(tr);
    }
    $("tbl-quality").hidden = false;
    const avg = rows.reduce((a, b) => a + b.sim, 0) / rows.length;
    log(`平均相似度 ${avg.toFixed(6)}`, "ok");
  } catch (e) {
    log(`质量对比失败：${e.message}`, "err");
  } finally {
    btn.disabled = false;
    updateRuntime();
  }
});

/* ---------------------------- 启动 ---------------------------- */

/**
 * 自动演示模式。
 * ?auto=1&q=xxx&mode=hybrid&dtype=int8&device=auto
 * 依次完成「加载模型 -> 写入示例文档 -> 检索」。
 *
 * 存在的理由：无头浏览器没法点按钮，而这个项目最需要证明的恰恰是
 * 「浏览器真的在本地把向量算出来了」。有了这个参数，
 * 一条命令就能跑完整条链路并把结果留在 DOM 里，方便截图和回归。
 */
async function runAuto() {
  log("[auto] 开始自动演示", "ok");
  try {
    await withSlowHint(
      loadModel(
        params.get("dtype") || "int8",
        params.get("device") || "auto",
        (m) => log(`[auto] ${m}`)
      )
    );
    updateRuntime();
    log(
      `[auto] 模型就绪 device=${session.device} dtype=${session.dtype} degraded=${session.degraded}`,
      "ok"
    );
  } catch (e) {
    log(`[auto] 模型加载失败：${e.message}`, "err");
    return;
  }

  try {
    const docs = $("txt-docs").value.split("\n").map((s) => s.trim()).filter(Boolean);
    const t0 = performance.now();
    const vecs = await embed(docs);
    log(`[auto] 向量化 ${docs.length} 条，${Math.round(performance.now() - t0)}ms，维度=${vecs[0].length}`);
    const items = docs.map((content, i) => ({
      id: `doc-${Date.now()}-${i}`,
      content,
      embedding: vecs[i],
      meta: { idx: i },
    }));
    const r = await api.ingest(items);
    log(`[auto] 写入 seekdb ${r.inserted} 条`, "ok");
    await refreshCount();
  } catch (e) {
    log(`[auto] 建索引失败：${e.message}`, "err");
    return;
  }

  const q = params.get("q");
  if (!q) return;
  try {
    const mode = $("sel-mode").value;
    const payload = { topK: Number($("inp-topk").value) || 5 };
    if (mode === "hybrid" || mode === "vector") {
      payload.embedding = (await embed([q]))[0];
    }
    if (mode === "hybrid" || mode === "fulltext") payload.text = q;
    const data = await api.search(payload);
    renderResults(data);
    log(`[auto] 检索完成 mode=${data.mode} rows=${data.rows.length}`, "ok");
  } catch (e) {
    log(`[auto] 检索失败：${e.message}`, "err");
  }
}

async function bootstrap() {
  $("txt-docs").value = SAMPLE_DOCS.join("\n");
  $("sample-n").textContent = String(SAMPLE_DOCS.length);
  log(`模型仓库：${MODEL_ID}`);

  // 清空不依赖模型，可以立刻用；否则用户必须"先加载模型"才能清库，很不合理
  $("btn-clear").disabled = false;

  try {
    await api.health();
    $("chip-db").textContent = "seekdb 已连接";
    $("chip-db").className = "chip chip-ok";
    await refreshCount();
  } catch (e) {
    $("chip-db").textContent = "seekdb 未连接";
    log(`服务端未就绪：${e.message}`, "err");
  }

  const q = params.get("q");
  if (q) $("inp-query").value = q;
  if (params.get("mode")) $("sel-mode").value = params.get("mode");
  if (params.get("dtype")) $("sel-dtype").value = params.get("dtype");
  if (params.get("device")) $("sel-device").value = params.get("device");

  updateRuntime();

  if (params.get("auto") === "1") await runAuto();
}

bootstrap();
