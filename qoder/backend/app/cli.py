"""主 CLI：index / search / show 三条命令。"""

import argparse
import sys
import time
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.app import database
from backend.app.ingest.pipeline import run_ingest
from backend.app.search.engine import search


# ── 工具函数 ──────────────────────────────────────────────────

def display_width(s: str) -> int:
    """计算字符串的显示宽度（CJK 字符算 2，其他算 1）。"""
    width = 0
    for ch in str(s):
        ea = unicodedata.east_asian_width(ch)
        if ea in ("F", "W", "A"):
            width += 2
        else:
            width += 1
    return width


def pad_right(s: str, width: int) -> str:
    """右补空格到指定显示宽度。"""
    s = str(s)
    dw = display_width(s)
    if dw >= width:
        return s
    return s + " " * (width - dw)


def truncate(s: str, max_width: int) -> str:
    """截断字符串到指定显示宽度，超出部分用 ... 替换。"""
    s = str(s)
    if display_width(s) <= max_width:
        return s
    result = ""
    for ch in s:
        ea = unicodedata.east_asian_width(ch)
        w = 2 if ea in ("F", "W", "A") else 1
        if display_width(result) + w + 3 > max_width:
            return result + "..."
        result += ch
    return result


def print_table(headers: list[str], rows: list[list[str]], col_widths: list[int]):
    """打印对齐的表格。"""
    # 打印表头
    header_line = " | ".join(
        pad_right(h, w) for h, w in zip(headers, col_widths)
    )
    print(header_line)
    print("-+-".join("-" * w for w in col_widths))

    # 打印行
    for row in rows:
        line = " | ".join(
            pad_right(truncate(cell, w), w)
            for cell, w in zip(row, col_widths)
        )
        print(line)


# ── 命令实现 ──────────────────────────────────────────────────

def cmd_index(args):
    """重建索引。"""
    if args.rebuild:
        print("正在清空数据库并全量重建...")
        conn = database.get_connection()
        try:
            database.execute(conn, "DELETE FROM note_chunks")
            database.execute(conn, "DELETE FROM notes")
            conn.commit()
            print("已清空 notes 和 note_chunks 表")
        finally:
            conn.close()

    directory = args.directory
    print(f"开始扫描目录: {directory or '(默认)'}")
    start = time.time()
    stats = run_ingest(directory)
    elapsed = time.time() - start

    print(f"\n本次运行统计（耗时 {elapsed:.2f}s）：")
    print(f"  新增文件: {stats['files_processed']}")
    print(f"  新增块数: {stats['total_chunks']}")
    print(f"  跳过文件（hash 未变）: {stats['files_skipped']}")
    
    if stats['files_empty'] > 0:
        print(f"  无内容文件: {stats['files_empty']}")
        empty_list = stats['empty_files']
        if len(empty_list) <= 3:
            for f in empty_list:
                print(f"    - {Path(f).name}")
        else:
            for f in empty_list[:3]:
                print(f"    - {Path(f).name}")
            print(f"    ... 等共 {len(empty_list)} 个")

    # 查询库内合计
    conn = database.get_connection()
    try:
        notes_count = database.fetchone(conn, "SELECT COUNT(*) FROM notes")[0]
        chunks_count = database.fetchone(conn, "SELECT COUNT(*) FROM note_chunks")[0]
    finally:
        conn.close()
        database.close_db()

    print(f"\n库内合计: {notes_count} 篇 / {chunks_count} 块")


