/**
 * fetch-model.mjs —— 把 embedding 模型的各种精度权重下载到本地，供前端离线使用
 *
 * 背景：默认情况前端会联网从 HuggingFace 拉权重。在离线/内网/比赛现场环境下
 *       这一步必然失败。本脚本把权重预先抓到 web/public/models/ 下，
 *       之后页面完全不需要外网。
 *
 * 用法（在能联网的机器上跑一次即可）：
 *   node tools/fetch-model.mjs
 *   node tools/fetch-model.mjs --host https://hf-mirror.com   # 国内镜像
 *   node tools/fetch-model.mjs --only int8                    # 只下量化版（体积最小）
 *   node tools/fetch-model.mjs --source modelscope            # 境外站不可达时用这个
 *
 * 下载完的产物结构（前端会自动优先读这里）：
 *   web/public/models/Xenova/all-MiniLM-L6-v2/
 *     config.json  tokenizer.json  tokenizer_config.json
 *     onnx/model.onnx            (FP32)
 *     onnx/model_fp16.onnx       (FP16)
 *     onnx/model_quantized.onnx  (INT8)
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { pipeline as streamPipeline } from "node:stream/promises";

const MODEL_ID = "Xenova/all-MiniLM-L6-v2";

// 定位到仓库根的 web/public/models
// 注意：必须用 fileURLToPath，不能用 decodeURI/字符串替换去处理 URL —— 那样遇到中文路径
// 会得到 %E5%A4%A7%E8%B5%9B 这种百分号编码串，在别人机器上就会找不到目录。
const here = path.dirname(fileURLToPath(import.meta.url));
const OUT = path.resolve(here, "..", "web", "public", "models", MODEL_ID);

const FILES = [
  "config.json",
  "tokenizer.json",
  "tokenizer_config.json",
  "special_tokens_map.json",
  "vocab.txt",
  "onnx/model.onnx",
  "onnx/model_fp16.onnx",
  "onnx/model_quantized.onnx",
];

/** 精度 -> 需要的文件 */
const PRECISION_MAP = {
  fp32: ["onnx/model.onnx"],
  fp16: ["onnx/model_fp16.onnx"],
  int8: ["onnx/model_quantized.onnx"],
};

/**
 * ModelScope 源。
 *
 * 存在的理由：本机到 HuggingFace / hf-mirror 都不通（DNS 能解析但 TCP 443 握手超时），
 * 而 ModelScope 可达。它的文件名与 HF 官方仓库**不一样**，所以必须做映射：
 *   FP32 <- onnx/model.onnx（同名）
 *   FP16 <- onnx/model_O4.onnx（ORT O4 优化，体积恰为 FP32 的一半，确为半精度权重）
 *   INT8 <- onnx/model_qint8_avx512.onnx（x64 动态量化版）
 * 注意 ModelScope 没有 `Xenova/all-MiniLM-L6-v2`，用的是社区同步版，详见 docs/TECHNICAL.md §4.3。
 */
const MODELSCOPE = {
  repo: "AI-ModelScope/all-MiniLM-L6-v2",
  revision: "master",
  // 目标文件名 -> 源仓库里的文件名
  map: {
    "onnx/model.onnx": "onnx/model.onnx",
    "onnx/model_fp16.onnx": "onnx/model_O4.onnx",
    "onnx/model_quantized.onnx": "onnx/model_qint8_avx512.onnx",
  },
  url: (file) =>
    `https://modelscope.cn/api/v1/models/${MODELSCOPE.repo}/repo?Revision=${MODELSCOPE.revision}&FilePath=${file}`,
};

function parseArgs(argv) {
  const args = { host: "https://huggingface.co", only: null, source: "huggingface" };
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === "--host") args.host = argv[++i];
    if (argv[i] === "--only") args.only = argv[++i];
    if (argv[i] === "--source") args.source = argv[++i];
  }
  return args;
}

/** 按下载源算出某个目标文件的 URL */
function urlFor(f, args) {
  if (args.source === "modelscope") {
    return MODELSCOPE.url(MODELSCOPE.map[f] ?? f);
  }
  return `${args.host}/${MODEL_ID}/resolve/main/${f}`;
}

async function download(url, dest) {
  fs.mkdirSync(path.dirname(dest), { recursive: true });
  // 已存在且非空就跳过，重复跑不会浪费带宽
  if (fs.existsSync(dest) && fs.statSync(dest).size > 0) {
    console.log(`  跳过（已存在） ${path.relative(OUT, dest)}`);
    return fs.statSync(dest).size;
  }
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status} for ${url}`);
  const tmp = `${dest}.part`;
  if (res.body) {
    await streamPipeline(res.body, fs.createWriteStream(tmp));
  } else {
    fs.writeFileSync(tmp, Buffer.from(await res.arrayBuffer()));
  }
  fs.renameSync(tmp, dest);
  return fs.statSync(dest).size;
}

const mb = (n) => `${(n / 1024 / 1024).toFixed(2)} MB`;

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const { only } = args;
  let files = FILES;
  if (only) {
    const need = PRECISION_MAP[only];
    if (!need) throw new Error(`未知精度 ${only}，可选：${Object.keys(PRECISION_MAP).join(" / ")}`);
    // 公共文件始终需要，权重文件按精度挑选
    files = FILES.filter((f) => !f.startsWith("onnx/") || need.includes(f));
  }

  const source = args.source === "modelscope" ? MODELSCOPE.repo : args.host;
  console.log(`目标模型: ${MODEL_ID}\n下载源: ${source}\n输出目录: ${OUT}\n`);

  const sizes = {};
  for (const f of files) {
    // 有些 tokenizer 变体不存在，缺失是允许的；权重文件缺失才报错
    const optional = ["special_tokens_map.json", "vocab.txt", "tokenizer_config.json"].includes(f);
    const url = urlFor(f, args);
    const dest = path.join(OUT, f);
    try {
      const size = await download(url, dest);
      sizes[f] = size;
      console.log(`  OK   ${f}  ${mb(size)}`);
    } catch (e) {
      if (optional) {
        console.log(`  --   ${f}（不存在，忽略）`);
      } else {
        console.log(`  FAIL ${f} :: ${e.message}`);
      }
    }
  }

  // 顺手输出压缩比，正好是赛题要的那份量化报告素材
  const base = sizes["onnx/model.onnx"];
  console.log("\n=== 体积对比 ===");
  for (const [label, key] of [
    ["FP32", "onnx/model.onnx"],
    ["FP16", "onnx/model_fp16.onnx"],
    ["INT8", "onnx/model_quantized.onnx"],
  ]) {
    const s = sizes[key];
    if (!s) continue;
    const ratio = label === "FP32" ? "基准" : `${((s / base) * 100).toFixed(1)}%（省 ${((1 - s / base) * 100).toFixed(1)}%）`;
    console.log(`  ${label.padEnd(5)} ${mb(s).padStart(10)}   ${ratio}`);
  }
  console.log("\n完成。之后前端在离线环境也能直接加载模型。");
}

main().catch((e) => {
  console.error("失败:", e.message);
  process.exit(1);
});
