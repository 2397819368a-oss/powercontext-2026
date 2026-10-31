"""解析 Markdown 文件的 frontmatter（YAML）和正文。"""

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date as DateType, datetime
from pathlib import Path

import yaml

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


@dataclass
class NoteMeta:
    """从 frontmatter 提取的元数据。"""
    title: str | None = None
    date: DateType | None = None
    year: int | None = None
    tags: str = ""
    status: str | None = None
    dir_type: str = ""


@dataclass
class ParsedNote:
    """解析后的笔记：元数据 + 正文 + 文件信息。"""
    file_path: str
    file_hash: str
    meta: NoteMeta
    body: str  # frontmatter 之后的正文
    body_line_offset: int = 0  # frontmatter 占用的行数，body 的行号需要加上此偏移才是原文件行号


def compute_file_hash(file_path: Path) -> str:
    """计算文件的 SHA-256 哈希。"""
    h = hashlib.sha256()
    h.update(file_path.read_bytes())
    return h.hexdigest()


def parse_frontmatter(text: str) -> tuple[dict, str, int]:
    """分离 frontmatter 和正文。

    Returns:
        (frontmatter_dict, body_text, frontmatter_line_count)
        frontmatter_line_count: frontmatter 占用的行数（含 --- 分隔符和后续空行）
    """
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text, 0

    raw_yaml = m.group(1)
    body = text[m.end():]

    # 计算 frontmatter 占用的行数
    frontmatter_text = text[:m.end()]
    frontmatter_line_count = frontmatter_text.count("\n")

    try:
        fm = yaml.safe_load(raw_yaml) or {}
    except yaml.YAMLError:
        fm = {}

    if not isinstance(fm, dict):
        return {}, text, 0

    return fm, body, frontmatter_line_count


def _parse_date(value) -> DateType | None:
    """尝试将各种格式的值解析为 date 对象。"""
    if value is None:
        return None
    if isinstance(value, DateType):
        return value
    if isinstance(value, datetime):
        return value.date()

    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y年%m月%d日", "%Y.%m.%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def extract_meta(fm: dict, dir_type: str = "") -> NoteMeta:
    """从 frontmatter dict 提取 NoteMeta。

    支持 date 和 created 两种字段名。
    """
    # date 字段：优先 date，其次 created
    d = _parse_date(fm.get("date"))
    if d is None:
        d = _parse_date(fm.get("created"))

    # tags：列表则 join，字符串则直接用
    raw_tags = fm.get("tags", "")
    if isinstance(raw_tags, list):
        tags = ",".join(str(t) for t in raw_tags)
    elif isinstance(raw_tags, str):
        tags = raw_tags
    else:
        tags = str(raw_tags) if raw_tags else ""

    return NoteMeta(
        title=fm.get("title"),
        date=d,
        year=d.year if d else None,
        tags=tags,
        status=fm.get("status"),
        dir_type=dir_type,
    )


def parse_note(file_path: Path, dir_type: str = "") -> ParsedNote:
    """解析单个笔记文件。"""
    text = file_path.read_text(encoding="utf-8")
    file_hash = compute_file_hash(file_path)

    fm, body, line_offset = parse_frontmatter(text)
    meta = extract_meta(fm, dir_type)

    return ParsedNote(
        file_path=str(file_path),
        file_hash=file_hash,
        meta=meta,
        body=body,
        body_line_offset=line_offset,
    )
