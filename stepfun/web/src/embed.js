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

import { pipeline, cos_sim, env } from "@xenova/transformers";

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
 * 本地离线模型优先。
 *
 * 为什么必须有这条：Transformer.js 默认要联网从 HF 拉权重，
 * 一旦身处离线环境（内网/比赛现场/墙）页面就永远卡在"加载模型"。
 * 把权重放进 web/public/models/ 后，这里的 allowLocalModels 会让它直接读本地文件，
 * 再也不需要外网。tools/fetch-model.mjs 就是用来准备这份离线权重的。
 */
env.localModelPath = import.meta.env?.VITE_LOCAL_MODEL_PATH || "/models/";
env.allowLocalModels = true;
env.allowRemoteModels = true; // 本地没有才回落到远程

/** 三种精度：面向用户展示的名字 -> Transformers.js 的 dtype 取值 + 对应权重文件 */
export const DTYPES = {
  fp32: { label: "FP32（原始）", dtype: "fp32", file: "onnx/model.onnx" },
  fp16: { label: "FP16（半精度）", dtype: "fp16", file: "onnx/model_fp16.onnx" },
  int8: { label: "INT8（量化）", dtype: "q8", file: "onnx/model_quantized.onnx" },
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

  const key = `${device}:${spec.dtype}`;
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
      dtype: spec.dtype,
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
        dtype: "fp32",
      });
    } else {
      throw e;
    }
  }

  const loadMs = Math.round(performance.now() - t0);
  session = {
    loaded: true,
    device,
    dtype: key.split(":")[1],
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
}

/**
 * 从 HuggingFace 读取各量化变体的真实文件体积。
 * 走 API 而不是 HEAD 请求：hf 的 resolve 接口对浏览器 HEAD 的 CORS 支持不稳定，
 * 而 /api/models 明确带 CORS 头。拿到的是仓库里记录的 size（字节）。
 */
export async function fetchModelSizes() {
  const host = MIRROR || "https://huggingface.co";
  const url = `${host}/api/models/${MODEL_ID}/tree/main/onnx`;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`读取模型文件列表失败: HTTP ${res.status}`);
  const json = await res.json();
  const map = {};
  for (const item of json) {
    if (item.type === "file") map[`onnx/${item.path}`] = item.size ?? item.lfs?.size ?? 0;
  }
  return map;
}
