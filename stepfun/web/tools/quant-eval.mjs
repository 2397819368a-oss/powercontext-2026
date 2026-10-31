/**
 * quant-eval.mjs —— 量化评测（离线）
 *
 * 目的：给「INT8 / FP16 相对 FP32 的体积压缩比与质量损失」提供可复现的实测数字。
 *
 * 设计要点：
 *  1. allowRemoteModels=false —— 只要本脚本跑通，就证明权重落地之后推理完全不需要外网。
 *  2. 体积直接读磁盘字节数（不是从 API 拿），因此不存在"标称值 vs 实际"的偏差。
 *  3. 质量指标用余弦相似度：FP16/INT8 句向量与 FP32 句向量逐条比对。
 *     向量已做过 L2 normalize（pooling=mean, normalize=true），余弦相似度分布便于直接观察。
 *
 * 用法： node tools/quant-eval.mjs
 * 输出： tools/quant-result.json + tools/quant-result.md
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { pipeline, env, cos_sim } from "@xenova/transformers";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const MODEL_ROOT = path.resolve(__dirname, "..", "public", "models");
const MODEL_ID = "Xenova/all-MiniLM-L6-v2";

// 本地优先，且**禁止回退远程**：跑通即等价于"完全离线可用"
env.localModelPath = MODEL_ROOT.replace(/\\/g, "/") + "/";
env.allowLocalModels = true;
env.allowRemoteModels = false;

const DTYPES = {
  fp32: { label: "FP32（原始）", dtype: "fp32", file: "onnx/model.onnx" },
  fp16: { label: "FP16（半精度）", dtype: "fp16", file: "onnx/model_fp16.onnx" },
  int8: { label: "INT8（量化）", dtype: "q8", file: "onnx/model_quantized.onnx" },
};

/** 评测文本：混合了本项目真实语料特征（检索术语、短句、中英混杂、长句） */
const TEXTS = [
  "混合检索融合了向量召回与关键词召回",
  "seekdb 支持 HNSW 向量索引与 ik 中文全文索引",
  "INT8 量化可以显著减小模型体积",
  "浏览器端推理使用 Transformers.js 与 onnxruntime-web",
  "WebGPU 不可用时降级到 WASM 后端",
  "混合检索的结果在库内用 RRF 融合排序",
  "句向量的维度是 384",
  "Hybrid search combines dense retrieval and sparse retrieval",
  "Embedding vectors are computed locally inside the browser",
  "量化后的模型在 CPU 上推理速度更快",
  "知识库检索要求低延迟与高精度",
  "mean pooling 之后再做 L2 normalize",
];

const RESULT = {
  meta: {
    model: MODEL_ID,
    dim: 384,
    runtime: `node ${process.version}`,
    remoteDisabled: env.allowRemoteModels === false,
    texts: TEXTS.length,
    date: new Date().toISOString(),
    note: "",
  },
  sizes: {},
  runs: {},
  quality: {},
};

function mb(bytes) {
  return (bytes / 1024 / 1024).toFixed(2);
}

/** 1) 体积：读真实磁盘字节数 */
console.log("=== 1. 权重文件体积 ===");
for (const [key, spec] of Object.entries(DTYPES)) {
  const abs = path.join(MODEL_ROOT, MODEL_ID, spec.file);
  if (!fs.existsSync(abs)) {
    console.log(`${key.padEnd(5)} : 缺少 ${spec.file}`);
    RESULT.sizes[key] = null;
    continue;
  }
  const size = fs.statSync(abs).size;
  RESULT.sizes[key] = { bytes: size, file: spec.file };
  console.log(`${key.padEnd(5)} : ${spec.file.padEnd(24)} ${String(size).padStart(10)} B  ${mb(size)} MB`);
}

const base = RESULT.sizes.fp32?.bytes;
console.log("\n=== 2. 加载并推理（逐精度）===");
/** key -> number[][] */
const vectors = {};

for (const [key, spec] of Object.entries(DTYPES)) {
  if (RESULT.sizes[key] == null) continue;
  try {
    const t0 = Date.now();
    const extractor = await pipeline("feature-extraction", MODEL_ID, { dtype: spec.dtype });
    const loadMs = Date.now() - t0;

    const t1 = Date.now();
    const out = await extractor(TEXTS, { pooling: "mean", normalize: true });
    const inferMs = Date.now() - t1;
    const vecs = out.tolist();
    if (typeof out.dispose === "function") out.dispose();

    vectors[key] = vecs;
    RESULT.runs[key] = { ok: true, loadMs, inferMs, perTextMs: +(inferMs / TEXTS.length).toFixed(1), dim: vecs[0].length };
    console.log(
      `${key.padEnd(5)} : OK  加载 ${String(loadMs).padStart(5)}ms  推理 ${String(inferMs).padStart(5)}ms ` +
        `(${TEXTS.length} 条, 平均 ${(inferMs / TEXTS.length).toFixed(1)}ms/条)  dim=${vecs[0].length}`
    );
  } catch (e) {
    RESULT.runs[key] = { ok: false, error: e.message };
    console.log(`${key.padEnd(5)} : FAILED  ${e.message}`);
  }
}

