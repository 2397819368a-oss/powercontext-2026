/**
 * dify-setup-model.mjs —— 装模型插件 + 注册本地 llama.cpp 模型
 *
 * 为什么单独一个脚本：这两步是 Dify 里最容易「点错」的地方，
 * 参数名必须和插件的 provider schema 完全一致，手点容易漏。
 * 脚本化之后，换机器重跑就能复现同样的模型配置。
 *
 * 用法：
 *   node tools/dify-setup-model.mjs
 *   LLM_BASE_URL=http://host.docker.internal:8088/v1 node tools/dify-setup-model.mjs
 */

const BASE = process.env.DIFY_BASE || "http://127.0.0.1:8090";
const EMAIL = process.env.DIFY_EMAIL || "admin@grudge.local";
const PASSWORD = process.env.DIFY_PASSWORD || "GrudgeTavern2026!";
const PROVIDER = "langgenius/openai_api_compatible/openai_api_compatible";
const PLUGIN_ID = "langgenius/openai_api_compatible:0.0.73@4cf123ffaa4556f079bed6c47f3a462cc825504b859b183239e74eb19136a4b5";
const MODEL = process.env.LLM_MODEL || "qwen2.5-3b-instruct";
const ENDPOINT = process.env.LLM_BASE_URL || "http://host.docker.internal:8088/v1";

let CSRF = "";
let COOKIE = "";

async function j(method, p, body, token) {
  const headers = { "Content-Type": "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;
  if (COOKIE) headers.Cookie = COOKIE;
  if (CSRF) headers["X-CSRF-Token"] = CSRF;
  const res = await fetch(`${BASE}${p}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  let data;
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { raw: text };
  }
  return { status: res.status, data, text, res };
}

function pickCookie(res, name) {
  const list = typeof res.headers.getSetCookie === "function"
    ? res.headers.getSetCookie()
    : [res.headers.get("set-cookie") || ""];
  for (const c of list) {
    const m = new RegExp(`(?:^|,\\s*)((?:__Host-)?${name})=([^;]+)`).exec(c || "");
    if (m) return m[2];
  }
  return "";
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function login() {
  const b64 = Buffer.from(PASSWORD, "utf8").toString("base64");
  const r = await j("POST", "/console/api/login", { email: EMAIL, password: b64, remember_me: true });
  const token = r.data?.data?.access_token || pickCookie(r.res, "access_token");
  if (!token) throw new Error(`登录失败 ${r.status}: ${r.text.slice(0, 200)}`);
  CSRF = pickCookie(r.res, "csrf_token");
  COOKIE = `access_token=${token}; csrf_token=${CSRF}`;
  console.log(`登录 OK token=${token.slice(0, 10)}…`);
  return token;
}

async function main() {
  const token = await login();

  /* ① 装插件 */
  const inst = await j("POST", "/console/api/workspaces/current/plugin/install/marketplace",
    { plugin_unique_identifiers: [PLUGIN_ID] }, token);
  console.log(`① 安装插件: HTTP ${inst.status} ${JSON.stringify(inst.data).slice(0, 300)}`);
  if (inst.status >= 300) throw new Error("插件安装失败");

  /* ② 等安装完成 */
  for (let i = 0; i < 60; i++) {
    await sleep(3000);
    const list = await j("GET", "/console/api/workspaces/current/plugin/list?page=1&page_size=100", undefined, token);
    const plugins = list.data?.plugins || list.data?.data || [];
    const hit = plugins.find((p) => String(p.plugin_id || p.id || "").includes("openai_api_compatible"));
    if (hit) {
      const st = hit.install_status || hit.status || "";
      console.log(`② 插件状态: ${st || "已就绪"}  (${i + 1}/60)`);
      if (st === "installed" || st === "ready" || st === "") break;
      if (st === "install_failed") throw new Error("插件安装失败（install_failed）");
    } else if (i % 5 === 4) {
      console.log(`   等待中… (${i + 1}/60)`);
    }
  }

  /* ③ 注册模型 */
  const cred = {
    api_key: "local",
    endpoint_url: ENDPOINT,
    mode: "chat",
    context_size: "4096",
    max_tokens_to_sample: "512",
    function_calling_type: "no_call",
    stream_mode: "not_streaming",
    vision_support: "no_support",
  };
  const mk = await j("POST", `/console/api/workspaces/current/model-providers/${PROVIDER}/models/credentials`,
    { model: MODEL, model_type: "llm", credentials: cred, name: "local-llama" }, token);
  console.log(`③ 注册模型: HTTP ${mk.status} ${JSON.stringify(mk.data).slice(0, 400)}`);

  /* ④ 列出模型确认 */
  const models = await j("GET", `/console/api/workspaces/current/model-providers/${PROVIDER}/models`, undefined, token);
  const arr = models.data?.data || [];
  console.log(`④ 供应商下模型 ${arr.length} 个: ${arr.map((m) => `${m.model}(${m.model_type})`).join(", ") || "（空）"}`);

  /* ⑤ 设为系统默认 LLM */
  const def = await j("POST", "/console/api/workspaces/current/default-model",
    { model_settings: [{ model_type: "llm", provider: PROVIDER, model: MODEL }] }, token);
  console.log(`⑤ 设为默认模型: HTTP ${def.status} ${JSON.stringify(def.data).slice(0, 200)}`);
}

main().catch((e) => {
  console.error(`\n失败：${e.message}`);
  process.exit(1);
});
