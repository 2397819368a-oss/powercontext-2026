"""测试 chunker 模块：验证切块数量和行号正确性。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.app.ingest.chunker import chunk_body, Chunk


# ── 测试用例 1：无 H2 的纯文本 ──────────────────────────────

MD_NO_H2 = """这是一篇没有 H2 标题的笔记。

它有三行内容。
最后一行在这里。"""


def test_no_h2_single_chunk():
    """无 H2 时，整文件应为一个块。"""
    chunks = chunk_body(MD_NO_H2)
    assert len(chunks) == 1

    c = chunks[0]
    assert c.chunk_index == 0
    assert c.heading is None
    assert c.line_start == 1
    assert c.line_end == 4  # 4 行文本
    assert "没有 H2" in c.content


# ── 测试用例 2：多个 H2 ──────────────────────────────────────

MD_MULTI_H2 = """---
title: 测试笔记
date: 2024-03-15
---

这是开头的一段文字，在第一个 H2 之前。

## 第一部分

这是第一部分的内容。
包含两行。

## 第二部分

这是第二部分。

### 子标题（不影响切块）

子标题下面的内容。

## 第三部分

最后一个部分。"""


def test_multi_h2_chunks():
    """多个 H2 应正确切分，行号准确。"""
    chunks = chunk_body(MD_MULTI_H2)

    # 应有 4 个块：前言 + 3 个 H2
    assert len(chunks) == 4

    # chunk 0: 前言部分
    c0 = chunks[0]
    assert c0.chunk_index == 0
    assert c0.heading is None
    assert c0.line_start == 1
    assert c0.line_end == 7  # frontmatter 4 行 + 空行 + 文字 + 空行
    assert "开头" in c0.content

    # chunk 1: 第一部分
    c1 = chunks[1]
    assert c1.chunk_index == 1
    assert c1.heading == "第一部分"
    assert c1.line_start == 8  # "## 第一部分" 在第 8 行
    assert "第一部分" in c1.content

    # chunk 2: 第二部分（含子标题）
    c2 = chunks[2]
    assert c2.chunk_index == 2
    assert c2.heading == "第二部分"
    assert "子标题" in c2.content

    # chunk 3: 第三部分
    c3 = chunks[3]
    assert c3.chunk_index == 3
    assert c3.heading == "第三部分"
    assert c3.line_end == len(MD_MULTI_H2.split("\n"))


# ── 测试用例 3：H2 紧邻 + 空内容 ──────────────────────────────

MD_ADJACENT_H2 = """## 第一个

内容一。

## 第二个

## 第三个

内容三。"""


def test_adjacent_h2():
    """相邻 H2（中间无内容）应产生空块。"""
    chunks = chunk_body(MD_ADJACENT_H2)

    assert len(chunks) == 3

    assert chunks[0].heading == "第一个"
    assert "内容一" in chunks[0].content

    assert chunks[1].heading == "第二个"
    # 第二个 H2 和第三个 H2 之间只有空行，content 可能为空
    # 但块本身必须存在
    assert chunks[1].chunk_index == 1

    assert chunks[2].heading == "第三个"
    assert "内容三" in chunks[2].content


# ── 测试用例 4：行号精确验证 ──────────────────────────────────

MD_LINE_NUMBERS = """第一行
第二行
## 标题A
第四行
第五行
## 标题B
第七行"""


def test_line_numbers_exact():
    """精确验证行号计算。"""
    chunks = chunk_body(MD_LINE_NUMBERS)

    # chunk 0: 行 1-2（H2 之前的内容）
    assert chunks[0].line_start == 1
    assert chunks[0].line_end == 2
    assert chunks[0].heading is None

    # chunk 1: 行 3-5（## 标题A 到 ## 标题B 之前）
    assert chunks[1].line_start == 3
    assert chunks[1].line_end == 5
    assert chunks[1].heading == "标题A"

    # chunk 2: 行 6-7（## 标题B 到文件末尾）
    assert chunks[2].line_start == 6
    assert chunks[2].line_end == 7
    assert chunks[2].heading == "标题B"


# ── 测试用例 5：空文件 ──────────────────────────────────────

def test_empty_body():
    """空正文应返回空列表。"""
    chunks = chunk_body("")
    assert chunks == []

    chunks2 = chunk_body("   \n\n  ")
    assert chunks2 == []


# ── 测试用例 6：带 frontmatter 的行号偏移 ──────────────────────

MD_WITH_FRONTMATTER = """---
title: 测试笔记
date: 2024-03-15
tags:
  - 标签一
  - 标签二
