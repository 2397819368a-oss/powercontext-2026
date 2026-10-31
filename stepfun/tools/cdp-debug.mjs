/**
 * cdp-debug.mjs —— 一次性诊断：看页面到底跑没跑 JS、控制台有没有报错
 * 用法: node tools/cdp-debug.mjs <url> [waitMs]
 */
import { spawn } from "node:child_process";

const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const PORT = 9500 + (process.pid % 100);
const URL_TARGET = process.argv[2] || "http://127.0.0.1:5174/";
const WAIT = Number(process.argv[3] || 20000);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const child = spawn(
  EDGE,
  [
    `--remote-debugging-port=${PORT}`,
    "--headless=new",
    "--disable-gpu",
    "--no-first-run",
    `--user-data-dir=C:\\Temp\\edge-dbg-${PORT}`,
    "about:blank",
  ],
  { stdio: "ignore" }
);

try {
  let ok = false;
  for (let i = 0; i < 80 && !ok; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${PORT}/json/version`, { signal: AbortSignal.timeout(1500) });
      ok = r.ok;
    } catch {}
    if (!ok) await sleep(300);
  }
  if (!ok) throw new Error("调试端口未就绪");

  const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
  const page = list.find((t) => t.type === "page");
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((res, rej) => {
    ws.addEventListener("open", res, { once: true });
    ws.addEventListener("error", rej, { once: true });
  });

  let id = 0;
  const pending = new Map();
  ws.addEventListener("message", (ev) => {
    let m;
    try {
      m = JSON.parse(ev.data);
    } catch {
      return;
    }
    // 打印所有控制台输出与未捕获异常
    if (m.method === "Runtime.consoleAPICalled") {
      const txt = (m.params.args || [])
        .map((a) => a.value ?? a.description ?? a.type)
        .join(" ");
      console.log(`[console.${m.params.type}] ${txt}`);
    }
    if (m.method === "Runtime.exceptionThrown") {
      const d = m.params.exceptionDetails;
      console.log(`[EXCEPTION] ${d.text} ${d.exception?.description?.split("\n")[0] ?? ""}`);
    }
    if (m.method === "Log.entryAdded") {
      console.log(`[log.${m.params.entry.level}] ${m.params.entry.text} :: ${m.params.entry.url ?? ""}`);
    }
    if (m.id && pending.has(m.id)) {
      pending.get(m.id)(m);
      pending.delete(m.id);
    }
  });

  const send = (method, params = {}) =>
    new Promise((resolve, reject) => {
      const mid = ++id;
      const t = setTimeout(() => reject(new Error(`timeout ${method}`)), 30000);
      pending.set(mid, (m) => {
        clearTimeout(t);
        resolve(m);
      });
      ws.send(JSON.stringify({ id: mid, method, params }));
    });

  await send("Page.enable");
  await send("Runtime.enable");
  await send("Log.enable");
  await send("Page.navigate", { url: URL_TARGET });
  console.log(`已导航到 ${URL_TARGET}，等待 ${WAIT}ms ...\n`);
  await sleep(WAIT);

  const probes = {
    href: "location.href",
    readyState: "document.readyState",
    autoState: "JSON.stringify(window.__AUTO_STATE__ || null)",
    logCount: "document.querySelectorAll('.log-line').length",
    logTexts: "JSON.stringify([...document.querySelectorAll('.log-line')].map(e=>e.textContent).slice(0,20))",
    chipDb: "document.getElementById('chip-db')?.textContent",
    rtCount: "document.getElementById('rt-count')?.textContent",
    scriptTags: "document.querySelectorAll('script').length",
  };
  for (const [k, expr] of Object.entries(probes)) {
    const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true });
    const val = r.result?.value ?? r.result?.description ?? JSON.stringify(r.result);
    const exc = r.result?.exceptionDetails ? ` !! ${r.result.exceptionDetails.text}` : "";
    console.log(`${k.padEnd(12)} = ${String(val).slice(0, 600)}${exc}`);
  }

  // 额外表达式：node cdp-debug.mjs <url> <waitMs> "expr1" "expr2" ...
  for (const expr of process.argv.slice(4)) {
    const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true });
    const exc = r.result?.exceptionDetails ? ` !! ${r.result.exceptionDetails.text} ${r.result.exceptionDetails.exception?.description ?? ""}` : "";
    console.log(`> ${expr.slice(0, 60)}...\n  = ${String(r.result?.value ?? r.result?.description ?? JSON.stringify(r.result)).slice(0, 800)}${exc}`);
  }
  ws.close();
} finally {
  child.kill();
}
process.exit(0);
