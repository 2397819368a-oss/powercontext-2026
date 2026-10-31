"""扫描笔记目录，返回所有 .md 文件路径。"""

import os
from pathlib import Path

from backend.app.config import EXCLUDED_DIRS, NOTES_ROOT


def scan_notes(root: str | None = None) -> list[Path]:
    """递归扫描笔记目录，返回所有 .md 文件路径（排除隐藏目录）。

    Args:
        root: 笔记根目录，默认使用 config.NOTES_ROOT

    Returns:
        按路径排序的 .md 文件列表
    """
    root_path = Path(root or NOTES_ROOT)
    if not root_path.exists():
        return []

    results = []
    for dirpath, dirnames, filenames in os.walk(root_path):
        # 就地修改 dirnames 以排除不需要的目录
        dirnames[:] = [
            d for d in dirnames
            if d not in EXCLUDED_DIRS and not d.startswith(".")
        ]
        dirnames.sort()  # 保证遍历顺序稳定

        for fname in sorted(filenames):
            if fname.lower().endswith(".md"):
                results.append(Path(dirpath) / fname)

    return results


def get_dir_type(file_path: Path, root: str | None = None) -> str:
    """从文件路径推断 dir_type（相对于笔记根目录的第一层目录名）。

    例如：D:/笔记/12-原子卡片/xxx.md → "12-原子卡片"
    """
    root_path = Path(root or NOTES_ROOT)
    try:
        rel = file_path.relative_to(root_path)
        parts = rel.parts
        if len(parts) >= 2:
            return parts[0]
        return ""
    except ValueError:
        return ""
