"""seekdb 嵌入式访问层。

两个约束（实测得出，见 ``docs/one-pager.md``）：

1. ``pylibseekdb.aopen`` 是异步的；
2. ``Cursor.execute`` **只接受纯 SQL 字符串**，不支持参数绑定，因此所有值
   都要经 :func:`literal` 转义。

中文全文检索必须用 ``WITH PARSER ngram``：默认分词器对中文返回 0 命中。
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

CHUNKS_DDL = """
CREATE TABLE IF NOT EXISTS ctx_chunks (
    chunk_id    VARCHAR(96) PRIMARY KEY,
    doc_path    VARCHAR(255) NOT NULL,
    kind        VARCHAR(16)  NOT NULL,
    symbol      VARCHAR(128) NOT NULL,
    start_line  INT NOT NULL,
    end_line    INT NOT NULL,
    body        TEXT NOT NULL,
    generation  INT NOT NULL,
    FULLTEXT KEY ft_body (body) WITH PARSER ngram
)
"""


def literal(value: Any) -> str:
    """把 Python 值渲染成 MySQL 字面量。"""

    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).replace("\\", "\\\\").replace("'", "\\'")
    return "'" + text + "'"


class SeekdbStore:
    """一次运行内持有的嵌入式实例。"""

    def __init__(self, db_dir: Path) -> None:
        self._db_dir = db_dir
        self._instance: Any = None
        self._conn: Any = None
        self._query_log: list[dict[str, Any]] = []

    # ---------- 生命周期 ----------
    async def open(self, *, fresh: bool = False) -> None:
        import pylibseekdb as seekdb

        # ``fresh`` 不再删目录，只表示「索引清空重建」。见 _reset_dir 的说明。
        already = self._db_dir.exists()
        self._db_dir.mkdir(parents=True, exist_ok=True)
        self._instance = await seekdb.aopen(str(self._db_dir))
        self._conn = self._instance.connect()
        self._execute(CHUNKS_DDL)
        if fresh and already:
            # 旧 index 一律丢掉，保证「不存在 Data Action 沿用旧资料」的可能
            self.clear()

    @staticmethod
    def _explain_no_rmtree() -> str:
        """为什么不再删库目录（Windows 实测，2026-10-30）。

        三层踩坑依次是：

        1. seekdb 的 ``log/seekdb.log`` 即使 ``close()`` 之后仍被占用，
           ``shutil.rmtree`` 抛 ``PermissionError: [WinError 32]``；
        2. rmtree 是**部分生效**的 —— 它会在抛异常前已经删掉一部分文件，
           目录留下一半内容，下一次 ``seekdb.open`` 直接 ``code=-1`` 起不来。
           也就是说，「删一半」比「完全不删」更糟；
        3. 而 fresh 的语义（不沿用旧索引）本来就不依赖删目录：:meth:`replace_chunks`
           先 ``clear()`` 再插入，旧块不可能残留。

        所以这里彻底放弃删除目录，改为原地清空，副作用为零。
        """

        return "Windows 下 seekdb 日志文件锁会导致 rmtree 只删一半并使实例无法启动；改为原地清空"

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        if self._instance is not None:
            self._instance.close()
            self._instance = None

    # ---------- SQL ----------
    def _execute(self, sql: str) -> Any:
        if self._conn is None:
            raise RuntimeError("SeekdbStore 未打开")
        cursor = self._conn.cursor()
        cursor.execute(sql)
        return cursor

    def execute(self, sql: str) -> int:
        """执行写语句并提交，返回受影响行数。"""

        cursor = self._execute(sql)
        affected = cursor.rowcount if hasattr(cursor, "rowcount") else 0
        cursor.close()
        self._conn.commit()
        return affected

    def query(self, sql: str) -> list[tuple]:
        cursor = self._execute(sql)
        rows = cursor.fetchall()
        cursor.close()
        return list(rows)

    # ---------- 领域操作 ----------
    def replace_chunks(self, rows: Iterable[dict[str, Any]]) -> int:
        """用一批新块整体替换旧内容（保证「重新导入后不复用旧答案」）。"""

        payload = list(rows)
        if not payload:
            return 0
        # 索引是"当前快照"：先清空再插入，避免旧块残留导致重导入后仍命中旧内容
        self.clear()
        columns = ("chunk_id", "doc_path", "kind", "symbol", "start_line", "end_line", "body", "generation")
        for row in payload:
            self.execute(
                "INSERT INTO ctx_chunks (%s) VALUES (%s)"
                % (", ".join(columns), ", ".join(literal(row[key]) for key in columns))
            )
        return len(payload)

    def clear(self) -> int:
        return self.execute("DELETE FROM ctx_chunks WHERE 1 = 1")

    def count(self) -> int:
        rows = self.query("SELECT COUNT(*) FROM ctx_chunks")
        return int(rows[0][0]) if rows else 0

    def search(self, query: str, *, kind: str | None = None, limit: int = 8) -> list[dict[str, Any]]:
        """ngram 全文检索，按相关性降序。"""

        if not query.strip():
            return []
        where = ["MATCH(body) AGAINST(%s)" % literal(query)]
        if kind is not None:
            where.append("kind = %s" % literal(kind))
        sql = (
            "SELECT chunk_id, doc_path, kind, symbol, start_line, end_line, body, "
            "MATCH(body) AGAINST(%s) AS score FROM ctx_chunks WHERE %s "
            "ORDER BY score DESC LIMIT %d" % (literal(query), " AND ".join(where), int(limit))
        )
        rows = self.query(sql)
        hits = [
            {
                "chunk_id": row[0],
                "doc_path": row[1],
                "kind": row[2],
                "symbol": row[3],
                "start_line": int(row[4]),
                "end_line": int(row[5]),
                "body": row[6],
                "score": float(row[7]),
            }
            for row in rows
        ]
        self._query_log.append(
            {
                "at": datetime.now().isoformat(timespec="seconds"),
                "query": query,
                "kind": kind,
                "limit": limit,
                "sql": sql,
                "hits": [{"chunk_id": h["chunk_id"], "doc_path": h["doc_path"], "score": round(h["score"], 6)} for h in hits],
            }
        )
        return hits

    def dump_query_log(self, path: Path) -> None:
        """把本次所有检索记录写盘（技术使用记录之一）。"""

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self._query_log, ensure_ascii=False, indent=2), encoding="utf-8")

