/**
 * db.mjs —— 本地 seekdb 实例管理 + 全部 SQL 封装
 *
 * 设计红线（对应赛题要求）：
 *  1. 服务端【绝不做推理】。这个模块不引入任何 embedding 模型，
 *     写入接口只接受浏览器已经算好的向量；一旦有人传纯文本想让服务端
 *     代算向量，直接拒绝（HttpError 400）。这是"禁止云端推理"的可验证实现。
 *  2. 混合检索必须由 seekdb 完成。这里用库内 SQL 做 RRF 融合，
 *     不是把两路结果拉到 Node 里再拼——见 hybridSearch()。
 *
 * 为什么不用 seekdb 的嵌入式(@seekdb/js-bindings)：
 *   官方文档明确 Windows 不支持嵌入式模式；实测其 native 调用会同步阻塞
 *   Node 事件循环（Promise.race 超时都不触发）。因此改用
 *   @oceanbase/seekdb-node 拉起本地 seekdb 进程 + mysql2 over TCP，
 *   这是官方在 Windows 11 x64 上验证过的路径。
 */

import path from "node:path";
import fs from "node:fs";
import { seekdb } from "@oceanbase/seekdb-node";
import mysql from "mysql2/promise";

/** 模型维度：Xenova/all-MiniLM-L6-v2 = 384 维。前端模型一旦换了要同步这里 */
export const DIM = Number(process.env.EMBED_DIM || 384);

const TB_DOC = "docs";
const TB_META = "collection_meta";

export class HttpError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

/**
 * 选择数据目录。
 * seekdb 的 native 层对非 ASCII 路径不友好（中文目录下实例可能起不来），
 * 所以这里主动检测：路径里一旦有非 ASCII 字符，就降级到 ASCII 目录，
 * 并明确告知实际用了哪个位置，避免"起了但连不上"的黑盒失败。
 */
function resolveDataDir() {
  const candidates = [];
  if (process.env.SEEKDB_DATA_DIR) candidates.push(process.env.SEEKDB_DATA_DIR);
  candidates.push(path.resolve(process.cwd(), "../data/seekdb.db"));

  const isAscii = (s) => /^[\x20-\x7E]+$/.test(s);
  for (const c of candidates) {
    if (isAscii(c)) return { dir: c, fallback: false };
  }
  // 所有候选都不干净（典型：项目放在 D:\OceanBase大赛\... )
  const fb = path.join("C:/", "powercontext-data", "seekdb.db");
  return { dir: fb, fallback: true, original: candidates[candidates.length - 1] };
}

/** 单实例句柄 */
let _handle = null;
let _conn = null;
let _endpoint = null;

export function endpointInfo() {
  return _endpoint;
}

/** 启动（或复用）本地 seekdb 实例，并建立 TCP 连接 */
export async function open() {
  if (_conn) return { conn: _conn, endpoint: _endpoint };

  const { dir, fallback, original } = resolveDataDir();
  fs.mkdirSync(path.dirname(dir), { recursive: true });
  if (fallback) {
    console.warn(
      `[db] 检测到非 ASCII 数据路径: ${original}\n` +
        `[db] 已降级到 ASCII 目录: ${dir}（可用 SEEKDB_DATA_DIR 指定）`
    );
  }
  console.log(`[db] 启动本地 seekdb，数据目录 = ${dir}`);

  // mysql_port_mode=random：避免固定 2881 端口冲突
  _handle = await seekdb.open(dir, {
    parameters: { memory_budget: "2G", mysql_port_mode: "random" },
  });

  const endpoint = _handle.connectionOptions();
  if (endpoint.transport !== "tcp") {
    throw new Error(`暂不支持的传输方式: ${endpoint.transport}（本实现走 TCP）`);
  }
  _endpoint = endpoint;

  _conn = await mysql.createConnection({
    host: endpoint.host,
    port: endpoint.port,
    user: endpoint.user,
    password: "",
    database: "test",
    multipleStatements: false, // 显式关掉，杜绝多语句注入
  });

  await ensureSchema(_conn);
  console.log(`[db] 就绪: ${endpoint.host}:${endpoint.port} dim=${DIM}`);
  return { conn: _conn, endpoint };
}

export async function close() {
  try {
    if (_conn) await _conn.end();
  } finally {
    if (_handle) _handle.close();
    _conn = null;
    _handle = null;
    _endpoint = null;
  }
}

