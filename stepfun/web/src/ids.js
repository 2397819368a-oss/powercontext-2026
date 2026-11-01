/**
 * ids.js —— 由文本内容派生稳定的文档 id
 *
 * 为什么不能用 `doc-${Date.now()}-${i}`：
 * 每点一次"建索引"都会生成一批全新 id，同一条文本就在库里堆出多份，
 * 检索结果里会出现三条一模一样的记录（实测踩过：8 条示例涨到 28 条）。
 * 用内容哈希后，id 与内容一一对应，重复建索引等价于"覆盖更新"，
 * 这也才符合"索引"这个动作应有的语义。
 */

/** @param {string} text @returns {Promise<string>} */
export async function contentId(text) {
  const data = new TextEncoder().encode(text.trim());

  // crypto.subtle 只在安全上下文可用（https 或 localhost，本项目都满足）
  if (globalThis.crypto?.subtle) {
    const buf = await crypto.subtle.digest("SHA-256", data);
    const hex = [...new Uint8Array(buf)]
      .slice(0, 16)
      .map((b) => b.toString(16).padStart(2, "0"))
      .join("");
    return `doc-${hex}`;
  }

  // 后备：FNV-1a。仅在非安全上下文出现（例如用局域网 IP 打开页面）时才会走到，
  // 冲突概率高于 SHA-256，但足以保证同一次演示内不重复。
  let h = 0x811c9dc5;
  for (const b of data) {
    h ^= b;
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return `doc-${h.toString(16).padStart(8, "0")}`;
}

/** 批量版本，保持与输入同序 */
export function contentIds(texts) {
  return Promise.all(texts.map(contentId));
}
