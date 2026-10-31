"""FastAPI Web 应用。"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from backend.app import database
from backend.app.search.engine import search, build_sql

app = FastAPI(title="笔记检索系统")


# ── API 模型 ──────────────────────────────────────────────────

class SearchResult(BaseModel):
    chunk_id: int
    file_path: str
    heading: str
    line_start: int
    line_end: int
    score: float
    snippet: str
    dir_type: str
    year: int
    tags: str


class SearchResponse(BaseModel):
    sql: str
    took_ms: float
    results: list[SearchResult]


class ChunkDetail(BaseModel):
    chunk_id: int
    file_path: str
    heading: str
    line_start: int
    line_end: int
    content: str
    context_before: list[str]
    context_after: list[str]


class Stats(BaseModel):
    notes_count: int
    chunks_count: int


# ── API 端点 ──────────────────────────────────────────────────

@app.get("/api/search", response_model=SearchResponse)
def api_search(
    q: str,
    type: str | None = None,
    year: int | None = None,
    tag: str | None = None,
    top_k: int = 10,
):
    """搜索笔记，返回实际执行的 SQL + 耗时 + 结果。"""
    filters = {}
    if type:
        type_map = {
            "moc": "10-MOC与索引",
            "exam": "11-赛题归档",
            "card": "12-原子卡片",
            "code": "13-代码片段",
            "literature": "14-文献卡片",
        }
        filters["dir_type"] = type_map.get(type, type)
    if year:
        filters["year"] = year
    if tag:
        filters["tags"] = tag

    sql = build_sql(q or "", filters, top_k)

    if not q or not q.strip():
        return SearchResponse(sql=sql, took_ms=0.0, results=[])

    database.init_db()
    conn = database.get_connection()
    try:
        t0 = time.perf_counter()
        results = search(conn, q, filters=filters, top_k=top_k)
        took_ms = (time.perf_counter() - t0) * 1000
    finally:
        conn.close()
        database.close_db()

    return SearchResponse(
        sql=sql,
        took_ms=round(took_ms, 2),
        results=[SearchResult(**r) for r in results],
    )


@app.get("/api/chunk/{chunk_id}", response_model=ChunkDetail)
def api_chunk(chunk_id: int):
    """获取 chunk 详情（含上下文）。"""
    database.init_db()
    conn = database.get_connection()
    try:
        row = database.fetchone(conn, f"""
          SELECT nc.content, nc.line_start, nc.line_end, nc.heading, n.file_path
          FROM note_chunks nc
          JOIN notes n ON n.id = nc.note_id
          WHERE nc.id = {database.escape_int(chunk_id)}
        """)
    finally:
        conn.close()
        database.close_db()
    
    if not row:
        raise HTTPException(status_code=404, detail="Chunk not found")
    
    content, line_start, line_end, heading, file_path = row
    
    # 读取原文件，提取上下文
    context_before = []
    context_after = []
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        
        # 前 2 行上下文
        for i in range(max(0, line_start - 3), line_start - 1):
            context_before.append(f"{i+1:4d} | {lines[i].rstrip()}")
        
        # 后 2 行上下文
        for i in range(line_end, min(len(lines), line_end + 2)):
            context_after.append(f"{i+1:4d} | {lines[i].rstrip()}")
    except Exception:
        pass
    
    return ChunkDetail(
        chunk_id=chunk_id,
        file_path=file_path,
        heading=heading or "",
        line_start=line_start,
        line_end=line_end,
        content=content,
        context_before=context_before,
        context_after=context_after,
    )


@app.get("/api/stats", response_model=Stats)
def api_stats():
    """获取库内统计。"""
    database.init_db()
    conn = database.get_connection()
    try:
        notes_count = database.fetchone(conn, "SELECT COUNT(*) FROM notes")[0]
        chunks_count = database.fetchone(conn, "SELECT COUNT(*) FROM note_chunks")[0]
    finally:
        conn.close()
        database.close_db()
    
    return Stats(notes_count=notes_count, chunks_count=chunks_count)


# ── 首页 ──────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def index():
    """返回单页 HTML。"""
    html_path = Path(__file__).parent / "templates" / "index.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