async function ensureSchema(conn) {
  // 建表语法取自 seekdb SDK 自身的 SQLBuilder.buildCreateTable，关键三点：
  //   VECTOR INDEX 用括号包列名（没有 ON）、必须带 lib=vsag、表尾 ORGANIZATION = HEAP
  await conn.query(
    `CREATE TABLE IF NOT EXISTS ${TB_DOC} (
       id VARBINARY(512) PRIMARY KEY NOT NULL,
       content STRING,
       embedding VECTOR(${DIM}),
       metadata JSON,
       FULLTEXT INDEX idx_ft (content) WITH PARSER ik,
       VECTOR INDEX idx_vec (embedding) WITH(distance=cosine, type=hnsw, lib=vsag)
     ) ORGANIZATION = HEAP`
  );
  await conn.query(
    `CREATE TABLE IF NOT EXISTS ${TB_META} (
       k VARBINARY(128) PRIMARY KEY NOT NULL,
       v STRING
     ) ORGANIZATION = HEAP`
  );
  await checkDimension(conn);
}

/**
 * 维度漂移防护。
 *
 * 坑的来源：CREATE TABLE IF NOT EXISTS 在表已存在时是【静默跳过】的。
 * 如果之前用 EMBED_DIM=8 跑过自检，表里就是 VECTOR(8)；
 * 换成真实的 384 维模型再启动，建表语句不会生效，
 * 于是插入时报一个看起来莫名其妙的维度错误。这里提前拦下来。
 *
 * 查询失败也不影响启动（包在 try 里），只是放弃这项体检。
 */
async function checkDimension(conn) {
  try {
    const [rows] = await conn.query(
      `SELECT COLUMN_TYPE FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? AND COLUMN_NAME = 'embedding'`,
      ["test", TB_DOC]
    );
    if (!rows || rows.length === 0) return; // 查不到就不校验
    const decl = String(rows[0].COLUMN_TYPE ?? rows[0].column_type ?? "");
    const m = decl.match(/(\d+)/);
    if (!m) return;
    const existing = Number(m[1]);
    if (existing !== DIM) {
      throw new Error(
        `已存在的表 ${TB_DOC}.embedding 是 ${existing} 维，但当前配置为 ${DIM} 维。\n` +
          `这是同一个数据目录被不同 EMBED_DIM 用过导致的。请二选一：\n` +
          `  1) 换一个干净的数据目录：SEEKDB_DATA_DIR=<ASCII路径>\n` +
          `  2) 确认不再需要旧数据后删掉该目录重建\n` +
          `当前数据目录：${resolveDataDir().dir}`
      );
    }
  } catch (e) {
    // 上面抛的维度冲突要往外传；只有 information_schema 本身不可用时才放行
    if (/维度/.test(e.message)) throw e;
  }
}

/* ----------------------------- 写入 ----------------------------- */

/**
 * 批量写入文档。
 * @param {Array<{id:string, content:string, embedding:number[], meta?:object}>} items
 * 每个 item 的 embedding 必填且长度必须等于 DIM —— 这是"浏览器端向量化"的强制点。
 */
export async function insertDocs(items) {
  if (!Array.isArray(items) || items.length === 0) {
    throw new HttpError(400, "items 不能为空");
  }
  const rows = [];
  for (const it of items) {
    if (!it.id) throw new HttpError(400, "每条记录都需要 id");
    if (typeof it.content !== "string") {
      throw new HttpError(400, `id=${it.id} 缺少 content`);
    }
    if (!Array.isArray(it.embedding)) {
      // 服务端不代算：与其偷偷调用模型违反"禁止云端推理"，不如明确报错
      throw new HttpError(
        400,
        `id=${it.id} 缺少 embedding。向量必须在浏览器端计算后传入，服务端不做推理。`
      );
    }
    if (it.embedding.length !== DIM) {
      throw new HttpError(
        400,
        `id=${it.id} 向量维度 ${it.embedding.length} != ${DIM}`
      );
    }
    rows.push([
      Buffer.from(String(it.id)),
      it.content,
      JSON.stringify(it.embedding), // VECTOR 列接受 JSON 数组字符串
      JSON.stringify(it.meta ?? {}),
    ]);
  }

  const { conn } = await open();
  // 分批提交，避免单条 SQL 过大
  const BATCH = 200;
  let affected = 0;
  for (let i = 0; i < rows.length; i += BATCH) {
    const part = rows.slice(i, i + BATCH);
    const [r] = await conn.query(
      `INSERT INTO ${TB_DOC} (id, content, embedding, metadata) VALUES ?`,
      [part]
    );
    affected += r.affectedRows;
  }
  return { inserted: affected };
}

export async function clearDocs() {
  const { conn } = await open();
  await conn.query(`DELETE FROM ${TB_DOC}`);
  return { cleared: true };
}

export async function stats() {
  const { conn } = await open();
  const [[{ c }]] = await conn.query(`SELECT COUNT(*) AS c FROM ${TB_DOC}`);
  return { count: Number(c), dim: DIM };
}

/* ----------------------------- 检索 ----------------------------- */

