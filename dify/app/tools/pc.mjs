/**
 * pc.mjs —— PowerContext HTTP API 的最小封装
 *
 * 只做三件事：建 scope、写记忆、搜记忆。
 * 之所以单独抽出来，是因为「初始化数据」和「验证样例」都要用它，
 * 而 Dify 里那套 HTTP 节点也得打同样的接口——两边必须对得上。
 *
 * 用法：
 *   PC_BASE=http://127.0.0.1:8000 node tools/pc.mjs search "盐罐"
 */

export const PC_BASE = process.env.PC_BASE || "http://127.0.0.1:8000";
const TOKEN = process.env.POWERCONTEXT_SERVER_AUTH_TOKEN || "";

function headers() {
  const h = { "Content-Type": "application/json" };
  if (TOKEN) h.Authorization = `Bearer ${TOKEN}`;
  return h;
}

async function call(path, init = {}) {
  const res = await fetch(`${PC_BASE}${path}`, { ...init, headers: headers() });
  const text = await res.text();
  let json;
  try {
    json = text ? JSON.parse(text) : {};
  } catch {
    json = { raw: text };
  }
  if (!res.ok) {
    throw new Error(`${path} -> HTTP ${res.status} ${text.slice(0, 300)}`);
  }
  return json;
}

/** 当前默认 scope（Server 启动时自动创建的那个） */
export async function defaultScope() {
  return call("/v1/scopes/default");
}

/**
 * 建一个专用 scope。idempotency_key 固定，重复调用返回同一个 scope，
 * 所以初始化脚本可以反复跑而不会堆出一堆 scope。
 */
export async function ensureScope({ title, summary, idempotencyKey }) {
  try {
    const created = await call("/v1/scopes", {
      method: "POST",
      body: JSON.stringify({ title, summary, idempotency_key: idempotencyKey }),
    });
    return created;
  } catch (e) {
    // 409 = 这个 idempotency_key 已经建过了，从列表里找出同名的那个
    if (!/HTTP 409/.test(e.message)) throw e;
    const list = await call("/v1/scopes?limit=50");
    const hit = (list.items || []).find((s) => s.title === title);
    if (!hit) throw new Error(`scope 冲突但找不到同名 scope: ${title}`);
    return hit;
  }
}

/** 写一条显式记忆 */
export async function remember(scopeId, text, kind = "episode", reason = null) {
  return call("/v1/memory/remember", {
    method: "POST",
    body: JSON.stringify({ scope_id: scopeId, kind, text, reason }),
  });
}

/** 搜记忆；mode=auto 时无 embedding 配置会自动走全文 */
export async function search(scopeId, query, limit = 6) {
  return call("/v1/memory/search", {
    method: "POST",
    body: JSON.stringify({ scope_id: scopeId, query, limit, mode: "auto" }),
  });
}

/** 列出全部记忆条目（用于核对与截图） */
export async function listEntries(scopeId, limit = 50) {
  return call("/v1/memory/entries/list", {
    method: "POST",
    body: JSON.stringify({ scope_id: scopeId, limit }),
  });
}

// 注意：用 node -e / --input-type=module 调用时 process.argv[1] 是 undefined，
// 直接 .replace 会把「作为库被 import」的场景也搞崩。这里先做空值保护。
const invokedDirectly = (() => {
  const a = process.argv?.[1];
  if (!a) return false;
  return import.meta.url === `file://${a.replace(/\\/g, "/")}`;
})();

if (invokedDirectly) {
  const [cmd, ...rest] = process.argv.slice(2);
  const scopeId = process.env.PC_SCOPE || (await defaultScope()).scope_id;
  if (cmd === "search") {
    const r = await search(scopeId, rest.join(" "), 5);
    console.log(JSON.stringify(r, null, 2));
  } else if (cmd === "remember") {
    const r = await remember(scopeId, rest.join(" "), "episode");
    console.log(JSON.stringify(r, null, 2));
  } else if (cmd === "list") {
    console.log(JSON.stringify(await listEntries(scopeId), null, 2));
  } else {
    console.log(`用法: node tools/pc.mjs <search|remember|list> [文本]\nscope=${scopeId}`);
  }
}