console.log("\n=== 3. 质量对比（以 FP32 为基准）===");
const ref = vectors.fp32;
if (ref) {
  for (const [key, spec] of Object.entries(DTYPES)) {
    if (key === "fp32" || !vectors[key]) continue;
    const sims = TEXTS.map((_, i) => Number(cos_sim(ref[i], vectors[key][i]).toFixed(6)));
    const mean = sims.reduce((a, b) => a + b, 0) / sims.length;
    // 归一化向量的余弦相似度 = 内积；角度差用于直观感知偏差有多少度
    const worst = Math.min(...sims);
    RESULT.quality[key] = {
      mean: +mean.toFixed(6),
      min: +worst.toFixed(6),
      max: +Math.max(...sims).toFixed(6),
      /** 平均偏离角度（度）*/
      avgAngleDeg: +((Math.acos(Math.min(1, mean)) * 180) / Math.PI).toFixed(3),
      perText: TEXTS.map((t, i) => ({ text: t, sim: sims[i] })),
    };
    console.log(
      `${key.padEnd(5)} vs fp32 : 平均 ${mean.toFixed(6)}  最差 ${worst.toFixed(6)}  ` +
        `偏离 ${((Math.acos(Math.min(1, mean)) * 180) / Math.PI).toFixed(3)}°`
    );
  }
} else {
  console.log("FP32 未成功，无法作为基准");
}

/* ---------------- 输出结构化结果 ---------------- */

const outJson = path.resolve(__dirname, "quant-result.json");
fs.writeFileSync(outJson, JSON.stringify(RESULT, null, 2), "utf8");

let md = `# 量化实测结果（自动生成）\n\n`;
md += `- 模型：\`${MODEL_ID}\`，维度 ${RESULT.meta.dim}\n`;
md += `- 运行环境：${RESULT.meta.runtime}\n`;
md += `- **是否离线运行**：${RESULT.meta.remoteDisabled ? "是（allowRemoteModels=false）" : "否"}\n`;
md += `- 评测文本：${TEXTS.length} 条\n`;
md += `- 生成时间：${RESULT.meta.date}\n\n`;

md += `## 体积与压缩比\n\n| 精度 | 权重文件 | 体积 | 相对 FP32 |\n|---|---|---|---|\n`;
for (const [key, spec] of Object.entries(DTYPES)) {
  const s = RESULT.sizes[key];
  if (!s) {
    md += `| ${spec.label} | \`${spec.file}\` | — | 未获取 |\n`;
  } else {
    const ratio = ((s.bytes / base) * 100).toFixed(1);
    const saved = (100 - s.bytes / base * 100).toFixed(1);
    md += `| ${spec.label} | \`${spec.file}\` | ${mb(s.bytes)} MB | ${key === "fp32" ? "基准 100%" : `${ratio}%（省 ${saved}%）`} |\n`;
  }
}

md += `\n## 质量（逐条余弦相似度，以 FP32 为基准）\n\n`;
if (ref) {
  md += `| 精度 | 平均相似度 | 最差 | 最好 | 平均偏离角 |\n|---|---|---|---|---|\n`;
  for (const [key, spec] of Object.entries(DTYPES)) {
    const q = RESULT.quality[key];
    if (!q) continue;
    md += `| ${spec.label} | ${q.mean.toFixed(6)} | ${q.min.toFixed(6)} | ${q.max.toFixed(6)} | ${q.avgAngleDeg}° |\n`;
  }
  md += `\n### 逐条明细\n\n`;
  for (const [key, spec] of Object.entries(DTYPES)) {
    const q = RESULT.quality[key];
    if (!q) continue;
    md += `**${spec.label}**\n\n| 文本 | 相似度 |\n|---|---|\n`;
    for (const row of q.perText) md += `| ${row.text.trim()} | ${row.sim.toFixed(6)} |\n`;
    md += `\n`;
  }
}

md += `## 加载与推理耗时\n\n| 精度 | 加载 | 推理(${TEXTS.length} 条) | 平均/条 |\n|---|---|---|---|\n`;
for (const [key, spec] of Object.entries(DTYPES)) {
  const r = RESULT.runs[key];
  if (!r) continue;
  if (!r.ok) {
    md += `| ${spec.label} | — | — | 失败：${r.error} |\n`;
  } else {
    md += `| ${spec.label} | ${r.loadMs} ms | ${r.inferMs} ms | ${r.perTextMs} ms |\n`;
  }
}

const outMd = path.resolve(__dirname, "quant-result.md");
fs.writeFileSync(outMd, md, "utf8");

console.log(`\n结果已写入：\n  ${outJson}\n  ${outMd}`);
process.exit(0);
