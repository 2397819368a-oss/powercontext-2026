/**
 * record-model-sizes.mjs —— 把离线权重文件的真实磁盘字节数写成 sizes.json
 *
 * 为什么不能直接用干静态服务器：
 *  体积对比是很严肃的一栏数据，但用 HTTP HEAD 的 Content-Length 是**错的**：
 *  静态服务器一旦开了 gzip，Content-Length 返回的是压缩后的大小。实测里
 *  INT8 权重值域小、压缩率高，21.96 MB 被报成 16.32 MB；而 FP32/FP16 几乎压不动，
 *  数字又恰好是对的 —— 这种"部分正确"最难发现。
 *  因此这里在构建前直接 stat 磁盘文件，把真实字节数固化为 sizes.json，
 *  页面优先读它，彻底绕开传输编码的影响。
 *
 * 用法: node tools/record-model-sizes.mjs
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const WEB = path.resolve(__dirname, "..");
const MODELS_DIR = path.join(WEB, "public", "models");

if (!fs.existsSync(MODELS_DIR)) {
  console.error("没有 public/models，请先准备离线权重");
  process.exit(1);
}

function walkModelDir(dir, relBase) {
  const out = {};
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const abs = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      Object.assign(out, walkModelDir(abs, relBase));
    } else if (entry.name.endsWith(".onnx")) {
      // key 与 DTYPES[].file 一致：onnx/xxx.onnx
      const key = path.relative(relBase, abs).replace(/\\/g, "/");
      out[key] = fs.statSync(abs).size;
    }
  }
  return out;
}

/** public/models 下每个 <org>/<name> 都是一个模型目录 */
function eachModel(callback) {
  for (const org of fs.readdirSync(MODELS_DIR, { withFileTypes: true })) {
    if (!org.isDirectory()) continue;
    const orgDir = path.join(MODELS_DIR, org.name);
    for (const name of fs.readdirSync(orgDir, { withFileTypes: true })) {
      if (!name.isDirectory()) continue;
      const dir = path.join(orgDir, name.name);
      callback(`${org.name}/${name.name}`, dir);
    }
  }
}

let count = 0;
eachModel((modelId, dir) => {
  const files = walkModelDir(dir, dir);
  if (Object.keys(files).length === 0) return;
  const payload = {
    generatedAt: new Date().toISOString(),
    source: "local-disk",
    note: "真实磁盘字节数（未受 HTTP 传输编码影响）",
    files,
  };
  fs.writeFileSync(path.join(dir, "sizes.json"), JSON.stringify(payload, null, 2), "utf8");
  count += 1;
  console.log(`${modelId}:`);
  for (const [k, v] of Object.entries(files)) {
    console.log(`  ${k.padEnd(28)} ${String(v).padStart(12)} B  ${(v / 1024 / 1024).toFixed(2)} MB`);
  }
});

console.log(`\n已生成 ${count} 个 sizes.json`);
