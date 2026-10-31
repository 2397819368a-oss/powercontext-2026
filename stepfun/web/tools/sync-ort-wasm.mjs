/**
 * sync-ort-wasm.mjs —— 把 onnxruntime-web 的 wasm 运行时同步到 public/ort/
 *
 * 为什么必须有这一步：
 *  transformers.js 会把 ORT 的 wasm 二进制默认指向 jsdelivr CDN。
 *  一旦处在离线/内网/墙内环境，页面就会卡在"正在加载模型"却永远加载不出来，
 *  而且报错信息不会提到 CDN —— 非常难查。把二进制落到本地静态目录、
 *  再把 env.backends.onnx.wasm.wasmPaths 指过去（见 src/embed.js），离线才是真的离线。
 *
 * 用法: node tools/sync-ort-wasm.mjs
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const WEB = path.resolve(__dirname, "..");

/** transformers.js 可能把新版 ORT 装在嵌套目录里，两个位置都要找 */
const CANDIDATES = [
  path.join(WEB, "node_modules", "onnxruntime-web", "dist"),
  path.join(WEB, "node_modules", "@huggingface", "transformers", "node_modules", "onnxruntime-web", "dist"),
];

/**
 * 哪些运行时变体需要处理：
 *  ORT 按设备与编译选项挑一个用（纯 wasm / jsep=WebGPU / jspi / asyncify），
 *  具体挑哪个取决于浏览器能力，所以这里一律全量同步，一个都不能缺。
 *  曾经只同步了三个，结果 Edge 156 要的是 asyncify 那个，页面直接报
 *  "Failed to fetch dynamically imported module"。
 */
const PREFIXES = ["ort-wasm-simd-threaded", "ort-wasm-simd", "ort-wasm-threaded", "ort-wasm"];
const FILES = [
  ...new Set(
    CANDIDATES
      .flatMap((dir) => (fs.existsSync(dir) ? fs.readdirSync(dir) : []))
      .filter((f) => /\.(mjs|wasm)$/.test(f) && PREFIXES.some((p) => f.startsWith(p + ".") || f === p + ".wasm"))
  ),
].sort();

let src = CANDIDATES.find((d) => fs.existsSync(path.join(d, "ort-wasm-simd-threaded.mjs")));
if (!src) {
  console.error("找不到 onnxruntime-web/dist。请先 npm install");
  process.exit(1);
}

const dst = path.join(WEB, "public", "ort");
fs.mkdirSync(dst, { recursive: true });

let total = 0;
let n = 0;
for (const f of FILES) {
  const s = path.join(src, f);
  if (!fs.existsSync(s)) {
    console.log(`  跳过 ${f}（源目录中没有）`);
    continue;
  }
  fs.copyFileSync(s, path.join(dst, f));
  const size = fs.statSync(s).size;
  total += size;
  n += 1;
  console.log(`  ${f.padEnd(40)} ${(size / 1024 / 1024).toFixed(2)} MB`);
}
console.log(`\n已同步 ${n} 个文件共 ${(total / 1024 / 1024).toFixed(1)} MB -> public/ort/`);
