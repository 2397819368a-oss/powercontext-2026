/**
 * dify-bootstrap.mjs —— 把「Dify 初始化 → 导入工作流 → 拿 API Key」全流程脚本化
 *
 * 为什么要脚本而不是手点：交付物要能被评审一键复现。
 * 手点出来的东西没法验证「换台机器还能不能跑起来」。
 *
 * 用到的 Dify Console 接口（对应 1.18 源码）：
 *   GET  /console/api/setup                       → {step: "not_started" | "finished"}
 *   POST /console/api/setup                       → 建管理员（INIT_PASSWORD 为空时无需额外校验）
 *   POST /console/api/login                       → access_token
 *   POST /console/api/apps/imports                → body {mode:"yaml-content", yaml_content}
 *   GET  /console/api/apps                        → 应用列表，取 app id
 *   POST /console/api/apps/{id}/api-keys          → Service API Key
 *
 * 用法：
 *   node tools/dify-bootstrap.mjs
 *   DIFY_BASE=http://127.0.0.1:8090 node tools/dify-bootstrap.mjs
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(HERE, "..");

const BASE = process.env.DIFY_BASE || "http://127.0.0.1:8090";
const EMAIL = process.env.DIFY_EMAIL || "admin@grudge.local";
const PASSWORD = process.env.DIFY_PASSWORD || "GrudgeTavern2026!";
const NAME = process.env.DIFY_NAME || "老陈";
const DSL = process.env.DSL_FILE || path.join(ROOT, "dsl", "grudge-tavern.chatflow.yml");
const APP_NAME = process.env.DIFY_APP_NAME || "记仇酒馆";

const log = (...a) => console.log(...a);

let CSRF = "";
let COOKIE = "";

async function j(method, p, body, token) {
  const headers = { "Content-Type": "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;
  // libs/token.py:check_csrf_token 要求「header X-CSRF-Token」和「cookie csrf_token」
  // 两者都存在且相等，缺一边就是 401。所以必须把 cookie 一起带上。
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

/**
 * 从 Set-Cookie 里取指定 cookie。
 *
 * Dify 1.18 的登录响应体是 {"result":"success","data":null}——
 * token 不再放在 body 里，而是写成 cookie（controllers/console/auth/login.py:_token_response）。
 * 后端 libs/token.py:extract_access_token() 支持 Authorization: Bearer，
 * 所以把 access_token 抠出来当 Bearer 用；CSRF 则按规则单独放 header。
 */
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

const tokenOf = (d) => d?.access_token || d?.data?.access_token || "";

