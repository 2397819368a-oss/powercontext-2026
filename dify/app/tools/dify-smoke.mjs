/**
 * dify-smoke.mjs —— 「记仇酒馆」跨轮记忆的端到端验收脚本
 *
 * 为什么要写得比普通冒烟测试啰嗦：
 *   这类作品最容易被质疑的是「你到底真的记住了，还是根本是硬编码的台词」。
 *   所以这个脚本不只看「回答里有没有那个词」，而是把证据分三层捞出来：
 *     甲｜Service API 的回答本体          —— 用户真正看到的那句台词
 *     乙｜工作流节点原始输出（模型原始文本）—— 证明台词是模型当轮生成的
 *     丙｜PowerContext 里的条目增减        —— 证明记忆真的写进去了，并且下一轮能被翻出来
 *
 * 关键设计：第二轮提问故意**不提**"伞"字（只说"我是不是有东西落这儿了"），
 * 第三轮甚至换成全新会话（新 conversation_id）。只有真的去记忆库里翻，才答得出来。
 *
 * 用法：
 *   DIFY_BASE=http://127.0.0.1:8090 DIFY_KEY=app-xxxx \
 *   POWERCONTEXT_SERVER_AUTH_TOKEN=... node tools/dify-smoke.mjs
 *
 * 没有 DIFY_KEY / 管理员密码时，只做「直接打 PowerContext」的降级验证。
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { listEntries, search, PC_BASE } from "./pc.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(HERE, "..");
const SCOPE_FILE = path.join(ROOT, ".scope");

const DIFY_BASE = process.env.DIFY_BASE || "http://127.0.0.1:8090";
const DIFY_KEY = process.env.DIFY_KEY || fs.existsSync(path.join(ROOT, ".difykey"))
  ? (process.env.DIFY_KEY || fs.readFileSync(path.join(ROOT, ".difykey"), "utf8").trim())
  : "";
const USER = process.env.SMOKE_USER || "smoke-tester";
const SCOPE = process.env.PC_SCOPE || fs.readFileSync(SCOPE_FILE, "utf8").trim();

/* 刻意用一件**种子记忆里没有**的物件，避免「明明是世界设定里本来就有的」这种假命中 */
const MARKER = "蓝色油纸伞";

let pass = 0;
let fail = 0;
function check(name, ok, extra = "") {
  if (ok) {
    pass++;
    console.log(`  ✅ ${name}${extra ? ` — ${extra}` : ""}`);
  } else {
    fail++;
    console.log(`  ❌ ${name}${extra ? ` — ${extra}` : ""}`);
  }
}

/* ---------------------- Dify Service API（玩家侧） ---------------------- */

/**
 * 为什么用 streaming 而不是 blocking：
 *   Service API 的 blocking 响应里没有 workflow_run_id，也没办法按 console 会话去查
 *   「已发布工作流」的运行（/workflows/published/runs/... 会 404）。
 *   streaming 事件流里则逐个节点给出 node_finished 的 inputs/outputs，
 *   这正是我们需要的审计证据：能直接看到「模型当轮生成的原始 JSON」和「幂等写回结果」。
 */
async function ask(query, conversationId = "") {
  const res = await fetch(`${DIFY_BASE}/v1/chat-messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${DIFY_KEY}` },
    body: JSON.stringify({
      inputs: {},
      query,
      response_mode: "streaming",
      conversation_id: conversationId,
      user: USER,
    }),
  });
  if (!res.ok) throw new Error(`Dify ${res.status}: ${(await res.text()).slice(0, 400)}`);
  const decoder = new TextDecoder();
  let buf = "";
  let answer = "";
  let seenConversationId = "";
  const nodes = {};
  for await (const chunk of res.body) {
    buf += decoder.decode(chunk, { stream: true });
    const parts = buf.split("\n\n");
    buf = parts.pop() ?? "";
    for (const part of parts) {
      // SSE 帧之间可能是 \r\n\r\n（nginx 转发后常见），直接按 \n\n 切会留下 \r 前缀，
      // 导致 startsWith("data: ") 全部失配 —— 症状是「回答永远是空的」。trim 掉前缀最稳。
      const line = part.trim();
      if (!line.startsWith("data: ")) continue;
      let event;
      try {
        event = JSON.parse(line.slice(6));
      } catch {
        continue;
      }
      // chat-messages 的流式事件是扁平的：answer / conversation_id 在**顶层**，
      // 不在 data 里（只有 node_finished 才把东西放在 data 下）。这两处位置不同，容易记混。
      if (event.event === "message" && typeof event.answer === "string") answer += event.answer;
      if (event.conversation_id) seenConversationId = event.conversation_id;
      if (event.event === "node_finished" && event.data?.node_id) {
        nodes[event.data.node_id] = event.data.outputs || {};
      }
    }
  }
  return { answer, nodes, conversation_id: seenConversationId };
}

