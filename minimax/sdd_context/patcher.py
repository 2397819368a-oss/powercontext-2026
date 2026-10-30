"""从模型响应里取出补丁并落到文件上。

**刻意不让模型输出 unified diff**：模型写的行号几乎总是对不上，评审一跑
就崩。改为「模型只输出目标符号的完整实现 → 本地用 ast 定位替换 → 本地用
difflib 生成差异」。这样差异一定真实，且可回滚。
"""

from __future__ import annotations

import ast
import difflib
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

_FENCE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL)


def extract_code(response: str) -> str | None:
    """取出响应中最长的 Python 代码块。"""

    blocks = [match.group(1).strip() for match in _FENCE.finditer(response)]
    if not blocks:
        stripped = response.strip()
        return stripped if stripped.startswith(("def ", "async def ", "class ")) else None
    return max(blocks, key=len)


@dataclass(slots=True)
class PatchResult:
    applied: bool
    target_symbol: str
    diff: str
    backup_path: Path | None = None
    error: str | None = None


def _symbol_span(source: str, symbol: str) -> tuple[int, int] | None:
    """返回顶层符号在源码中的 [start, end] 行号（1-based，含端点）。"""

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == symbol:
            start = min([node.lineno] + [d.lineno for d in node.decorator_list])
            return start, node.end_lineno or node.lineno
    return None


def apply_patch(
    *,
    target_file: Path,
    symbol: str,
    new_source: str,
    backup_dir: Path,
) -> PatchResult:
    """用 ``new_source`` 替换 ``target_file`` 里的顶层 ``symbol``。"""

    original = target_file.read_text(encoding="utf-8")
    span = _symbol_span(original, symbol)
    if span is None:
        return PatchResult(False, symbol, "", error=f"未在 {target_file} 中找到顶层符号 {symbol}")

    start, end = span
    lines = original.splitlines()
    replacement = new_source.rstrip("\n").splitlines()
    updated_lines = lines[: start - 1] + replacement + lines[end:]
    updated = "\n".join(updated_lines) + "\n"

    # 语法校验：改完必须还能被解析，否则不落盘
    try:
        ast.parse(updated)
    except SyntaxError as exc:
        return PatchResult(False, symbol, "", error=f"补丁引入语法错误: {exc}")

    # 两级备份：
    #   original/  只写一次，保存**最初**的原始文件，reset 依赖它；
    #   <时间戳>-  每次运行前都留一份，便于逐轮回溯。
    # 只在单级目录里覆盖写会把最初的原始版本冲掉（踩过：--resume 那次
    # 的备份是已改好的文件，导致 reset 还原不回改前状态）。
    pristine_dir = backup_dir / "original"
    pristine_dir.mkdir(parents=True, exist_ok=True)
    pristine = pristine_dir / target_file.name
    if not pristine.exists():
        pristine.write_text(original, encoding="utf-8")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = backup_dir / f"{stamp}-{target_file.name}"
    backup.write_text(original, encoding="utf-8")

    diff = "\n".join(
        difflib.unified_diff(
            lines,
            updated_lines,
            fromfile=f"a/{target_file.name}",
            tofile=f"b/{target_file.name}",
            lineterm="",
        )
    )
    target_file.write_text(updated, encoding="utf-8")
    return PatchResult(True, symbol, diff.rstrip("\n") + "\n", backup_path=backup)


def restore(backup_path: Path | None, target_file: Path) -> bool:
    """从备份还原。"""

    if backup_path is None or not backup_path.exists():
        return False
    target_file.write_text(backup_path.read_text(encoding="utf-8"), encoding="utf-8")
    return True
