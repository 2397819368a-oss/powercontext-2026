/**
 * simulate.mjs —— 与 Dify Chatflow 完全等价的一轮对话（本地可跑）
 *
 * 为什么要有这个文件：
 * 1. Dify 的流程一旦写死在 DSL 里，调试成本很高。先在纯 Node 里把
 *    「召回 → 拼 prompt → 生成 → 解析 → 写回」跑通，DSL 只是把它搬到节点里。
 * 2. 它同时是**离线验证证据**：不装 Dify 也能证明记忆链路是通的。
 *
 * 每一步的输入输出都打印出来，方便和 Dify 运行日志对照。
 *
 * 用法：
 *   node tools/simulate.mjs "老板，来碗酒，先记着"
 *   node tools/simulate.mjs --reset          # 只新建一个干净 scope
 *   LLM_BASE=http://127.0.0.1:11434/v1 LLM_MODEL=qwen2.5:3b node tools/simulate.mjs "..."
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { search, remember, ensureScope } from "./pc.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SCOPE_FILE = path.join(HERE, "..", ".scope");

const LLM_BASE = process.env.LLM_BASE || "http://127.0.0.1:8088/v1";
const LLM_MODEL = process.env.LLM_MODEL || "qwen2.5-3b-instruct";
const LLM_KEY = process.env.LLM_KEY || "local";
const PLAYER = process.env.PLAYER || "阿七";

const TRUST_MIN = -5;
const TRUST_MAX = 5;

/* ------------------------------ ① 召回 ------------------------------ */

/** 去掉 seed 标记，只把内容喂给模型 */
const clean = (t) => t.replace(/^\[seed:[^\]]+\]\s*/, "");

function formatMemories(hits) {
  if (!hits?.length) return "（这次什么都没想起来）";
  return hits
    .map((h, i) => {
      const kind = h.memory?.kind ?? "?";
      return `${i + 1}. [${kind}] ${clean(h.memory?.text ?? "")}`;
    })
    .join("\n");
}

/* ------------------------------ ② prompt ------------------------------ */

function systemPrompt({ memories, trust, player }) {
  return `你是「老陈」，乌云镇唯一一家酒馆「漏雨酒馆」的老板，52 岁，早年走镖，右手背有一道刀疤。
你说话短、爱用比喻、认账不认脸。你不讨好客人，也不无故刁难——你按账本说话。

【你记得的事】（从你的账本里翻出来的，按相关度排序，可能有不相干的）
${memories}

【当前关系】对 ${player} 的信任度：${trust}（范围 -5 到 5）
【在场的人】旅人${player}

写作规则：
1. 你必须至少引用一条上面【你记得的事】里的具体信息（具体到人名、物件、钱、次数），
   不要泛泛地说"我记得你"。引不出具体的，就承认你记不清，绝不许编。
2. 态度必须与信任度一致：<= -2 冷淡、话里带刺；-1 到 1 公事公办；>= 2 熟络、会多说一句。
3. reply 写 2-4 句，口语，短句。不要书面腔，不要解释设定，不要括号里的动作描写。
4. trust 是本轮之后的新信任度，只能在上一个值基础上动 1 格（-5 到 5 之间的整数）。
5. memory 必须包含玩家名「${player}」和一个具体物件或金额。

只输出一个 JSON 对象，不要 markdown 代码块，不要任何解释文字：
{"reply": "你的台词", "trust": 0, "memory": "第三人称一句话，记录这一轮发生了什么"}`;
}

/**
 * 模型输出的结构约束。
 *
 * 为什么要这个：3B 级别的小模型对「请按 <标签> 输出」这类指令几乎不服从，
 * 实测会直接吐一段台词、把 few-shot 示例照抄成答案。
 * 用 llama.cpp 的 response_format.schema（内部转 GBNF 语法）在解码层强制形状，
 * 模型只能吐合法 JSON，字段和取值范围都被焊死——这比在 prompt 里反复恳求可靠得多。
 */
const TURN_SCHEMA = {
  type: "object",
  properties: {
    reply: { type: "string" },
    trust: { type: "integer", minimum: TRUST_MIN, maximum: TRUST_MAX },
    memory: { type: "string" },
  },
  required: ["reply", "trust", "memory"],
  additionalProperties: false,
};

