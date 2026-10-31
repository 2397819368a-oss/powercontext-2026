/**
 * cdp-run.mjs —— 用真实时间驱动无头 Edge 跑完页面异步流程并抓取结果
 *
 * 为什么非要这个脚本：
 *  1. `--dump-dom` 在 load 事件后就 dump，而本项目要「加载 90MB 权重 + WASM 推理」，
 *     真正的结果几十秒后才出现，dump 出来永远是半成品。
 *  2. `--virtual-time-budget` 会把 setTimeout 快进，页面里那些超时提示会被瞬间触发，
 *     同时虚拟时间不一定等真实 IO —— 用它跑出来的是假失败。
 *  3. 页面侧已经会在流程结束时挂 `window.__AUTO_STATE__`，这里轮询它，
 *     "跑完了没有"是确定的，不再靠猜时长。
 *
 * 用法：node tools/cdp-run.mjs <url> [timeoutMs]
 * 产物：tools/cdp-out/ 下的 dom.html / logs.txt / tables.json
 */
import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const OUT_DIR = path.join(__dirname, "cdp-out");
fs.mkdirSync(OUT_DIR, { recursive: true });

const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const PORT = 9333 + (process.pid % 200); // 避免并发时撞端口
const URL_TARGET = process.argv[2];
const TIMEOUT_MS = Number(process.argv[3] || 600000);

