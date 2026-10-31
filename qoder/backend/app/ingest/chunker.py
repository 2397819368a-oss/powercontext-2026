"""按 H2 标题将笔记正文切分为块，记录每块的起止行号（1-based）。"""

import re
from dataclasses import dataclass

from backend.app.config import H2_PATTERN

H2_RE = re.compile(H2_PATTERN)


@dataclass
class Chunk:
    """一个 H2 级别的文本块。"""
    chunk_index: int      # 块序号，从 0 开始
    heading: str | None   # H2 标题文本（去掉 "## " 前缀），第一个块可能为 None
    line_start: int       # 起始行号（1-based，含 H2 行）
    line_end: int         # 结束行号（1-based，含）
    content: str          # 块的正文内容


def chunk_body(body: str, line_offset: int = 0) -> list[Chunk]:
    """将正文按 H2 切分为块列表。

    规则：
      - chunk 0：从文件开头到第一个 H2 之前（若无 H2 则整文件为一个块）
      - chunk N：从第 N 个 H2 行开始，到下一个 H2 行之前或文件结束
      - 行号均为 1-based

    Args:
        body: frontmatter 之后的正文内容
        line_offset: frontmatter 占用的行数，加到所有行号上以得到原文件绝对行号

    Returns:
        Chunk 列表，按出现顺序排列
    """
    lines = body.split("\n")
    
    # 计算实际行数：如果 body 以 "\n" 结尾，split 会多出一个空元素
    if body.endswith("\n"):
        total_lines = len(lines) - 1
    else:
        total_lines = len(lines)
    
    chunks: list[Chunk] = []

    # 找到所有 H2 行的索引（在 lines 数组中的 0-based 索引）
    h2_indices = [i for i, line in enumerate(lines) if H2_RE.match(line)]

    if not h2_indices:
        # 没有 H2，整文件为一个块
        content = body.strip()
        if content:
            chunks.append(Chunk(
                chunk_index=0,
                heading=None,
                line_start=1 + line_offset,
                line_end=min(total_lines, len(lines)) + line_offset,
                content=content,
            ))
        return chunks

    # chunk 0：从开头到第一个 H2 之前
    if h2_indices[0] > 0:
        pre_lines = lines[:h2_indices[0]]
        content = "\n".join(pre_lines).strip()
        if content:
            chunks.append(Chunk(
                chunk_index=0,
                heading=None,
                line_start=1 + line_offset,
                line_end=min(h2_indices[0], total_lines) + line_offset,
                content=content,
            ))

    # chunk N：每个 H2 到下一个 H2 之前（或文件末尾）
    for n, h2_idx in enumerate(h2_indices):
        # 确定本块的结束位置
        if n + 1 < len(h2_indices):
            end_idx = h2_indices[n + 1] - 1  # 0-based，下一个 H2 的前一行
        else:
            end_idx = total_lines - 1  # 0-based，文件最后一行（用 actual total）

        # 提取 heading 文本
        heading_line = lines[h2_idx]
        heading = heading_line.lstrip("#").strip()

        # 提取内容
        chunk_lines = lines[h2_idx:end_idx + 1]
        content = "\n".join(chunk_lines).strip()

        # 计算在 chunks 列表中的序号
        chunk_index = len(chunks)

        chunks.append(Chunk(
            chunk_index=chunk_index,
            heading=heading,
            line_start=h2_idx + 1 + line_offset,     # 转为 1-based + offset
            line_end=min(end_idx + 1, total_lines) + line_offset,  # 封顶 + offset
            content=content,
        ))

    return chunks
