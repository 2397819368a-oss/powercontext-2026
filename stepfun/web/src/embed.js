/**
 * embed.js —— 浏览器端本地向量化的唯一出口
 *
 * 赛题约束落在这里：
 *  - 全部推理发生在浏览器进程内（Transformers.js / onnxruntime-web），
 *    没有任何一次联网调用模型服务；模型文件虽然有首次下载，
 *    但下载完之后所有 forward 都在本地完成。
 *  - WebGPU 优先；不可用时降级到 WASM，并且**必须把实际用的后端如实上报**，
 *    由 UI 显示出来（不能悄悄降级还让用户以为是 GPU）。
 *  - 支持 fp32 / fp16 / int8 三种精度，用于量化对比。
 */

import { pipeline, cos_sim, env } from "@huggingface/transformers";

export const MODEL_ID = "Xenova/all-MiniLM-L6-v2";
export const DIM = 384;

/**
 * 模型从哪儿下。
 * huggingface.co 在国内常常连不通/极慢，hf-mirror.com 是常用镜像。
 * 在 web/.env 里写 VITE_HF_MIRROR=https://hf-mirror.com 即可切换。
 * 注意：这只是【首次下载权重】的地址，下载完所有推理都在浏览器本地完成，
 * 不涉及任何在线推理 API——这点与赛题"禁止云端推理"不冲突。
 */
const MIRROR = (import.meta.env?.VITE_HF_MIRROR || "").replace(/\/+$/, "");
if (MIRROR) {
  env.remoteHost = MIRROR;
  env.remotePathTemplate = "{model}/resolve/{revision}/";
}

/**
 * ORT 的 wasm 二进制默认从 jsdelivr CDN 拉，离线环境会永远卡在"正在加载模型"。
 * 这里指到本地静态目录；文件由 tools/sync-ort-wasm.mjs 从 node_modules 同步而来。
 */
if (env.backends?.onnx?.wasm) {
  env.backends.onnx.wasm.wasmPaths = import.meta.env?.VITE_ORT_WASM_PATH || "/ort/";
}

/**
 * 本地离线模型优先。
 *
 * 为什么必须有这条：Transformer.js 默认要联网从 HF 拉权重，
 * 一旦身处离线环境（内网/比赛现场/墙）页面就永远卡在"加载模型"。
 * 把权重放进 web/public/models/ 后，这里的 allowLocalModels 会让它直接读本地文件，
 * 再也不需要外网。tools/fetch-model.mjs 就是用来准备这份离线权重的。
 *
 * 注意：v4 起浏览器环境里 allowLocalModels 默认是 false，必须显式打开。
 */
env.localModelPath = import.meta.env?.VITE_LOCAL_MODEL_PATH || "/models/";
env.allowLocalModels = true;
env.allowRemoteModels = true; // 本地没有才回落到远程

/**
 * 三种精度：展示名 -> v4 的 dtype 取值（决定权重文件后缀）+ 实际文件名。
 *
 * **踩坑记录（重要）**：早期用的是 @xenova/transformers v2.17.2，它**不支持 `dtype` 选项**，
 * 传进去被静默忽略，于是三个精度加载的都是同一个 model.onnx —— 表现为"量化对比跑得很顺、
 * 平均相似度恰好 1.000000"，全是假数据。v4 的映射关系是：
 *   fp32 -> (无后缀) -> onnx/model.onnx
 *   fp16 -> _fp16     -> onnx/model_fp16.onnx
 *   q8   -> _quantized-> onnx/model_quantized.onnx
 * 见 node_modules/@huggingface/transformers/dist/transformers.web.js 的 DEFAULT_DTYPE_SUFFIX_MAPPING。
 */
export const DTYPES = {
  fp32: { label: "FP32（原始）", dtype: "fp32", modelFile: "model", file: "onnx/model.onnx" },
  fp16: { label: "FP16（半精度）", dtype: "fp16", modelFile: "model_fp16", file: "onnx/model_fp16.onnx" },
  int8: { label: "INT8（量化）", dtype: "q8", modelFile: "model_quantized", file: "onnx/model_quantized.onnx" },
};

/** 已加载的模型实例缓存：key = `${device}:${dtype}` */
const cache = new Map();

/** 当前活跃会话信息，UI 直接读它来展示"到底跑在什么上面" */
export let session = {
  loaded: false,
  device: null, // 'webgpu' | 'wasm'
  dtype: null, // 'fp32' | 'fp16' | 'q8'
  loadMs: 0,
  degraded: false, // 是否发生了降级
  degradedReason: "",
};

