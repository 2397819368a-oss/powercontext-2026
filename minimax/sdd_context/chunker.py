"""把工程资料切成可检索的块。

三种来源、两种切法：

- 规格（markdown）：按**二级标题**切。刻意不按三级标题切——规格的逻辑单元
  是「§2 筛选条件」这样的整节，按 §2.1/§2.2 切开会让两行的小节拼不过长节，
  新增的短小节几乎永远进不了 top-k（实测分数 2.98 vs 22.63）。
- 代码/测试（python）：按顶层函数/类切，用 ``ast`` 取真实行号

每块都带 ``doc_path`` / ``symbol`` / ``start_line`` / ``end_line``，
因为验收要求「输出中包含必要的规格、代码和测试来源」。
"""

from __future__ import annotations

import ast
import hashlib
import re
from pathlib import Path
from typing import Any

KIND_BY_DIR = {"specs": "spec", "tests": "test", "src": "code"}

# 这些目录不参与索引
SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache", "var", "node_modules"}

_HEADING = re.compile(r"^(#{2})\s+(.+?)\s*$", re.MULTILINE)


def _chunk_id(doc_path: str, symbol: str, start: int) -> str:
    digest = hashlib.sha1(f"{doc_path}|{symbol}|{start}".encode()).hexdigest()[:12]
    return f"{digest}"


def kind_of(relative: Path) -> str:
    for part in relative.parts:
        if part in KIND_BY_DIR:
            return KIND_BY_DIR[part]
    return "code"


def chunk_markdown(path: Path, relative: Path) -> list[dict[str, Any]]:
    """按二级标题切；无标题时整篇成块。"""

    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    marks = [(m.start(), m.group(2).strip()) for m in _HEADING.finditer(text)]
    if not marks:
        return [
            {
                "doc_path": relative.as_posix(),
                "kind": "spec",
                "symbol": path.stem,
                "start_line": 1,
                "end_line": len(lines),
                "body": text,
            }
        ]

    spans: list[tuple[int, int, str]] = []
    offsets = [m[0] for m in marks]
    for index, (start, title) in enumerate(marks):
        end = offsets[index + 1] if index + 1 < len(offsets) else len(text)
        spans.append((start, end, title))

    chunks: list[dict[str, Any]] = []
    for start, end, title in spans:
        body = text[start:end].strip()
        if len(body) < 20:  # 只有标题没有内容，跳过
            continue
        start_line = text[:start].count("\n") + 1
        end_line = start_line + body.count("\n")
        chunks.append(
            {
                "doc_path": relative.as_posix(),
                "kind": "spec",
                "symbol": title,
                "start_line": start_line,
                "end_line": end_line,
                "body": body,
            }
        )
    return chunks


def chunk_python(path: Path, relative: Path) -> list[dict[str, Any]]:
    """按顶层函数/类切；解析失败时整文件成块（不让索引因语法错误中断）。"""

    text = path.read_text(encoding="utf-8")
    kind = kind_of(relative)
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return [
            {
                "doc_path": relative.as_posix(),
                "kind": kind,
                "symbol": path.stem,
                "start_line": 1,
                "end_line": len(text.splitlines()),
                "body": text,
            }
        ]

    lines = text.splitlines()
    chunks: list[dict[str, Any]] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        start = min([node.lineno] + [d.lineno for d in node.decorator_list])
        end = node.end_lineno or node.lineno
        body = "\n".join(lines[start - 1 : end])
        chunks.append(
            {
                "doc_path": relative.as_posix(),
                "kind": kind,
                "symbol": node.name,
                "start_line": start,
                "end_line": end,
                "body": body,
            }
        )

    if not chunks:  # 只有模块级语句（如 __init__.py）
        chunks.append(
            {
                "doc_path": relative.as_posix(),
                "kind": kind,
                "symbol": path.stem,
                "start_line": 1,
                "end_line": len(lines),
                "body": text,
            }
        )
    return chunks


def collect(workspace: Path, *, generation: int) -> list[dict[str, Any]]:
    """遍历 workspace，产出全部块（带 generation 供整体替换）。"""

    rows: list[dict[str, Any]] = []
    for path in sorted(workspace.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix not in {".md", ".py"}:
            continue
        if SKIP_DIRS.intersection(path.parts):
            continue
        relative = path.relative_to(workspace)
        pieces = chunk_markdown(path, relative) if path.suffix == ".md" else chunk_python(path, relative)
        for piece in pieces:
            piece["generation"] = generation
            piece["chunk_id"] = _chunk_id(piece["doc_path"], piece["symbol"], piece["start_line"])
            rows.append(piece)
    return rows
