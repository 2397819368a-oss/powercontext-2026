/**
 * http-check.mjs —— HTTP 层端到端验证
 *
 * 这里刻意用「手工构造的 384 维向量」来冒充浏览器算好的结果，
 * 目的是在不依赖浏览器的情况下压测整条 HTTP 链路
 * （路由、JSON 解析、参数校验、seekdb 检索、响应序列化）。
 * 真实场景里这些向量由 web/ 端的 Transformers.js 产生。
 *
 * 运行：先 npm start（server），再 node tools/http-check.mjs
 */

const BASE = process.env.API_BASE || "http://127.0.0.1:8787";
const DIM = 384;

let pass = 0, fail = 0;
const ok = (n, extra = "") => { pass++; console.log(`  PASS  ${n} ${extra}`); };
const bad = (n, e) => { fail++; console.log(`  FAIL  ${n} :: ${e}`); };
const check = async (n, fn) => {
  try { const r = await fn(); ok(n, r ?? ""); return r; }
  catch (e) { bad(n, e); return null; }
};

// 确定性伪向量：seed 相同则结果相同，便于复现
function vec(seed) {
  const v = [];
  let x = seed * 9301 + 49297;
  for (let i = 0; i < DIM; i++) {
    x = (x * 9301 + 49297) % 233280;
    v.push(Number((x / 233280).toFixed(6)));
  }
  return v;
}

async function call(path, options) {
  const res = await fetch(`${BASE}${path}`, options);
  const json = await res.json().catch(() => ({}));
  return { status: res.status, json };
}

const DOCS = [
  { id: `h1-${Date.now()}`, content: "seekdb 支持 HNSW 向量索引与 ik 中文全文索引", c: 0 },
  { id: `h2-${Date.now()}`, content: "混合检索用 RRF 融合向量召回与关键词召回", c: 1 },
  { id: `h3-${Date.now()}`, content: "INT8 量化能把模型体积压缩到接近四分之一", c: 2 },
  { id: `h4-${Date.now()}`, content: "今天天气不错，完全不相关的句子", c: 99 },
];

console.log(`\n=== HTTP 端到端验证 (${BASE}, DIM=${DIM}) ===\n`);

await check("健康检查", async () => {
  const { status, json } = await call("/api/health");
  if (status !== 200 || !json.ok) throw new Error(`HTTP ${status} ${JSON.stringify(json)}`);
  return `dim=${json.dim} port=${json.endpoint?.port}`;
});

await check("清空", async () => {
  const { status, json } = await call("/api/docs", { method: "DELETE" });
  if (status !== 200) throw new Error(JSON.stringify(json));
  return "";
});

await check("写入 4 条（自供向量）", async () => {
  const items = DOCS.map((d) => ({
    id: d.id, content: d.content, embedding: vec(d.c), meta: { seed: d.c },
  }));
  const { status, json } = await call("/api/ingest", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ items }),
  });
  if (status !== 200) throw new Error(JSON.stringify(json));
  return `inserted=${json.inserted}`;
});

await check("拒绝无向量写入（禁云端推理）", async () => {
  const { status, json } = await call("/api/ingest", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ items: [{ id: "x", content: "服务端不能替我算" }] }),
  });
  if (status !== 400) throw new Error(`期望 400，实际 ${status}: ${JSON.stringify(json)}`);
  return `400 ${json.error.slice(0, 24)}…`;
});

await check("拒绝维度不符", async () => {
  const { status } = await call("/api/ingest", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ items: [{ id: "y", content: "维度错", embedding: [0.1, 0.2] }] }),
  });
  if (status !== 400) throw new Error(`期望 400，实际 ${status}`);
  return "400";
});

await check("混合检索：rrf 必须是数值", async () => {
  const { status, json } = await call("/api/search", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ embedding: vec(0), text: "混合检索 向量", topK: 3 }),
  });
  if (status !== 200) throw new Error(JSON.stringify(json));
  if (json.mode !== "hybrid") throw new Error(`mode=${json.mode}`);
  if (json.rows.length === 0) throw new Error("无结果");
  const top = json.rows[0];
  if (typeof top.rrf !== "number" || !Number.isFinite(top.rrf)) {
    throw new Error(`rrf 非数值: ${typeof top.rrf} ${top.rrf}`);
  }
  return `top1=${top.content.slice(0, 14)}… rrf=${top.rrf.toFixed(5)}`;
});

await check("仅向量检索", async () => {
  const { status, json } = await call("/api/search", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ embedding: vec(2), topK: 3 }),
  });
  if (status !== 200 || json.mode !== "vector") throw new Error(JSON.stringify(json));
  return `top1=${json.rows[0].content.slice(0, 14)}… cosine=${json.rows[0].distance?.toFixed(5)}`;
});

await check("仅全文检索", async () => {
  const { status, json } = await call("/api/search", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text: "量化 压缩", topK: 3 }),
  });
  if (status !== 200 || json.mode !== "fulltext") throw new Error(JSON.stringify(json));
  if (json.rows.length === 0) throw new Error("无结果");
  return `top1=${json.rows[0].content.slice(0, 14)}… score=${json.rows[0].score?.toFixed(4)}`;
});

await check("空请求应 400", async () => {
  const { status } = await call("/api/search", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
  });
  if (status !== 400) throw new Error(`期望 400，实际 ${status}`);
  return "400";
});

await check("TopK 上限生效", async () => {
  const { status, json } = await call("/api/search", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ embedding: vec(0), text: "检索", topK: 2 }),
  });
  if (status !== 200) throw new Error(JSON.stringify(json));
  if (json.rows.length > 2) throw new Error(`返回 ${json.rows.length} 条`);
  return `rows=${json.rows.length}`;
});

console.log(`\n=== ${pass} passed, ${fail} failed ===\n`);
process.exit(fail === 0 ? 0 : 1);
