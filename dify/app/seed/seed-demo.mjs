/**
 * seed-demo.mjs —— 给「截图/录像」用的干净演示账本
 *
 * 为什么不用 seed.mjs 那个 scope：
 *   调提示词、跑 smoke 会往里写一堆「smoke-tester」的测试条目，
 *   最后截图时「记忆回响」会露出这些脏数据，观感和可信度都打折。
 *   这里单独建一个 scope（idempotency_key 固定），演示前重跑一次即可重置到同一份初始状态。
 *
 * 用法：
 *   node seed/seed-demo.mjs            # 重置到初始状态（幂等，只补不删）
 *   node seed/seed-demo.mjs --fresh    # 换一个新 key 的干净 scope（旧的还在，不影响）
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { ensureScope, remember, listEntries, search, PC_BASE } from "../tools/pc.mjs";
import { SEED, TAG } from "./world.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SCOPE_FILE = path.join(HERE, "..", ".scope");

const KEY = process.argv.includes("--fresh")
  ? `grudge-tavern-demo-${Date.now()}`
  : "grudge-tavern-demo-v1";

async function main() {

  const scope = await ensureScope({
    title: "记仇酒馆 · 演示账本",
    summary:
      "演示用干净账本。存着酒馆的规矩、镇上的传闻，以及旅人阿七和老陈之间发生过的每一件事。",
    idempotencyKey: KEY,
  });
  const scopeId = scope.scope_id;
  console.log(`scope: ${scopeId}  (${scope.title})`);

  const cur = await listEntries(scopeId, 100);
  const items = cur.items || cur.entries || [];
  const have = new Set(items.map((e) => (e.text || "").match(/^\[seed:([^\]]+)\]/)?.[1]).filter(Boolean));
  console.log(`已有 seed 条目: ${have.size} / ${SEED.length}`);

  for (const s of SEED) {
    if (have.has(s.id)) continue;
    await remember(scopeId, `${TAG(s.id)} ${s.text}`, s.kind, "演示账本初始化");
    console.log(`  write ${s.id} (${s.kind})`);
  }

  const after = await listEntries(scopeId, 100);
  const total = (after.items || after.entries || []).length;
  console.log(`\n核对：scope 内共 ${total} 条记忆`);

  for (const q of ["规矩 赊账", "生客 先付钱", "算盘 猫", "阿七 醋 账"]) {
    const r = await search(scopeId, q, 3);
    console.log(`  检索「${q}」-> ${r.hits?.length ?? 0} 条 (mode=${r.mode ?? "-"})`);
  }

  fs.writeFileSync(SCOPE_FILE, scopeId + "\n", "utf8");
  console.log(`\nscope_id 已写入 ${SCOPE_FILE}`);
}

main().catch((e) => {
  console.error(`失败: ${e.message}`);
  process.exit(1);
});
