"""seekdb 连接管理 + schema 初始化。

关键约束（实测踩坑）：
1. seekdb.execute() 只接受单个 SQL 字符串，不支持参数绑定（%s 占位符）。
   所有带用户输入的值必须先经 escape_literal() 转义再拼进 SQL。
2. seekdb 无法打开含非 ASCII 字符的路径，数据库目录固定在 C:/Temp/mmkb_db。
"""

import os

import pylibseekdb

from backend.app.config import DB_DIR

_instance: pylibseekdb.SeekdbInstance | None = None

# ── 建表 SQL ──────────────────────────────────────────────────

CREATE_NOTES_TABLE = """
CREATE TABLE IF NOT EXISTS notes (
    id          BIGINT AUTO_INCREMENT,
    file_path   VARCHAR(500)  NOT NULL,
    file_hash   VARCHAR(64)   NOT NULL,
    dir_type    VARCHAR(50)   DEFAULT NULL,
    title       VARCHAR(200)  DEFAULT NULL,
    date        DATE          DEFAULT NULL,
    year        INT           DEFAULT NULL,
    tags        VARCHAR(500)  DEFAULT NULL,
    status      VARCHAR(50)   DEFAULT NULL,
    created_at  TIMESTAMP     DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP     DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_file_path (file_path),
    KEY idx_dir_type (dir_type),
    KEY idx_year (year),
    KEY idx_tags (tags(100))
)
"""

CREATE_NOTE_CHUNKS_TABLE = """
CREATE TABLE IF NOT EXISTS note_chunks (
    id           BIGINT AUTO_INCREMENT,
    note_id      BIGINT        NOT NULL,
    chunk_index  INT           NOT NULL,
    heading      VARCHAR(200)  DEFAULT NULL,
    line_start   INT           NOT NULL,
    line_end     INT           NOT NULL,
    content      LONGTEXT,
    dir_type     VARCHAR(50)   DEFAULT NULL,
    year         INT           DEFAULT NULL,
    tags         VARCHAR(500)  DEFAULT NULL,
    PRIMARY KEY (id),
    KEY idx_note_id (note_id),
    FULLTEXT KEY ft_content (content) WITH PARSER ngram,
    FULLTEXT KEY ft_heading (heading) WITH PARSER ngram,
    KEY idx_dir_type (dir_type),
    KEY idx_year (year)
)
"""


# ── 转义函数 ──────────────────────────────────────────────────

def escape_literal(value: str) -> str:
    """将字符串转义为 seekdb 可用的 SQL 字面量（含两侧单引号）。

    seekdb 不支持参数绑定，所有用户输入必须手动转义后拼进 SQL。
    处理规则：
      - 反斜杠  \\  →  \\\\
      - 单引号  '   →  ''
      - 双引号  "   →  \\"
      - NULL byte      →  去掉
    """
    if value is None:
        return "NULL"
    s = str(value)
    s = s.replace("\\", "\\\\")
    s = s.replace("'", "''")
    s = s.replace('"', '\\"')
    s = s.replace("\x00", "")
    return f"'{s}'"


def escape_int(value) -> str:
    if value is None:
        return "NULL"
    return str(int(value))


# ── 连接管理 ──────────────────────────────────────────────────

def get_instance() -> pylibseekdb.SeekdbInstance:
    """获取（或创建）全局 seekdb 实例。"""
    global _instance
    if _instance is None or _instance.closed:
        os.makedirs(DB_DIR, exist_ok=True)
        _instance = pylibseekdb.open(DB_DIR)
    return _instance


def get_connection() -> pylibseekdb.Connection:
    """从全局实例获取连接。"""
    return get_instance().connect()


def execute(conn: pylibseekdb.Connection, sql: str) -> int:
    """执行单条 SQL，返回受影响行数。"""
    cur = conn.cursor()
    try:
        affected = cur.execute(sql)
        return affected
    finally:
        cur.close()


def fetchall(conn: pylibseekdb.Connection, sql: str):
    """执行查询 SQL，返回全部结果。"""
    cur = conn.cursor()
    try:
        cur.execute(sql)
        return cur.fetchall()
    finally:
        cur.close()


def fetchone(conn: pylibseekdb.Connection, sql: str):
    """执行查询 SQL，返回单条结果。"""
    cur = conn.cursor()
    try:
        cur.execute(sql)
        return cur.fetchone()
    finally:
        cur.close()


# ── Schema 初始化 ──────────────────────────────────────────────

def init_db():
    """创建 notes 和 note_chunks 两张表（如果不存在）。"""
    conn = get_connection()
    try:
        execute(conn, CREATE_NOTES_TABLE)
        execute(conn, CREATE_NOTE_CHUNKS_TABLE)
        conn.commit()
    finally:
        conn.close()


def close_db():
    """关闭全局 seekdb 实例。"""
    global _instance
    if _instance is not None and not _instance.closed:
        _instance.close()
    _instance = None
