/**
 * seed.mjs —— 「记仇酒馆」的初始化数据
 *
 * 为什么需要这一步：PowerContext 的记忆不是凭空来的。
 * 一个"记得你"的 NPC，前提是酒馆本身有底子——规矩、传闻、老板的旧账。
 * 这些预置记忆让玩家第一句话就能问出东西，也让老陈一开口就有分量。
 *
 * 幂等性：scope 用固定 idempotency_key 创建；记忆文本前带 [seed:编号] 标记，
 * 重复执行时先查已有条目，已存在就跳过，不会越跑越多。
 *
 * 用法：
 *   node seed/seed.mjs            # 初始化（可重复执行）
 *   node seed/seed.mjs --verify   # 只核对，不写入
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { ensureScope, remember, listEntries, search, PC_BASE } from "../tools/pc.mjs";
import { SEED, TAG } from "./world.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SCOPE_FILE = path.join(HERE, "..", ".scope");

const TAG_IDS = new Set(SEED.map((s) => s.id));

async function main() {
  const verifyOnly = process.argv.includes("--verify");
  console.log(`PowerContext: ${PC_BASE}`);

  const scope = await ensureScope({
    title: "记仇酒馆 · 老陈的账本",
    summary:
      "一个只有一间酒馆的雨镇。老陈记得每个客人做过的事——这条 scope 存的是酒馆的规矩、镇上的传闻，以及旅人阿七和老陈之间发生过的每一件事。",
    idempotencyKey: "grudge-tavern-v1",
  });
  const scopeId = scope.scope_id;
  console.log(`scope: ${scopeId}  (${scope.title})`);
  fs.writeFileSync(SCOPE_FILE, scopeId + "\n", "utf8");

  const existing = await listEntries(scopeId, 100);
  const items = existing.items || existing.entries || [];
  const have = new Set(
    items
      .map((e) => (e.text || e.entry?.text || "").match(/^\[seed:([^\]]+)\]/)?.[1])
      .filter(Boolean)
  );
  console.log(`已有 seed 条目: ${have.size} / ${SEED.length}`);

  if (!verifyOnly) {
    for (const s of SEED) {
      if (have.has(s.id)) {
        console.log(`  skip  ${s.id}`);
        continue;
      }
      await remember(scopeId, `${TAG(s.id)} ${s.text}`, s.kind, "记仇酒馆初始化数据");
      console.log(`  write ${s.id} (${s.kind})`);
    }
  }

  // 核对：确认数据真的落进去了，并且能被检索到
  const after = await listEntries(scopeId, 100);
  const total = (after.items || after.entries || []).length;
  console.log(`\n核对：scope 内共 ${total} 条记忆`);

  const probes = ["规矩 赊账", "生客 先付钱", "算盘 猫", "阿七 醋"];
  for (const q of probes) {
    const r = await search(scopeId, q, 3);
    console.log(`  检索「${q}」-> ${r.hits?.length ?? 0} 条 (mode=${r.mode ?? "-"})`);
  }
  console.log(`\nscope_id 已写入 ${SCOPE_FILE}`);
}

main().catch((e) => {
  console.error(`失败: ${e.message}`);
  process.exit(1);
});
