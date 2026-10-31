/**
 * smoke.mjs —— 服务端端到端自检（不启 HTTP，直接打 db 层）
 * 覆盖：中文/非 ASCII 路径降级、建表、自供向量写入、三种检索模式、
 *      以及"服务端拒绝代算向量"这条红线。
 *
 * 运行：npm run smoke
 * 用 DIM=384 跑真实维度： EMBED_DIM=384 node src/smoke.mjs
 */

// 必须在 import db.mjs 之前设置，因为 DIM 是模块加载时读取的常量
process.env.EMBED_DIM = process.env.EMBED_DIM || "8";

// 自检必须用【独立】数据目录：默认自检用小维度(8)，如果和正式服务(384维)
// 共用一个目录，表会以先建的维度为准，导致两边互相污染。
process.env.SEEKDB_DATA_DIR =
  process.env.SEEKDB_DATA_DIR || "C:/powercontext-data/seekdb-smoke.db";

// 自检之前先清掉这个目录。
// 为什么必须有这一步：切换 EMBED_DIM 重跑时，库里残留的旧维度表会让写入直接报
// "inconsistent dimension"（这是有意加的防御，防止静默维度漂移），
// 于是自检从第二次起就必然失败。自检要从零验证，所以每次跑都应该是干净的环境。
// 只删【自检自己的】目录，绝不碰正式服务的数据目录。
{
  const fs = await import("node:fs");
  fs.rmSync(process.env.SEEKDB_DATA_DIR, { recursive: true, force: true });
}

const DIM = Number(process.env.EMBED_DIM);

const db = await import("./db.mjs");

const t0 = Date.now();
let pass = 0;
let fail = 0;
const ok = (name, extra = "") => {
  pass++;
  console.log(`  PASS  ${name} ${extra}`);
};
const bad = (name, e) => {
  fail++;
  console.log(`  FAIL  ${name} :: ${e?.message ?? e}`);
};
const check = async (name, fn) => {
  try {
    const r = await fn();
    ok(name, r ?? "");
    return r;
  } catch (e) {
    bad(name, e);
    return null;
  }
};

// 造一个维度为 DIM 的确定性向量（越靠前越"相似"）
const vec = (seed) =>
  Array.from({ length: DIM }, (_, i) =>
    Number((0.5 + 0.01 * seed + 0.001 * ((i * (seed + 1)) % 7)).toFixed(4))
  );

console.log(`\n=== powercontext 服务端自检 (DIM=${DIM}) ===\n`);

await check("启动本地 seekdb 实例", async () => {
  await db.open();
  const ep = db.endpointInfo();
  return `transport=${ep.transport} port=${ep.port}`;
});

await check("清空旧数据", async () => {
  await db.clearDocs();
  return "";
});

const DOCS = [
  { id: "d1", content: "seekdb 是 OceanBase 的 AI 原生搜索数据库，支持向量与全文混合检索", meta: { tag: "介绍" } },
  { id: "d2", content: "混合检索用 RRF 融合向量召回和关键词召回的结果", meta: { tag: "检索" } },
  { id: "d3", content: "浏览器端用 Transformers.js 做本地向量化，INT8 量化可显著减小模型体积", meta: { tag: "前端" } },
  { id: "d4", content: "今天天气不错，适合出去散步", meta: { tag: "无关" } },
];

await check("写入自供向量", async () => {
  const r = await db.insertDocs(DOCS.map((d, i) => ({ ...d, embedding: vec(i) })));
  return `inserted=${r.inserted}`;
});

await check("统计", async () => {
  const s = await db.stats();
  if (s.count !== DOCS.length) throw new Error(`期望 ${DOCS.length} 条，实际 ${s.count}`);
  return `count=${s.count}`;
});

await check("红线：拒绝无向量的写入", async () => {
  try {
    await db.insertDocs([{ id: "bad", content: "服务端不该替我算向量" }]);
  } catch (e) {
    if (e.status === 400 && /浏览器端计算/.test(e.message)) return "正确拒绝(400)";
    throw new Error(`错误类型不符: ${e.message}`);
  }
  throw new Error("本该报错却插入成功了");
});

await check("红线：拒绝维度不符的向量", async () => {
  try {
    await db.insertDocs([{ id: "bad2", content: "维度错", embedding: [0.1, 0.2] }]);
  } catch (e) {
    if (e.status === 400) return "正确拒绝(400)";
    throw new Error(`错误类型不符: ${e.message}`);
  }
  throw new Error("本该报错却插入成功了");
});

await check("纯向量检索", async () => {
  const { rows, mode } = await db.vectorSearch(vec(0), 3);
  if (mode !== "vector") throw new Error("mode 不对");
  if (rows[0].id !== "d1") throw new Error(`最近邻应是 d1，实际 ${rows[0].id}`);
  return `top1=${rows[0].id} d=${rows[0].distance?.toFixed(6)}`;
});

await check("纯全文检索（ik 中文分词）", async () => {
  const { rows } = await db.fulltextSearch("混合检索 融合", 3);
  if (rows.length === 0) throw new Error("全文没命中任何结果");
  return `top1=${rows[0].id} score=${rows[0].score?.toFixed(4)} hits=${rows.length}`;
});

await check("混合检索（seekdb 内 RRF）", async () => {
  const { rows, mode, rank } = await db.hybridSearch(vec(0), "混合检索 向量", 4);
  if (mode !== "hybrid") throw new Error("mode 不对");
  if (rows.length === 0) throw new Error("无结果");
  // d1/d2 两条同时被向量和全文命中，rrf 应最高
  const top = rows[0];
  // 曾经踩过坑：seekdb 的 DECIMAL 结果是字符串，不转会静默变成 NaN
  if (typeof top.rrf !== "number" || !Number.isFinite(top.rrf)) {
    throw new Error(`rrf 必须是数值，实际 ${typeof top.rrf}: ${top.rrf}`);
  }
  if (top.id !== "d1") throw new Error(`应以 d1 为首，实际 ${top.id}`);
  return `top1=${top.id} rrf=${top.rrf.toFixed(5)} rank=${rank}`;
});

await check("混合检索返回条数受 topK 约束", async () => {
  const { rows } = await db.hybridSearch(vec(0), "检索", 2);
  if (rows.length > 2) throw new Error(`topK=2 却返回 ${rows.length} 条`);
  return `rows=${rows.length}`;
});

await check("关闭实例", async () => {
  await db.close();
  return "";
});

console.log(`\n=== ${pass} passed, ${fail} failed, ${Date.now() - t0}ms ===\n`);
process.exit(fail === 0 ? 0 : 1);