/** 回答的格式是「台词\\n---\\n账本\\n记忆回响」，取分隔线之前的部分才是 NPC 真正说的话 */
function speechOf(answer) {
  return String(answer || "").split(/\n\s*---\s*\n/)[0].trim();
}

/* ---------------------- PowerContext 侧 ---------------------- */

async function countEntries() {
  const r = await listEntries(SCOPE, 100);
  return (r.items || r.entries || []).length;
}

/* ---------------------- 主流程 ---------------------- */

async function main() {
  console.log(`PowerContext : ${PC_BASE}`);
  console.log(`scope        : ${SCOPE}`);
  console.log(`Dify         : ${DIFY_BASE}`);
  console.log(`验证标记物   : ${MARKER}（种子记忆里没有，只能靠本轮写回）`);

  const before = await countEntries();
  console.log(`\n【甲】记忆库现有 ${before} 条`);

  if (!DIFY_KEY) {
    console.log("跳过：未提供 DIFY_KEY。仅做 PowerContext 直连验证。");
    const r = await search(SCOPE, "醋 酒钱", 5);
    check("PowerContext 可检索", (r.hits || []).length > 0, `${(r.hits || []).length} 条命中`);
    process.exit(fail ? 1 : 0);
  }


  /* -------- 第 1 轮：埋一个只在这一轮出现过的物件 -------- */
  const q1 = `老板，我把那把${MARKER}落在你柜台上了，明天来取。`;
  console.log(`【1】第 1 轮（同一会话）: ${q1}`);
  const r1 = await ask(q1);
  const s1 = speechOf(r1.answer);
  console.log(`    老陈：${s1}`);
  check("返回了台词", Boolean(s1), `${s1.length} 字`);
  check("生成了 conversation_id", Boolean(r1.conversation_id), r1.conversation_id);

  const snap1 = r1.nodes;
  if (snap1["4"]) console.log(`    检索模式=${snap1["4"].mode}  召回 ${snap1["4"].hit_count} 条  信任度基线 ${snap1["4"].prev_trust}`);
  if (snap1["5"]) console.log(`    模型原始输出: ${String(snap1["5"].text).slice(0, 200)}`);
  if (snap1["6"]) console.log(`    解析: ${snap1["6"].parse_mode}  信任度 ${snap1["6"].trust} (${snap1["6"].trust_delta})`);
  if (snap1["7"] && snap1["7"].status_code !== 200) console.log(`    写回失败: ${snap1["7"].status_code}`);

  const mid = await countEntries();
  check("PowerContext 写入 +1", mid === before + 1, `${before} → ${mid}`);

  /* -------- 第 2 轮：同一会话，但**不提伞字** -------- */
  const q2 = "老板，我是不是有东西落这儿了？";
  console.log(`\n【2】第 2 轮（同一会话，不提「伞」字）: ${q2}`);
  const r2 = await ask(q2, r1.conversation_id);
  const s2 = speechOf(r2.answer);
  console.log(`    老陈：${s2}`);
  check("台词里提到了标记物", s2.includes(MARKER) || s2.includes("伞"), s2.slice(0, 60));
  check("回答带「记忆回响」区块", String(r2.answer).includes("记忆回响"));

  const n2 = r2.nodes["4"];
  if (n2) console.log(`    检索模式=${n2.mode}  召回 ${n2.hit_count} 条`);
  if (r2.nodes["6"]) console.log(`    解析: ${r2.nodes["6"].parse_mode}  信任度 ${r2.nodes["6"].trust}`);

  /* -------- 第 3 轮：新会话（等价于「隔天再来」） -------- */
  const q3 = "老板，我昨天落的东西你收好了吗？";
  console.log(`\n【3】第 3 轮（**新会话**）: ${q3}`);
  const r3 = await ask(q3, "");
  const s3 = speechOf(r3.answer);
  console.log(`    老陈：${s3}`);
  check("新会话仍能翻出旧账", s3.includes(MARKER) || s3.includes("伞"), s3.slice(0, 60));
  check("conversation_id 已刷新", r3.conversation_id !== r1.conversation_id, r3.conversation_id);

  /* -------- 汇总 -------- */
  const after = await countEntries();
  console.log(`\n【丙】记忆库 ${before} → ${after} 条（共 +${after - before}）`);
  check("三轮各写回一条", after - before === 3, `实际 +${after - before}`);

  const hit = await search(SCOPE, MARKER, 5);
  check(`${MARKER} 可被单独检索到`, (hit.hits || []).length > 0, `${(hit.hits || []).length} 条命中`);

  console.log(`\n结果：${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
}

main().catch((e) => {
  console.error(`\n失败：${e.message}`);
  process.exit(1);
});