if (!URL_TARGET) {
  console.error("用法: node tools/cdp-run.mjs <url> [timeoutMs]");
  process.exit(2);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* ------------ 1. 启动带远程调试端口的 Edge ------------ */
const profile = `C:\\Temp\\edge-cdp-${PORT}`;
const child = spawn(
  EDGE,
  [
    `--remote-debugging-port=${PORT}`,
    "--headless=new",
    "--disable-gpu",
    "--no-first-run",
    "--no-default-browser-check",
    "--hide-scrollbars",
    "--window-size=1400,2600",
    `--user-data-dir=${profile}`,
    "about:blank",
  ],
  { stdio: "ignore" }
);

async function waitForPort() {
  for (let i = 0; i < 100; i++) {
    try {
      const res = await fetch(`http://127.0.0.1:${PORT}/json/version`, { signal: AbortSignal.timeout(2000) });
      if (res.ok) return await res.json();
    } catch {
      /* 还没起来 */
    }
    await sleep(300);
  }
  throw new Error("Edge 调试端口迟迟未就绪");
}

let ws;
try {
  const version = await waitForPort();
  console.log(`Edge: ${version.Browser}`);

  const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
  const page = list.find((t) => t.type === "page");
  if (!page) throw new Error("没找到 page target");

  /* ------------ 2. 建立 CDP 连接 ------------ */
  ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((res, rej) => {
    ws.addEventListener("open", res, { once: true });
    ws.addEventListener("error", rej, { once: true });
  });

  let msgId = 0;
  const pending = new Map();
  ws.addEventListener("message", (ev) => {
    let msg;
    try {
      msg = JSON.parse(ev.data);
    } catch {
      return;
    }
    if (msg.id && pending.has(msg.id)) {
      const { resolve, reject } = pending.get(msg.id);
      pending.delete(msg.id);
      if (msg.error) reject(new Error(`${msg.error.message} (${JSON.stringify(msg.error.data || "")})`));
      else resolve(msg);
    }
  });

  /** 发一条 CDP 命令并等它的响应 */
  const send = (method, params = {}, timeoutMs = 60000) =>
    new Promise((resolve, reject) => {
      const id = ++msgId;
      const timer = setTimeout(() => {
        if (pending.has(id)) {
          pending.delete(id);
          reject(new Error(`CDP 超时: ${method}`));
        }
      }, timeoutMs);
      pending.set(id, {
        resolve: (v) => {
          clearTimeout(timer);
          resolve(v);
        },
        reject: (e) => {
          clearTimeout(timer);
          reject(e);
        },
      });
      ws.send(JSON.stringify({ id, method, params }));
    });

  await send("Page.enable");
  await send("Runtime.enable");
  console.log(`打开 ${URL_TARGET}`);
  await send("Page.navigate", { url: URL_TARGET });
  await sleep(2500); // 让新文档开始执行

  /* ------------ 3. 轮询完成标志（真实挂钟时间） ------------ */
  /**
   * 注意 MCP/CDP 的响应是两层 result：
   *   { id, result: { result: { type:"string", value:"..." } } }
   * 外层 result 是命令返回值，内层 result 是 Runtime.evaluate 的 remote object。
   * 少解一层就会读到 undefined —— 曾经在这里踩过一次。
   */
  const val = (r) => r?.result?.result?.value;
  const excText = (r) => r?.result?.result?.exceptionDetails?.text || r?.error?.message || null;

  const t0 = Date.now();
  let state = null;
  for (;;) {
    const elapsed = Date.now() - t0;
    if (elapsed > TIMEOUT_MS) {
      console.log(`等待超时（${TIMEOUT_MS}ms）`);
      break;
    }
    try {
      const r = await send(
        "Runtime.evaluate",
        {
          expression: "JSON.stringify(window.__AUTO_STATE__ || null)",
          returnByValue: true,
        },
        15000
      );
      const raw = val(r);
      if (raw && raw !== "null") {
        state = JSON.parse(raw);
        console.log(`页面标记完成，耗时 ${((Date.now() - t0) / 1000).toFixed(1)}s，error=${state.error ?? "无"}`);
        break;
      }
      const e = excText(r);
      if (e) console.log(`  (evaluate 异常: ${e})`);
    } catch (e) {
      // 页面正在跳转/重编译时 evaluate 可能短暂失败，忽略继续轮询
      if (String(e.message).includes("CDP 超时")) console.log("  (evaluate 超时，继续等待)");
    }
    await sleep(2000);
  }

  /* ------------ 4. 抓结果 ------------ */
  const grab = async (expr, label) => {
    try {
      const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true }, 30000);
      const exc = r?.result?.result?.exceptionDetails;
      if (exc) throw new Error(exc.text + " " + (exc.exception?.description ?? ""));
      return val(r);
    } catch (e) {
      console.log(`抓取 ${label} 失败: ${e.message}`);
      return null;
    }
  };

  const dom = await grab("document.documentElement.outerHTML", "DOM");
  const logsJson = await grab(
    "JSON.stringify([...document.querySelectorAll('.log-line')].map(e=>e.textContent))",
    "日志"
  );
  const tablesJson = await grab(
    `JSON.stringify({
       size:    document.getElementById('tbl-size')?.hidden    === false ? document.getElementById('tbl-size').outerHTML : null,
       quality: document.getElementById('tbl-quality')?.hidden === false ? document.getElementById('tbl-quality').outerHTML : null,
       rt: { device: document.getElementById('rt-device')?.textContent,
             dtype:  document.getElementById('rt-dtype')?.textContent,
             loadMs: document.getElementById('rt-load')?.textContent },
       chipDb: document.getElementById('chip-db')?.textContent
     })`,
    "表格"
  );

  if (dom) fs.writeFileSync(path.join(OUT_DIR, "dom.html"), dom, "utf8");
  if (logsJson) fs.writeFileSync(path.join(OUT_DIR, "logs.txt"), JSON.parse(logsJson).join("\n"), "utf8");
  if (tablesJson) {
    const parsed = JSON.parse(tablesJson);
    fs.writeFileSync(path.join(OUT_DIR, "tables.json"), JSON.stringify(parsed, null, 2), "utf8");
    fs.writeFileSync(path.join(OUT_DIR, "tbl-size.html"), parsed.size || "", "utf8");
    fs.writeFileSync(path.join(OUT_DIR, "tbl-quality.html"), parsed.quality || "", "utf8");
  }

  console.log("\n=== 运行日志 ===");
  if (logsJson) for (const line of JSON.parse(logsJson)) console.log("  " + line);
  console.log(`\n产物目录: ${OUT_DIR}`);
} finally {
  try {
    ws?.close();
  } catch {}
  child.kill();
}
process.exit(0);