/**
 * 探测 WebGPU 是否真的可用。
 * 注意 navigator.gpu 存在 ≠ 可用：还要 requestAdapter() 成功才行
 * （虚拟机/无显卡/浏览器黑名单下都会返回 null）。
 */
export async function detectWebGPU() {
  if (typeof navigator === "undefined" || !("gpu" in navigator)) {
    return { ok: false, reason: "当前浏览器没有 navigator.gpu（不支持 WebGPU）" };
  }
  try {
    const adapter = await navigator.gpu.requestAdapter();
    if (!adapter) return { ok: false, reason: "WebGPU 存在但拿不到 adapter（无可用显卡或被禁用）" };
    return { ok: true, adapter };
  } catch (e) {
    return { ok: false, reason: `WebGPU 探测异常: ${e.message}` };
  }
}

/**
 * 加载模型。
 * @param {string} dtypeKey  'fp32' | 'fp16' | 'int8'
 * @param {'auto'|'webgpu'|'wasm'} devicePref
 * @param {(msg:string)=>void} onProgress
 */
export async function loadModel(dtypeKey = "fp32", devicePref = "auto", onProgress = () => {}) {
  const spec = DTYPES[dtypeKey];
  if (!spec) throw new Error(`未知精度: ${dtypeKey}`);

  let device = devicePref;
  let degraded = false;
  let reason = "";

  if (devicePref === "auto" || devicePref === "webgpu") {
    const probe = await detectWebGPU();
    if (probe.ok) {
      device = "webgpu";
    } else {
      if (devicePref === "webgpu") {
        // 用户明确要 WebGPU 却拿不到，按题意必须明确告知，而不是静默换掉
        throw new Error(`无法使用 WebGPU：${probe.reason}`);
      }
      device = "wasm";
      degraded = true;
      reason = probe.reason;
      onProgress(`WebGPU 不可用：${probe.reason}，已降级到 WASM`);
    }
  }

  const key = `${device}:${dtypeKey}`;
  if (cache.has(key)) {
    session = { ...cache.get(key), loaded: true, loadMs: 0, degraded, degradedReason: reason };
    onProgress("复用已加载模型");
    return session;
  }

  onProgress(`正在加载 ${spec.label} 权重（${device}）...`);
  const t0 = performance.now();

  let extractor;
  try {
    extractor = await pipeline("feature-extraction", MODEL_ID, {
      device,
      dtype: spec.dtype, // v4 起支持；会决定权重文件后缀，见 DTYPES 注释
    });
  } catch (e) {
    // 某些组合（例如 WebGPU + 量化）ORT 不支持，退回 fp32 + WASM 并如实上报
    if (device === "webgpu") {
      onProgress(`${device}+${dtypeKey} 加载失败(${e.message})，回退到 WASM`);
      device = "wasm";
      degraded = true;
      reason = `${dtypeKey} 在 WebGPU 下不被支持：${e.message}`;
      extractor = await pipeline("feature-extraction", MODEL_ID, {
        device: "wasm",
        dtype: DTYPES.fp32.dtype,
      });
    } else {
      throw e;
    }
  }

  const loadMs = Math.round(performance.now() - t0);
  session = {
    loaded: true,
    device,
    dtype: dtypeKey,
    dtypeLabel: spec.label,
    loadMs,
    degraded,
    degradedReason: reason,
    extractor,
  };
  cache.set(key, session);
  onProgress(`模型就绪（${device} / ${spec.label}），耗时 ${loadMs}ms`);
  return session;
}

/** 是否已经能把向量交给后端（128 · 384 维对齐检查在服务端做，这里只查局部状态） */
export function ready() {
  return session.loaded && !!session.extractor;
}

/**
 * 计算句向量。
 * @param {string[]} texts
 * @returns {Promise<number[][]>} 已做 mean pooling + L2 normalize 的 384 维向量
 *
 * pooling=mean + normalize=true 是检索场景的标准做法：
 * 归一化之后余弦相似度等价于内积，向量可以直接用 cosine_distance 比较。
 */
export async function embed(texts) {
  if (!ready()) throw new Error("模型尚未加载");
  const list = Array.isArray(texts) ? texts : [texts];
  if (list.length === 0) return [];

  const out = await session.extractor(list, {
    pooling: "mean",
    normalize: true,
  });
  const vectors = out.tolist();
  // 及时释放底层张量，长文本批量时很关键
  if (typeof out.dispose === "function") out.dispose();
  return vectors;
}

/** 单条文本 -> 向量 */
export async function embedOne(text) {
  return (await embed([text]))[0];
}

/** 余弦相似度（用于量化质量对比） */
export function similarity(a, b) {
  return cos_sim(a, b);
}