const SELECT_COLS = `id, content, metadata`;

/**
 * seekdb 会把 DECIMAL/DOUBLE 运算结果以【字符串】返回（如 rrf "0.03252"），
 * mysql2 不做自动转换。这里统一转 Number，否则上层拿到的是 string，
 * 一做数值比较/排序就会静默出错（曾经导致前端显示 NaN）。
 */
function num(v) {
  if (v === null || v === undefined) return undefined;
  const n = Number(v);
  return Number.isFinite(n) ? n : undefined;
}

function normalizeRows(rows) {
  return rows.map((r) => ({
    id: Buffer.isBuffer(r.id) ? r.id.toString("utf8") : String(r.id),
    content: r.content,
    meta: safeJson(r.metadata),
    distance: num(r.d),
    score: num(r.score),
    rrf: num(r.rrf),
  }));
}

function safeJson(v) {
  if (!v) return {};
  try {
    // mysql2 已把 JSON 列解析成对象；极端情况下仍是字符串时再 parse 一次
    return typeof v === "string" ? JSON.parse(v) : v;
  } catch {
    return {};
  }
}

/** 纯向量检索（cosine 距离，越小越近） */
export async function vectorSearch(embedding, topK) {
  const { conn } = await open();
  const [rows] = await conn.query(
    `SELECT ${SELECT_COLS}, cosine_distance(embedding, ?) AS d
       FROM ${TB_DOC} ORDER BY d ASC LIMIT ?`,
    [JSON.stringify(embedding), Math.min(topK, 100)]
  );
  return { rows: normalizeRows(rows), mode: "vector" };
}

/** 纯全文检索（seekdb ik 分词，MySQL 兼容的 MATCH ... AGAINST 语法） */
export async function fulltextSearch(text, topK) {
  const { conn } = await open();
  const [rows] = await conn.query(
    `SELECT ${SELECT_COLS}, MATCH(content) AGAINST(? IN NATURAL LANGUAGE MODE) AS score
       FROM ${TB_DOC}
      WHERE MATCH(content) AGAINST(? IN NATURAL LANGUAGE MODE)
      ORDER BY score DESC LIMIT ?`,
    [text, text, Math.min(topK, 100)]
  );
  return { rows: normalizeRows(rows), mode: "fulltext" };
}

/**
 * 混合检索：向量 + 全文，在 seekdb 内部用 RRF 融合。
 *
 * RRF（Reciprocal Rank Fusion, k=60）:  score(d) = Σ_i 1/(k + rank_i(d))
 * 优势是不需要归一化两路量纲不同的分数（cosine 距离 vs BM25 分数）。
 *
 * 为什么 UNION 而不是 JOIN：左路/右路各有对方没有的文档，
 * LEFT JOIN 会漏掉"只在全文里命中"的行，RIGHT 补齐后才完整，
 * 两次再 UNION 去重。这段 SQL 已在本机 seekdb 1.4.0 实测通过。
 */
export async function hybridSearch(embedding, text, topK) {
  const { conn } = await open();
  const vec = JSON.stringify(embedding);
  const limit = Math.min(topK, 100);

  const sql = `
    WITH ranked_vec AS (
      SELECT id, ROW_NUMBER() OVER (ORDER BY cosine_distance(embedding, ?) ASC) AS rk
        FROM ${TB_DOC}
    ),
    ranked_ft AS (
      SELECT id, ROW_NUMBER() OVER (ORDER BY MATCH(content) AGAINST(? IN NATURAL LANGUAGE MODE) DESC) AS rk
        FROM ${TB_DOC}
       WHERE MATCH(content) AGAINST(? IN NATURAL LANGUAGE MODE)
    ),
    fused AS (
      SELECT COALESCE(v.id, f.id) AS id,
             COALESCE(1.0/(60 + v.rk), 0) + COALESCE(1.0/(60 + f.rk), 0) AS rrf
        FROM ranked_vec v LEFT JOIN ranked_ft f ON v.id = f.id
      UNION
      SELECT COALESCE(v.id, f.id) AS id,
             COALESCE(1.0/(60 + v.rk), 0) + COALESCE(1.0/(60 + f.rk), 0) AS rrf
        FROM ranked_vec v RIGHT JOIN ranked_ft f ON v.id = f.id
    )
    SELECT d.id AS id, d.content AS content, d.metadata AS metadata,
           fused.rrf AS rrf,
           cosine_distance(d.embedding, ?) AS d
      FROM fused JOIN ${TB_DOC} d ON fused.id = d.id
     ORDER BY fused.rrf DESC LIMIT ?`;

  const [rows] = await conn.query(sql, [vec, text, text, vec, limit]);
  return { rows: normalizeRows(rows), mode: "hybrid", rank: "rrf(k=60)" };
}
