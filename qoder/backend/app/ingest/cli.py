"""Ingest CLI：重建索引。"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from backend.app import database
from backend.app.ingest.pipeline import run_ingest


def cmd_ingest(args):
    """执行 ingest，支持 --rebuild 全量重建。"""
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

    print(f"开始扫描目录: {args.directory or '(默认)'}")
    start = time.time()
    stats = run_ingest(args.directory)
    elapsed = time.time() - start

    print(f"\n完成！耗时: {elapsed:.2f}s")
    print(f"  处理文件: {stats['files_processed']}")
    print(f"  跳过文件: {stats['files_skipped']}")
    print(f"  总块数:   {stats['total_chunks']}")


def main():
    parser = argparse.ArgumentParser(description="Ingest CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # ingest 子命令
    ingest_parser = subparsers.add_parser("ingest", help="重建索引")
    ingest_parser.add_argument("directory", nargs="?", default=None, help="笔记目录")
    ingest_parser.add_argument("--rebuild", action="store_true", help="清空后全量重建")
    ingest_parser.set_defaults(func=cmd_ingest)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
