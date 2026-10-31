"""全文检索引擎。

关键约束（实测验证）：
1. seekdb 不支持参数绑定，所有值必须 escape 后拼串。
2. MATCH(content, heading) 会报错 code=1191，因为 ft_content 和 ft_heading
   是两个独立的单列全文索引，不存在联合索引 (content, heading)。
   统一使用单列 MATCH(content) AGAINST(...)。
   content 已包含 H2 标题行（见 chunker.py），因此标题文字同样可被检索到。
3. 使用默认自然语言模式（不写 IN BOOLEAN MODE），score 才有排序意义。
"""

from backend.app.database import escape_literal, escape_int, fetchall


def build_sql(keyword: str, filters: dict | None = None, top_k: int = 10) -> str:
    """构造检索 SQL 字符串（不执行）。

    用于在 API 响应中返回实际执行的 SQL，供前端展示核对。
    """
    filters = filters or {}
    esc = escape_literal

    where_parts = [f"MATCH(c.content) AGAINST({esc(keyword)})"]
    filter_clauses = []

    if filters.get("dir_type"):
        filter_clauses.append(f"c.dir_type = {esc(filters['dir_type'])}")

    if filters.get("year") is not None:
        filter_clauses.append(f"n.year = {escape_int(filters['year'])}")

    if filters.get("year_from") is not None:
        filter_clauses.append(f"n.year >= {escape_int(filters['year_from'])}")

    if filters.get("year_to") is not None:
        filter_clauses.append(f"n.year <= {escape_int(filters['year_to'])}")

    if filters.get("tags"):
        filter_clauses.append(f"c.tags LIKE {esc('%' + filters['tags'] + '%')}")

    if filter_clauses:
        where_parts.extend(filter_clauses)

    where_sql = " AND ".join(where_parts)

    return (
        f"SELECT c.id, c.heading, c.line_start, c.line_end, c.content, "
        f"n.file_path, c.dir_type, c.year, c.tags, "
        f"MATCH(c.content) AGAINST({esc(keyword)}) AS score "
        f"FROM note_chunks c "
        f"JOIN notes n ON n.id = c.note_id "
        f"WHERE {where_sql} "
        f"ORDER BY score DESC "
        f"LIMIT {escape_int(top_k)}"
    )


def _clean_markdown(text: str) -> str:
    """去除 Markdown 格式符号，保留纯文本。

    顺序：
    1. 去掉行首的 #、>、列表符、数字编号
    2. 去掉代码围栏（``` 整行）
    3. 去掉表格分隔行（|---|---| 这类）
    4. 把 | 替换成空格
    5. 去掉 ** __ ` 装饰符（保留内部文字）
    6. 折叠换行和连续空格为单空格
    """
    import re
    lines = text.split("\n")
    cleaned_lines = []
    for line in lines:
        # 去掉代码围栏
        if line.strip().startswith("```"):
            continue
        # 去掉表格分隔行
        if re.match(r"^\s*\|?[\s:\-|]+\|?\s*$", line):
            continue
        # 去掉行首的 # + 空格
        line = line.lstrip("#").lstrip()
        # 去掉行首的 > + 空格
        line = line.lstrip(">").lstrip()
        # 去掉行首的列表符（- / * / +）+ 空格
        if line and line[0] in ("-", "*", "+") and len(line) > 1 and line[1] == " ":
            line = line[2:]
        # 去掉行首的数字编号（如 "1. "）
        if line and line[0].isdigit():
            i = 0
            while i < len(line) and line[i].isdigit():
                i += 1
            if i < len(line) and line[i] == "." and i + 1 < len(line) and line[i + 1] == " ":
                line = line[i + 2:]
        # 把 | 替换成空格
        line = line.replace("|", " ")
        cleaned_lines.append(line)

    text = "\n".join(cleaned_lines)

    # 去掉 ** __ ` 装饰符（保留内部文字）
    text = text.replace("**", "").replace("__", "").replace("`", "")

    # 折叠换行和连续空格为单空格
    text = re.sub(r"\s+", " ", text).strip()

    return text


