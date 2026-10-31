"""从 seekdb 导出全部 chunk 为静态 JSON，供 Qoder Sites 静态站点使用。

输出：
  site/data/chunks.json  - 全部 chunk 列表（按 id 升序）
  site/data/meta.json    - 元数据（生成时间、统计信息）

隐私：file_path 写相对笔记根的路径（正斜杠），不暴露本机绝对路径和用户名。
"""

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

# 确保能导入 backend 模块
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app import database

# 笔记根目录（用于计算相对路径）
NOTES_ROOT = "D:/新建文件夹/Obsidian/吉米"


def make_relative_path(absolute_path: str) -> str:
    """将绝对路径转换为相对笔记根的路径（正斜杠）。

    例：D:/新建文件夹/Obsidian/吉米/11-赛题归档/xxx.md
        → 11-赛题归档/xxx.md
    """
    # 统一为正斜杠
    normalized = absolute_path.replace("\\", "/")
    root_normalized = NOTES_ROOT.replace("\\", "/")

    if normalized.startswith(root_normalized):
        relative = normalized[len(root_normalized):].lstrip("/")
        return relative
    # 如果不在根目录下，返回文件名（兜底）
    return os.path.basename(absolute_path)


def sanitize_content(content: str) -> str:
    """脱敏笔记内容，移除用户名和绝对路径。

    - "王海彬" → "（用户名）"
    - "C:\\Users\\王海彬\\..." → "C:\\Users\\（用户名）\\..."
    - "D:\\..." → "（路径）"
    - "/c/Users/王海彬/..." → "/c/Users/（用户名）/..."
    """
    if not content:
        return content

    # 先处理包含用户名的路径（C:\Users\王海彬 或 /c/Users/王海彬）
    content = re.sub(r'C:\\Users\\王海彬', r'C:\\Users\\（用户名）', content)
    content = re.sub(r'/c/Users/王海彬', r'/c/Users/（用户名）', content)

    # 再处理独立出现的用户名
    content = content.replace('王海彬', '（用户名）')

    # 处理 D:\ 开头的绝对路径（Windows 风格）
    content = re.sub(r'D:\\[^`\s"\')\]]*', '（路径）', content)

    # 处理 D:/ 开头的绝对路径（正斜杠风格）
    content = re.sub(r'D:/[^`\s"\')\]]*', '（路径）', content)

    return content


def export_static(output_dir: str = "site/data"):
    """导出全部 chunk 到 JSON 文件。"""
    database.init_db()
    conn = database.get_connection()
    try:
        # 查询全部 chunk，关联 notes 表获取文件信息
        rows = database.fetchall(conn, """
            SELECT c.id, c.heading, c.line_start, c.line_end, c.content,
                   n.file_path, n.dir_type, n.year, n.tags
            FROM note_chunks c
            JOIN notes n ON n.id = c.note_id
            ORDER BY c.id ASC
        """)

        chunks = []
        for row in rows:
            chunk_id, heading, line_start, line_end, content, file_path, dir_type, year, tags = row

            # 提取文件名（只显示文件名，不显示全路径）
            file_name = os.path.basename(file_path)

            # 相对路径（脱敏，不暴露本机绝对路径）
            relative_path = make_relative_path(file_path)

            # 解析 tags（数据库里是逗号分隔的字符串）
            tags_list = []
            if tags:
                tags_list = [t.strip() for t in tags.split(",") if t.strip()]

            # 脱敏内容（移除用户名和绝对路径）
            sanitized_content = sanitize_content(content or "")

            chunks.append({
                "id": chunk_id,
                "file_name": file_name,
                "file_path": relative_path,
                "dir_type": dir_type or "",
                "year": year,
                "tags": tags_list,
                "heading": heading or "",
                "line_start": line_start,
                "line_end": line_end,
                "content": sanitized_content,
            })

        # 查询统计信息
        notes_count = database.fetchone(conn, "SELECT COUNT(*) FROM notes")[0]
        chunks_count = database.fetchone(conn, "SELECT COUNT(*) FROM note_chunks")[0]

    finally:
        conn.close()
        database.close_db()

    # 创建输出目录
    os.makedirs(output_dir, exist_ok=True)

    # 写入 chunks.json
    chunks_path = os.path.join(output_dir, "chunks.json")
    with open(chunks_path, "w", encoding="utf-8") as f:
        json.dump(chunks, f, ensure_ascii=False, indent=2)

    # 写入 meta.json（source_root 脱敏）
    meta_path = os.path.join(output_dir, "meta.json")
    meta = {
        "generated_at": datetime.now().isoformat(),
        "notes_count": notes_count,
        "chunks_count": chunks_count,
        "source_root": "（本机 Obsidian 库，已脱敏）",
    }
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    # 打印统计
    chunks_size = os.path.getsize(chunks_path)
    meta_size = os.path.getsize(meta_path)
    print(f"导出了 {len(chunks)} 个 chunk，{notes_count} 篇笔记")
    print(f"chunks.json: {chunks_size:,} 字节")
    print(f"meta.json: {meta_size:,} 字节")
    # 打印一条 file_path 示例（验证脱敏）
    if chunks:
        print(f"file_path 示例：{chunks[0]['file_path']}")


if __name__ == "__main__":
    export_static()