async function postLLM(body) {
  return fetch(`${LLM_BASE}/chat/completions`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${LLM_KEY}` },
    body: JSON.stringify(body),
  });
}

/* ------------------------------ ③ 生成 ------------------------------ */

async function callLLM(messages) {
  const base = { model: LLM_MODEL, messages, temperature: 0.7, max_tokens: 500 };

  let res = await postLLM({ ...base, response_format: { type: "json_object", schema: TURN_SCHEMA } });
  // 换一个不支持 schema 的 OpenAI 兼容后端时，降级成普通 json_object 再试一次
  if (res.status === 400) {
    res = await postLLM({ ...base, response_format: { type: "json_object" } });
  }
  if (!res.ok) throw new Error(`LLM ${res.status} ${(await res.text()).slice(0, 200)}`);
  const j = await res.json();
  return j.choices?.[0]?.message?.content ?? "";
}

/* ------------------------------ ④ 解析 ------------------------------ */

function parseTurn(raw) {
  const clamp = (n) => Math.max(TRUST_MIN, Math.min(TRUST_MAX, n));

  // 首选：JSON（正常情况下 schema 已保证形状）
  const tryJSON = (s) => {
    try {
      const o = JSON.parse(s);
      return o && typeof o === "object" ? o : null;
    } catch {
      return null;
    }
  };
  let obj = tryJSON(raw.trim().replace(/^```(?:json)?\s*/i, "").replace(/```\s*$/, ""));
  if (!obj) {
    const m = /\{[\s\S]*\}/.exec(raw);
    if (m) obj = tryJSON(m[0]);
  }
  if (obj && typeof obj.reply === "string" && obj.reply.trim()) {
    const n = Number.parseInt(String(obj.trust ?? "").replace(/[^\d-]/g, ""), 10);
    return {
      reply: obj.reply.trim(),
      trust: Number.isFinite(n) ? clamp(n) : null,
      memory: String(obj.memory ?? "").trim(),
      parsed: true,
      format: "json",
    };
  }

  // 兜底：模型没走 JSON 时，按尖括号标签或中文前缀行取
  const pick = (tag) => {
    const m = new RegExp(`<${tag}>([\\s\\S]*?)</${tag}>`).exec(raw);
    return m ? m[1].trim() : "";
  };
  const pickLine = (labels) => {
    for (const l of labels) {
      const m = new RegExp(`${l}\\s*[：:]\\s*(.+)`).exec(raw);
      if (m) return m[1].trim();
    }
    return "";
  };

  let reply = pick("回应");
  let trustRaw = pick("信任度");
  let memory = pick("记住");

  if (!reply) reply = pickLine(["回应", "你的台词", "台词", "老陈"]);
  if (!trustRaw) trustRaw = pickLine(["信任度", "信任"]);
  if (!memory) memory = pickLine(["记住", "记下", "记录"]);

  if (reply) reply = reply.split(/信任度\s*[：:]|记住\s*[：:]/)[0].trim();

  const n = Number.parseInt(String(trustRaw).replace(/[^\d-]/g, ""), 10);
  return {
    reply,
    trust: Number.isFinite(n) ? clamp(n) : null,
    memory,
    parsed: Boolean(reply),
    format: "tag-fallback",
  };
}

/* ------------------------------ 主流程 ------------------------------ */

async function main() {
  const args = process.argv.slice(2);

  if (args.includes("--reset")) {
    const scope = await ensureScope({
      title: `记仇酒馆 · 试玩存档 ${new Date().toISOString().slice(0, 16).replace(/[:T]/g, "")}`,
      summary: "simulate.mjs 新建的临时存档，用于反复试玩。",
      idempotencyKey: `grudge-tavern-sim-${Date.now()}`,
    });
    fs.writeFileSync(SCOPE_FILE, scope.scope_id + "\n", "utf8");
    console.log(`新存档 scope = ${scope.scope_id}（已写入 .scope）`);
    return;
  }

  const userText = args.join(" ").trim();
  if (!userText) {
    console.log('用法: node tools/simulate.mjs "老板，来碗酒"');
    return;
  }

  const scopeId = fs.readFileSync(SCOPE_FILE, "utf8").trim();
  const trust = Number(process.env.TRUST ?? 0);

  console.log(`\n【玩家】${userText}`);

  // ① 召回
  const recalled = await search(scopeId, userText, 5);
  console.log(`\n① 召回 ${recalled.hits?.length ?? 0} 条（mode=${recalled.mode}）`);
  for (const h of recalled.hits ?? []) {
    console.log(`   · ${clean(h.memory?.text ?? "").slice(0, 60)}…  score=${h.score?.toFixed(4)}`);
  }

  // ② 拼 prompt
  const messages = [
    { role: "system", content: systemPrompt({ memories: formatMemories(recalled.hits), trust, player: PLAYER }) },
    { role: "user", content: userText },
  ];

  // ③ 生成
  console.log(`\n② 调用模型 ${LLM_MODEL} …`);
  const raw = await callLLM(messages);

  // ④ 解析
  const turn = parseTurn(raw);
  console.log(`\n③ 解析 ${turn.parsed ? `成功（format=${turn.format}）` : "失败（模型没按格式输出）"}`);
  console.log(`\n【老陈】${turn.reply || raw.slice(0, 200)}`);
  console.log(`\n信任度：${trust} → ${turn.trust ?? "（未给出）"}`);

  // ⑤ 写回
  if (turn.memory) {
    const line = `${PLAYER}：${userText} → 老陈的反应：${turn.reply.slice(0, 80)}；记下：${turn.memory}；信任度变为 ${turn.trust ?? trust}`;
    await remember(scopeId, line, "episode", "记仇酒馆：一轮互动");
    console.log(`\n④ 已写回记忆：${line.slice(0, 80)}…`);
  } else {
    console.log("\n④ 模型没给「记住」，本轮不写回（避免存垃圾）");
  }

  console.log(`\n【记忆回响】本轮引用了 ${recalled.hits?.length ?? 0} 条旧记忆`);
}

main().catch((e) => {
  console.error(`失败: ${e.message}`);
  process.exit(1);
});