def _clean_markdown_lines(text: str) -> list[str]:
    """逐行清洗 Markdown，保留行结构（用于首行判断）。"""
    import re
    lines = text.split("\n")
    cleaned_lines = []
    for line in lines:
        # 去掉代码围栏
        if line.strip().startswith("```"):
            continue
        # 去掉表格分隔行（|---|---| 这类）
        if re.match(r"^\s*\|?[\s:\-|]+\|?\s*$", line):
            continue
        # 去掉行首的 # + 空格
        line = line.lstrip("#").lstrip()
        # 去掉行首的 > + 空格
        line = line.lstrip(">").lstrip()
        # 去掉行首的列表符（- / * / +）+ 空格
        if line and line[0] in ("-", "*", "+") and len(line) > 1 and line[1] == " ":
            line = line[2:]
        # 去掉行首的数字编号（如 "1. "）
        if line and line[0].isdigit():
            i = 0
            while i < len(line) and line[i].isdigit():
                i += 1
            if i < len(line) and line[i] == "." and i + 1 < len(line) and line[i + 1] == " ":
                line = line[i + 2:]
        # 把 | 替换成空格（表格竖线）
        line = line.replace("|", " ")
        cleaned_lines.append(line)

    # 去掉 ** __ ` 装饰符（保留内部文字）
    cleaned_lines = [line.replace("**", "").replace("__", "").replace("`", "") for line in cleaned_lines]

    return cleaned_lines


def _make_snippet(content: str, keyword: str, heading: str = "", width: int = 120) -> str:
    """以关键词首次命中位置为中心截取摘要窗口。

    流程：
    1. 在原始 content 上 find 关键词位置
    2. 逐行清洗 Markdown（保留行结构）
    3. 如果首行清洗后等于 heading 或为空，丢弃首行
    4. 折叠为单行文本
    5. 在清洗后文本上重新 find 关键词位置
    6. 截取 width 宽度窗口，两端补 "..."
    """
    if not content:
        return ""

    # 步骤 1：在原始 content 上找关键词位置
    original_pos = content.lower().find(keyword.lower()) if keyword else -1

    # 步骤 2：逐行清洗 Markdown（保留行结构）
    cleaned_lines = _clean_markdown_lines(content)

    # 步骤 3：如果首行清洗后等于 heading 或为空，丢弃首行
    cleaned_heading = _clean_markdown(heading) if heading else ""
    
    if cleaned_lines and (cleaned_lines[0].strip() == cleaned_heading or not cleaned_lines[0].strip()):
        cleaned_lines = cleaned_lines[1:]

    # 步骤 4：折叠为单行文本
    import re
    cleaned = " ".join(line.strip() for line in cleaned_lines if line.strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    if not cleaned:
        return ""

    # 步骤 5：在清洗后文本上重新找关键词位置
    pos = cleaned.lower().find(keyword.lower()) if keyword else -1

    # 步骤 6：截取窗口
    if pos < 0:
        snippet = cleaned[:width]
        return snippet + ("..." if len(cleaned) > width else "")

    start = max(0, pos - width // 3)
    end = min(len(cleaned), start + width)
    if end - start < width:
        start = max(0, end - width)

    snippet = cleaned[start:end]
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(cleaned) else ""
    return prefix + snippet + suffix


def search(
    conn,
    keyword: str,
    filters: dict | None = None,
    top_k: int = 10,
) -> list[dict]:
    """在 note_chunks 上执行全文检索，返回排序后的结果列表。

    Args:
        conn: seekdb 连接（由调用方管理生命周期）
        keyword: 检索关键词
        filters: 可选过滤条件，支持：
            - dir_type: str，精确匹配
            - year: int，精确匹配
            - year_from: int，年份下限（含）
            - year_to: int，年份上限（含）
            - tags: str，LIKE 模糊匹配
        top_k: 返回结果数量上限

    Returns:
        list[dict]，每条包含：
            chunk_id, file_path, heading, line_start, line_end,
            score, snippet, dir_type, year, tags
    """
    if not keyword or not keyword.strip():
        return []

    sql = build_sql(keyword, filters, top_k)
    rows = fetchall(conn, sql)

    results = []
    for row in rows:
        content_text = row[4] or ""
        results.append({
            "chunk_id": row[0],
            "file_path": row[5],
            "heading": row[1],
            "line_start": row[2],
            "line_end": row[3],
            "score": row[9],
            "snippet": _make_snippet(content_text, keyword, heading=row[1] or ""),
            "dir_type": row[6],
            "year": row[7],
            "tags": row[8],
        })

    return results
