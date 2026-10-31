"""Ingest 编排：扫描→解析→切块→入库。"""

import sys
from pathlib import Path

from backend.app import database
from backend.app.ingest.scanner import scan_notes, get_dir_type
from backend.app.ingest.parser import parse_note, ParsedNote
from backend.app.ingest.chunker import chunk_body, Chunk


def _upsert_note(conn, note: ParsedNote) -> int:
    """插入或更新 notes 记录，返回 note_id。"""
    esc = database.escape_literal
    eint = database.escape_int

    date_val = esc(str(note.meta.date)) if note.meta.date else "NULL"
    year_val = eint(note.meta.year) if note.meta.year else "NULL"

    # 先尝试查询是否已存在
    rows = database.fetchall(
        conn,
        f"SELECT id FROM notes WHERE file_path = {esc(note.file_path)}"
    )

    if rows:
        note_id = rows[0][0]
        database.execute(
            conn,
            f"""UPDATE notes SET
                file_hash   = {esc(note.file_hash)},
                dir_type    = {esc(note.meta.dir_type)},
                title       = {esc(note.meta.title or '')},
                date        = {date_val},
                year        = {year_val},
                tags        = {esc(note.meta.tags)},
                status      = {esc(note.meta.status or '')}
              WHERE id = {eint(note_id)}"""
        )
    else:
        database.execute(
            conn,
            f"""INSERT INTO notes
                (file_path, file_hash, dir_type, title, date, year, tags, status)
              VALUES
                ({esc(note.file_path)}, {esc(note.file_hash)},
                 {esc(note.meta.dir_type)}, {esc(note.meta.title or '')},
                 {date_val}, {year_val},
                 {esc(note.meta.tags)}, {esc(note.meta.status or '')})"""
        )
        row = database.fetchone(conn, "SELECT LAST_INSERT_ID()")
        note_id = row[0]

    return note_id


def _insert_chunks(conn, note_id: int, note: ParsedNote, chunks: list[Chunk]):
    """删除旧 chunks，插入新 chunks。"""
    esc = database.escape_literal
    eint = database.escape_int

    database.execute(conn, f"DELETE FROM note_chunks WHERE note_id = {eint(note_id)}")

    for chunk in chunks:
        database.execute(
            conn,
            f"""INSERT INTO note_chunks
                (note_id, chunk_index, heading, line_start, line_end, content,
                 dir_type, year, tags)
              VALUES
                ({eint(note_id)}, {eint(chunk.chunk_index)},
                 {esc(chunk.heading or '')},
                 {eint(chunk.line_start)}, {eint(chunk.line_end)},
                 {esc(chunk.content)},
                 {esc(note.meta.dir_type)},
                 {eint(note.meta.year) if note.meta.year else 'NULL'},
                 {esc(note.meta.tags)})"""
        )


def ingest_file(conn, file_path: Path, root: str | None = None) -> tuple[int, int, bool]:
    """处理单个文件，返回 (chunk_count, skipped, is_empty)。
    
    is_empty: 文件无内容（切块后为 0 块）
    """
    dir_type = get_dir_type(file_path, root)
    note = parse_note(file_path, dir_type)

    # 检查 hash 是否变化
    rows = database.fetchall(
        conn,
        f"SELECT file_hash FROM notes WHERE file_path = {database.escape_literal(str(file_path))}"
    )
    if rows and rows[0][0] == note.file_hash:
        return 0, 1, False  # 无变化，跳过

    chunks = chunk_body(note.body, line_offset=note.body_line_offset)
    if not chunks:
        return 0, 0, True  # 无内容

    note_id = _upsert_note(conn, note)
    _insert_chunks(conn, note_id, note, chunks)
    conn.commit()

    return len(chunks), 0, False


def run_ingest(root: str | None = None) -> dict:
    """对笔记库执行完整 ingest。

    Returns:
        统计信息 dict: {files_processed, files_skipped, files_empty, total_chunks, empty_files}
    """
    database.init_db()
    conn = database.get_connection()

    files = scan_notes(root)
    stats = {
        "files_processed": 0,
        "files_skipped": 0,
        "files_empty": 0,
        "total_chunks": 0,
        "empty_files": [],
    }

    try:
        for fp in files:
            chunks, skipped, is_empty = ingest_file(conn, fp, root)
            if skipped:
                stats["files_skipped"] += 1
            elif is_empty:
                stats["files_empty"] += 1
                stats["empty_files"].append(str(fp))
            else:
                stats["files_processed"] += 1
                stats["total_chunks"] += chunks
    finally:
        conn.close()

    return stats


if __name__ == "__main__":
    root = sys.argv[1] if len(sys.argv) > 1 else None
    result = run_ingest(root)
    print(f"处理文件: {result['files_processed']}")
    print(f"跳过文件: {result['files_skipped']}")
    print(f"总块数:   {result['total_chunks']}")
