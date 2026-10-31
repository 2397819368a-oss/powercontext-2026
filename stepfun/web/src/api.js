/**
 * api.js —— 与服务端通信的唯一出口
 *
 * 约定：所有写库的向量都由这里发出，且来源只有浏览器端的 embed()。
 * 服务端本身不具备产生向量的能力（它会拒绝无 embedding 的写入）。
 */

const BASE = import.meta.env.VITE_API_BASE || "http://127.0.0.1:8787";

async function req(path, options) {
  const res = await fetch(`${BASE}${path}`, options);
  const json = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(json.error || `HTTP ${res.status}`);
  return json;
}

export function health() {
  return req("/api/health");
}

export function stats() {
  return req("/api/stats");
}

/** @param {{id:string,content:string,embedding:number[],meta?:object}[]} items */
export function ingest(items) {
  return req("/api/ingest", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ items }),
  });
}

export function clearDocs() {
  return req("/api/docs", { method: "DELETE" });
}

/**
 * @param {{embedding?:number[], text?:string, topK?:number}} q
 * 传 embedding+text -> 混合；只传 embedding -> 向量；只传 text -> 全文
 */
export function search(q) {
  return req("/api/search", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(q),
  });
}