---

这是正文的第一行。

## 第一个章节

这是第一个章节的内容。

## 第二个章节

这是第二个章节的内容。"""


def test_frontmatter_line_offset():
    """带 frontmatter 的文件，行号应加上 frontmatter 的偏移量。

    frontmatter 占 8 行（--- + 6 行 YAML + --- + 1 空行），
    所以正文第一行是原文件的第 9 行。
    """
    from backend.app.ingest.parser import parse_frontmatter

    fm, body, offset = parse_frontmatter(MD_WITH_FRONTMATTER)

    # frontmatter 应被正确解析
    assert fm.get("title") == "测试笔记"
    assert offset == 8  # frontmatter 占 8 行

    # 对 body 切块，传入 offset
    chunks = chunk_body(body, line_offset=offset)

    # 应有 3 个块：前言 + 2 个 H2
    assert len(chunks) == 3

    # chunk 0: 前言部分（body 的第 1-2 行 = 原文件的第 9-10 行）
    c0 = chunks[0]
    assert c0.line_start == 9  # offset(8) + body_line(1)
    assert c0.line_end == 10    # offset(8) + body_line(2)
    assert "正文的第一行" in c0.content

    # chunk 1: 第一个章节（body 的第 3 行 = 原文件的第 11 行）
    c1 = chunks[1]
    assert c1.heading == "第一个章节"
    assert c1.line_start == 11  # offset(8) + body_line(3)
    assert "第一个章节的内容" in c1.content

    # chunk 2: 第二个章节
    c2 = chunks[2]
    assert c2.heading == "第二个章节"
    assert "第二个章节的内容" in c2.content

    # 验证：用原文件的行号来对照
    original_lines = MD_WITH_FRONTMATTER.split("\n")
    for chunk in chunks:
        # 从原文件按行号截取内容
        extracted = "\n".join(
            original_lines[chunk.line_start - 1 : chunk.line_end]
        )
        # 截取的内容应包含 chunk 的 content（可能有多余空行）
        assert chunk.content in extracted or extracted.strip() in chunk.content, (
            f"chunk lines {chunk.line_start}-{chunk.line_end} 与原文件不匹配"
        )


# ── 测试用例 7：末尾换行符不导致行号越界 ──────────────────────

MD_TRAILING_NEWLINE = """---
title: 测试
---

## 第一章

内容一。

## 第二章

内容二。
"""  # 注意：这里有一个末尾换行符


def test_trailing_newline_no_overflow():
    """文件以换行符结尾时，最后一个块的 line_end 不应超过文件总行数。"""
    from backend.app.ingest.parser import parse_frontmatter

    fm, body, offset = parse_frontmatter(MD_TRAILING_NEWLINE)
    chunks = chunk_body(body, line_offset=offset)

    # 应有 2 个块（2 个 H2）
    assert len(chunks) == 2

    # 计算原文件的实际行数
    original_lines = MD_TRAILING_NEWLINE.split("\n")
    # 如果以 \n 结尾，split 会多出一个空元素，实际行数 = len - 1
    if MD_TRAILING_NEWLINE.endswith("\n"):
        actual_total = len(original_lines) - 1
    else:
        actual_total = len(original_lines)

    # 最后一个块的 line_end 不应超过实际总行数
    last_chunk = chunks[-1]
    assert last_chunk.line_end <= actual_total, (
        f"line_end={last_chunk.line_end} 超过文件总行数={actual_total}"
    )

    # 且 line_end 应等于实际总行数（因为最后一个块延伸到文件末尾）
    assert last_chunk.line_end == actual_total, (
        f"最后一个块 line_end={last_chunk.line_end}，应为 {actual_total}"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