/**
 * 量化质量评估：同一批文本，用参考精度与待测精度各算一遍，
 * 返回逐条余弦相似度。这是"量化有没有伤效果"的直接证据。
 */
export async function compareQuality(texts, refKey = "fp32", testKey = "int8", onProgress = () => {}) {
  const refSpec = DTYPES[refKey];
  const testSpec = DTYPES[testKey];

  // 记住进场时的会话：对比要临时换 dtype，跑完必须换回来，
  // 否则用户点了"质量对比"之后.topbar 显示的精度就悄悄变成被测的那个了。
  const prev = { loaded: session.loaded, dtype: session.dtype, device: session.device };

  try {
    // 参考精度：始终跑在 WASM 上，保证两次对比的基线一致且可用
    onProgress(`计算参考精度 ${refSpec.label} ...`);
    await loadModel(refKey, "wasm", onProgress);
    const refVecs = await embed(texts);

    onProgress(`计算待测精度 ${testSpec.label} ...`);
    await loadModel(testKey, "wasm", onProgress);
    const testVecs = await embed(texts);

    return texts.map((t, i) => ({
      text: t,
      sim: Number(similarity(refVecs[i], testVecs[i]).toFixed(6)),
    }));
  } finally {
    if (prev.loaded) {
      // session.dtype 现在直接就是 DTYPES 的 key
      const prevKey = DTYPES[prev.dtype] ? prev.dtype : "fp32";
      const prevDevice = prev.device === "webgpu" ? "webgpu" : "wasm";
      try {
        await loadModel(prevKey, prevDevice, () => {});
      } catch (e) {
        onProgress(`（提示）恢复到原精度 ${prev?.dtype} 失败：${e.message}`);
      }
    }
  }
}

/**
 * 读取各量化变体的真实文件体积。
 *
 * 两条路径：
 *  1. **本地优先** —— 权重已经躺在 web/public/models 下时，静态服务器会直接提供，
 *     用 HEAD 读 Content-Length 即可，离线也能出数。这是比赛现场/内网的正确行为。
 *  2. 远程兜底 —— 本地没有才问 HuggingFace。用 /api/models 而不是 HEAD：
 *     HF 的 resolve 接口对浏览器 HEAD 的 CORS 支持不稳定，而 API 明确带 CORS 头。
 *
 * @returns {Promise<{sizes:Record<string,number>, source:"local"|"remote"}>}
 */
export async function fetchModelSizes() {
  const sizes = {};
  const prefix = `${env.localModelPath.replace(/\/+$/, "")}/${MODEL_ID}/`;

  // ---- 1. sizes.json：构建时固化下来的真实磁盘字节数（最可靠） ----
  //      tools/record-model-sizes.mjs 生成。不能依赖 HTTP 的 Content-Length：
  //      静态服务器开 gzip 时它给的是压缩后大小（实测 INT8 被报成 16.32MB，真实 21.96MB）。
  try {
    const res = await fetch(`${prefix}sizes.json`, { cache: "no-store" });
    if (res.ok) {
      const json = await res.json();
      if (json?.files && Object.keys(json.files).length > 0) {
        for (const [k, v] of Object.entries(json.files)) sizes[k] = v;
        return { sizes, source: "local-sizes.json" };
      }
    }
  } catch {
    /* 没有 sizes.json，继续走下面的兜底 */
  }

  // ---- 2. 本地静态资源：HEAD 读 Content-Length，不行再用 Range 取总长 ----
  await Promise.all(
    Object.values(DTYPES).map(async (spec) => {
      const url = `${prefix}${spec.file}`;
      try {
        let res = await fetch(url, { method: "HEAD" });
        let len = res.ok ? res.headers.get("content-length") : null;
        if (!len) {
          res = await fetch(url, { headers: { Range: "bytes=0-0" } });
          const cr = res.headers.get("content-range");
          if (cr) {
            const m = /\/(\d+)$/.exec(cr.trim());
            if (m) len = m[1];
          } else if (res.ok) {
            len = res.headers.get("content-length");
          }
        }
        if (len) sizes[spec.file] = Number(len);
      } catch {
        // 本地没有这个文件，交给下面的远程分支
      }
    })
  );
  if (Object.keys(sizes).length > 0) return { sizes, source: "local-http" };

  // ---- 2. 远程兜底 ----
  const host = MIRROR || "https://huggingface.co";
  const url = `${host}/api/models/${MODEL_ID}/tree/main/onnx`;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`读取模型文件列表失败: HTTP ${res.status}`);
  const json = await res.json();
  for (const item of json) {
    if (item.type === "file") sizes[`onnx/${item.path}`] = item.size ?? item.lfs?.size ?? 0;
  }
  return { sizes, source: "remote" };
}