def cmd_search(args):
    """搜索笔记。"""
    keyword = args.keyword
    if not keyword or not keyword.strip():
        print("错误：请提供搜索关键词", file=sys.stderr)
        sys.exit(1)

    filters = {}
    if args.type:
        # 映射简写到完整 dir_type
        type_map = {
            "moc": "10-MOC与索引",
            "exam": "11-赛题归档",
            "card": "12-原子卡片",
            "code": "13-代码片段",
            "literature": "14-文献卡片",
        }
        filters["dir_type"] = type_map.get(args.type, args.type)
    if args.tag:
        filters["tags"] = args.tag
    if args.year:
        filters["year"] = args.year

    database.init_db()
    conn = database.get_connection()
    try:
        results = search(conn, keyword, filters=filters, top_k=args.top_k)
    finally:
        conn.close()
        database.close_db()

    if not results:
        print(f"未找到匹配 '{keyword}' 的结果")
        return

    print(f"\n搜索 '{keyword}'，找到 {len(results)} 条结果：\n")

    headers = ["#", "score", "文件", "小节", "行号"]
    col_widths = [4, 8, 50, 30, 10]
    rows = []
    for i, r in enumerate(results, 1):
        # 提取文件名（去掉路径前缀）
        file_path = r["file_path"]
        try:
            file_name = Path(file_path).name
        except Exception:
            file_name = file_path

        rows.append([
            str(i),
            f"{r['score']:.4f}",
            file_name,
            r["heading"] or "(前言)",
            f"{r['line_start']}-{r['line_end']}",
        ])

    print_table(headers, rows, col_widths)


def cmd_show(args):
    """显示指定 chunk 的原文。"""
    chunk_id = args.chunk_id

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
        print(f"错误：未找到 chunk_id = {chunk_id}", file=sys.stderr)
        sys.exit(1)

    content, line_start, line_end, heading, file_path = row

    print(f"Chunk ID: {chunk_id}")
    print(f"文件: {file_path}")
    print(f"小节: {heading or '(前言)'}")
    print(f"行号: {line_start}-{line_end}")
    print("-" * 60)

    # 读取原文件，显示 chunk 内容并带行号前缀
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        # 显示 chunk 前后各 2 行上下文
        context_before = max(0, line_start - 3)
        context_after = min(len(lines), line_end + 2)

        for i in range(context_before, context_after):
            line_num = i + 1
            line_content = lines[i].rstrip("\n")
            marker = ">>>" if line_start <= line_num <= line_end else "   "
            print(f"{marker} {line_num:4d} | {line_content}")
    except Exception as e:
        print(f"无法读取原文件: {e}", file=sys.stderr)
        print("\n数据库中的内容:")
        print(content)


# ── 主入口 ────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="笔记检索 CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python -m backend.app.cli index                          # 增量索引
  python -m backend.app.cli index --rebuild                # 全量重建
  python -m backend.app.cli search "重排"                   # 搜索
  python -m backend.app.cli search "缓存" --type card      # 带过滤
  python -m backend.app.cli show 12                        # 查看 chunk
        """
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # index 子命令
    index_parser = subparsers.add_parser("index", help="重建索引")
    index_parser.add_argument("directory", nargs="?", default=None, help="笔记目录")
    index_parser.add_argument("--rebuild", action="store_true", help="清空后全量重建")
    index_parser.set_defaults(func=cmd_index)

    # search 子命令
    search_parser = subparsers.add_parser("search", help="搜索笔记")
    search_parser.add_argument("keyword", help="搜索关键词")
    search_parser.add_argument("--type", help="目录类型过滤 (moc/exam/card/code/literature)")
    search_parser.add_argument("--tag", help="标签过滤")
    search_parser.add_argument("--year", type=int, help="年份过滤")
    search_parser.add_argument("--top-k", type=int, default=10, help="返回结果数 (默认 10)")
    search_parser.set_defaults(func=cmd_search)

    # show 子命令
    show_parser = subparsers.add_parser("show", help="显示 chunk 原文")
    show_parser.add_argument("chunk_id", type=int, help="Chunk ID")
    show_parser.set_defaults(func=cmd_show)

    try:
        args = parser.parse_args()
        args.func(args)
    except KeyboardInterrupt:
        print("\n已取消")
        sys.exit(130)
    except Exception as e:
        print(f"错误：{e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
