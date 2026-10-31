/**
 * index.mjs —— HTTP 入口
 *
 * 对外只暴露三个检索/写入端点，全部要求调用方（浏览器）自带向量。
 * 任何需要"服务端算向量"的功能都没有实现，也不打算实现。
 */

import express from "express";
import cors from "cors";
import {
  open,
  close,
  insertDocs,
  clearDocs,
  stats,
  vectorSearch,
  fulltextSearch,
  hybridSearch,
  DIM,
} from "./db.mjs";

const PORT = Number(process.env.PORT || 8787);
const app = express();

app.use(cors());
app.use(express.json({ limit: "32mb" })); // 一批文档可能带几百条 384 维向量，默认 100kb 不够

// 记录实际执行的 SQL 语义，便于演示页展示"检索确实发生在 seekdb 里"
function wrap(mode, rank) {
  return (payload) => ({ mode, rank, ...payload });
}

/* ------------------------------- 路由 ------------------------------- */

app.get("/api/health", async (_req, res) => {
  try {
    await open();
    const ep = await import("./db.mjs").then((m) => m.endpointInfo());
    res.json({ ok: true, dim: DIM, endpoint: ep });
  } catch (e) {
    res.status(500).json({ ok: false, error: e.message });
  }
});

app.get("/api/stats", async (_req, res) => {
  try {
    res.json(await stats());
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

/** 写入：body = { items: [{id, content, embedding:[...], meta:{}}] } */
app.post("/api/ingest", async (req, res) => {
  try {
    const { items } = req.body ?? {};
    res.json(await insertDocs(items));
  } catch (e) {
    res.status(e.status || 500).json({ error: e.message });
  }
});

app.delete("/api/docs", async (_req, res) => {
  try {
    res.json(await clearDocs());
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

/**
 * 检索：body = { embedding: number[], text?: string, topK?: number }
 * - 只给 embedding         -> 纯向量
 * - 只给 text              -> 纯全文（ik）
 * - 两个都给               -> seekdb 内部 RRF 混合检索
 * 两个都不给 -> 400，因为服务端无法凭空产生查询向量。
 */
app.post("/api/search", async (req, res) => {
  try {
    const { embedding, text, topK } = req.body ?? {};
    const k = Number(topK) || 5;

    if (!Array.isArray(embedding) && !text) {
      return res
        .status(400)
        .json({ error: "需要 embedding 或 text（服务端不计算查询向量）" });
    }
    if (Array.isArray(embedding) && embedding.length !== DIM) {
      return res.status(400).json({
        error: `查询向量维度 ${embedding.length} != ${DIM}`,
      });
    }

    let out;
    if (Array.isArray(embedding) && text) {
      out = wrap("hybrid", "rrf(k=60)")(await hybridSearch(embedding, text, k));
    } else if (Array.isArray(embedding)) {
      out = wrap("vector", "cosine_distance")(await vectorSearch(embedding, k));
    } else {
      out = wrap("fulltext", "ik-MATCH-AGAINST")(await fulltextSearch(text, k));
    }
    res.json(out);
  } catch (e) {
    res.status(e.status || 500).json({ error: e.message });
  }
});

/* ------------------------------- 启动 ------------------------------- */

let server;
async function main() {
  await open();
  server = app.listen(PORT, () => {
    console.log(`[server] http://127.0.0.1:${PORT}  ready`);
  });

  // seekdb 是本地进程，必须优雅关闭，否则下次 open 会撞上残留实例和文件锁
  const shutdown = async (sig) => {
    console.log(`\n[server] 收到 ${sig}，正在关闭...`);
    if (server) server.close();
    await close();
    process.exit(0);
  };
  process.on("SIGINT", () => shutdown("SIGINT"));
  process.on("SIGTERM", () => shutdown("SIGTERM"));
}

main().catch((e) => {
  console.error("[server] 启动失败:", e);
  process.exit(1);
});