async function main() {
  log(`Dify: ${BASE}`);

  /* ① 初始化状态 */
  const st = await j("GET", "/console/api/setup");
  const step = st.data?.step;
  log(`① setup 状态: ${step ?? `HTTP ${st.status}`} (${st.text.slice(0, 120)})`);

  if (step === "not_started") {
    const r = await j("POST", "/console/api/setup", { email: EMAIL, name: NAME, password: PASSWORD, language: "zh-Hans" });
    if (r.status >= 300) throw new Error(`创建管理员失败 ${r.status}: ${r.text.slice(0, 300)}`);
    log(`   已创建管理员 ${EMAIL}`);
  }

  /* ② 登录
   * 坑：Dify 的 /console/api/login 挂了 decrypt_password_field 装饰器，
   * 会把 payload.password 直接做 base64 解码（libs/encryption.py，并非真加密）。
   * 传明文会解码失败 → 401 "Invalid encrypted data"。
   * setup 接口没有这个装饰器，所以上面建管理员用明文是对的。 */
  const b64 = Buffer.from(PASSWORD, "utf8").toString("base64");
  const lg = await j("POST", "/console/api/login", { email: EMAIL, password: b64, remember_me: true });
  const token = tokenOf(lg.data) || pickCookie(lg.res, "access_token");
  if (!token) throw new Error(`登录失败 ${lg.status}: ${lg.text.slice(0, 300)}  set-cookie=${lg.res.headers.get("set-cookie")}`);
  CSRF = pickCookie(lg.res, "csrf_token");
  COOKIE = `access_token=${token}; csrf_token=${CSRF}`;
  log(`② 登录成功，token=${token.slice(0, 12)}…  csrf=${CSRF ? CSRF.slice(0, 8) + "…" : "（无）"}`);

  /* ③ 清理同名旧应用
   * /console/api/apps/imports 每次都会**新建一个应用**，没有「覆盖同名」的语义。
   * 反复迭代 DSL 会堆出一堆叫「记仇酒馆」的应用，截图时容易选错、也说明不了东西。
   * 这里先按名字删掉旧的（同时清掉本地残留的 .difykey），再导入一份干净的。 */
  const pre = await j("GET", "/console/api/apps?page=1&limit=100", undefined, token);
  const preList = pre.data?.data || pre.data?.items || [];
  const olds = preList.filter((a) => a.name === APP_NAME);
  for (const o of olds) {
    const d = await j("DELETE", `/console/api/apps/${o.id}`, undefined, token);
    log(`③ 删除旧应用 ${o.id.slice(0, 8)}… -> HTTP ${d.status}`);
  }
  if (!olds.length) log(`③ 无同名旧应用`);

  let yaml = fs.readFileSync(DSL, "utf8");

  /* ④ 把本机实际的 scope / token 写进 DSL 后再导入
   *
   * 这里有两个真实的踩坑来源：
   * 1. scope 漂移：手改 seed 脚本换了 scope（`--fresh`），DSL 里还留着旧 id，
   *    于是工作流连的是一个「没人写」的空账本——NPC 什么都想不起来，看着像作品坏了。
   * 2. 仓库里的 DSL 不能带真 token：工作流 HTTP 节点的 Bearer 值会被导出到 YAML，
   *    直接提交等于把密钥送进版本库。所以仓库版写占位符，导入前在这里注入真值。
   *
   * 做法是强制以本机为准（.scope / 环境变量），从根上消掉这两类漂移。 */
  const lines = yaml.split(/\r?\n/);
  const nl = yaml.includes("\r\n") ? "\r\n" : "\n";

  /** 找到 `name: <ENV名>` 之后的第一个 `value:` 行，替换成 val。返回是否命中。 */
  function replaceEnvValue(envName, val) {
    for (let i = 0; i < lines.length; i++) {
      const hit = new RegExp(`^(\\s*)name:\\s*${envName}\\s*$`).exec(lines[i]);
      if (!hit) continue;
      const nameIndent = hit[1].length;
      for (let k = i + 1; k < lines.length; k++) {
        const indent = /^\s*/.exec(lines[k])[0].length;
        // 缩进更浅的 `-` 表示下一个 entry 开始了（不能用 `/^\s*-\s/` 直接判，
        // 因为同一个 entry 里 `selector:` 下的 `- env` 缩进更深，误判会漏替换）
        if (/^\s*-\s/.test(lines[k]) && indent < nameIndent) return false;
        if (new RegExp(`^\\s{${nameIndent}}value:\\s*\\S+`).test(lines[k])) {
          lines[k] = lines[k].replace(/value:\s*\S+/, `value: ${val}`);
          return true;
        }
      }
    }
    return false;
  }

  const scopeFile = process.env.PC_SCOPE_FILE || path.join(ROOT, ".scope");
  if (fs.existsSync(scopeFile)) {
    const scopeId = fs.readFileSync(scopeFile, "utf8").trim();
    if (scopeId && scopeId !== "undefined" && replaceEnvValue("PC_SCOPE", scopeId)) {
      yaml = lines.join(nl);
      log(`④ scope 同步为 ${scopeId}`);
    }
  }

  const pcToken = process.env.POWERCONTEXT_SERVER_AUTH_TOKEN || "";
  if (pcToken && pcToken !== "undefined" && replaceEnvValue("PC_TOKEN", pcToken)) {
    yaml = lines.join(nl);
    log(`④ token 已注入（${pcToken.slice(0, 6)}…）`);
  }

  /* ⑤ 导入 DSL */
  const im = await j("POST", "/console/api/apps/imports", { mode: "yaml-content", yaml_content: yaml }, token);
  log(`⑤ 导入 DSL: HTTP ${im.status} status=${im.data?.status ?? "?"}`);
  if (im.status >= 300) {
    log(`   失败详情: ${JSON.stringify(im.data).slice(0, 1500)}`);
    throw new Error("DSL 导入失败");
  }
  if (im.data?.status === "pending") {
    log(`   需要确认依赖: ${JSON.stringify(im.data).slice(0, 600)}`);
    const cf = await j("POST", `/console/api/apps/imports/${im.data.id}/confirm`, {}, token);
    log(`   确认: HTTP ${cf.status} ${cf.text.slice(0, 200)}`);
  }

  /* ⑥ 找到 app id */
  const apps = await j("GET", "/console/api/apps?page=1&limit=50", undefined, token);
  const list = apps.data?.data || apps.data?.items || [];
  const app = list.find((a) => a.name === APP_NAME) || list[0];
  if (!app) throw new Error(`没找到应用。列表：${JSON.stringify(apps.data).slice(0, 500)}`);
  log(`⑥ 应用: ${app.name}  id=${app.id}  mode=${app.mode}`);

  /* ⑦ 发布工作流 */
  // Service API（/v1/chat-messages）只跑**已发布**的工作流，
  // 不发布会直接报 400 {"code":"invalid_param","message":"Workflow not published"}。
  const pub = await j("POST", `/console/api/apps/${app.id}/workflows/publish`,
    { marked_name: "v1", marked_comment: "记仇酒馆首版" }, token);
  log(`⑦ 发布工作流: HTTP ${pub.status} ${JSON.stringify(pub.data).slice(0, 200)}`);
  if (pub.status >= 300) throw new Error(`发布失败: ${pub.text.slice(0, 500)}`);

  /* ⑧ 建 API Key */
  const keys = await j("GET", `/console/api/apps/${app.id}/api-keys`, undefined, token);
  const exist = (keys.data?.data || keys.data || [])[0];
  let newKey = exist?.token || "";
  if (!newKey) {
    const k = await j("POST", `/console/api/apps/${app.id}/api-keys`, {}, token);
    newKey = k.data?.token || k.data?.data?.token;
    log(`⑧ 新建 API Key: ${newKey ?? `HTTP ${k.status} ${k.text.slice(0, 300)}`}`);
  } else {
    log(`⑧ 已有 API Key: ${newKey}`);
  }

  if (newKey) {
    fs.writeFileSync(path.join(ROOT, ".difykey"), newKey + "\n", "utf8");
    fs.writeFileSync(path.join(ROOT, ".appid"), app.id + "\n", "utf8");
    log(`   已写入 app/.difykey 与 app/.appid`);
  }
}

main().catch((e) => {
  console.error(`\n失败：${e.message}`);
  process.exit(1);
});
