"""检查静态版与 seekdb 版的 Top-5 重合率。

使用与 site/app.js 完全相同的打分公式：
  score = 2.0 × 关键词出现次数 + 3.0 × (标题命中 ? 1 : 0) − 0.001 × content 长度

对每组查询，分别取 seekdb Top-5 和静态版 Top-5，比较 id 集合。
"""

import json
import os
import sys
from pathlib import Path

# 确保能导入 backend 模块
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app import database
from backend.app.search.engine import search as seekdb_search


def calculate_score_static(chunk, keyword):
    """静态版打分公式（与 site/app.js 保持一致）。"""
    if not keyword:
        return 0.0

    keyword_lower = keyword.lower()
    content_lower = chunk["content"].lower()
    heading_lower = chunk["heading"].lower()

    # 关键词在 content 中出现的次数
    count = 0
    pos = 0
    while True:
        pos = content_lower.find(keyword_lower, pos)
        if pos == -1:
            break
        count += 1
        pos += len(keyword_lower)

    # 标题是否命中
    heading_hit = 1 if keyword_lower in heading_lower else 0

    # content 长度惩罚
    length_penalty = len(chunk["content"]) * 0.001

    return 2.0 * count + 3.0 * heading_hit - length_penalty


def search_static(chunks, keyword, filters=None, top_k=5):
    """静态版搜索（与 site/app.js 逻辑一致）。"""
    if not keyword or not keyword.strip():
        return []

    filters = filters or {}
    keyword_lower = keyword.lower()

    # 过滤
    candidates = chunks
    if filters.get("dir_type"):
        candidates = [c for c in candidates if c["dir_type"] == filters["dir_type"]]
    if filters.get("year"):
        candidates = [c for c in candidates if c["year"] == filters["year"]]

    # 关键词匹配
    matched = [
        c for c in candidates
        if keyword_lower in c["content"].lower() or keyword_lower in c["heading"].lower()
    ]

    # 打分
    scored = [
        {**c, "score": calculate_score_static(c, keyword)}
        for c in matched
    ]

    # 排序：score 降序，score 相同时 id 升序
    scored.sort(key=lambda x: (-x["score"], x["id"]))

    return scored[:top_k]


def load_chunks():
    """加载 chunks.json。"""
    chunks_path = Path(__file__).parent.parent / "site" / "data" / "chunks.json"
    with open(chunks_path, "r", encoding="utf-8") as f:
        return json.load(f)


def main():
    chunks = load_chunks()

    # 测试用例
    test_cases = [
        {"keyword": "重排", "filters": {}, "desc": "重排 / 无过滤 / top5"},
        {"keyword": "重排", "filters": {"dir_type": "11-赛题归档"}, "desc": "重排 / dir_type=11-赛题归档 / top5"},
        {"keyword": "缓存", "filters": {}, "desc": "缓存 / 无过滤 / top5"},
        {"keyword": "混合检索", "filters": {}, "desc": "混合检索 / 无过滤 / top5"},
        {"keyword": "ngram", "filters": {}, "desc": "ngram / 无过滤 / top5"},
        {"keyword": "策略", "filters": {}, "desc": "策略 / 无过滤 / top5"},
    ]

    database.init_db()
    conn = database.get_connection()

    total_overlap = 0
    total_cases = 0

    print("=" * 70)
    print("静态版 vs seekdb 版 Top-5 重合率检查")
    print("=" * 70)
    print()

    for case in test_cases:
        keyword = case["keyword"]
        filters = case["filters"]
        desc = case["desc"]

        # seekdb 版
        seekdb_results = seekdb_search(conn, keyword, filters=filters, top_k=5)
        seekdb_ids = set(r["chunk_id"] for r in seekdb_results)

        # 静态版
        static_results = search_static(chunks, keyword, filters=filters, top_k=5)
        static_ids = set(r["id"] for r in static_results)

        # 重合数
        overlap = len(seekdb_ids & static_ids)
        total_overlap += overlap
        total_cases += 1

        print(f"【{desc}】")
        print(f"  seekdb Top-5:  {sorted(seekdb_ids)}")
        print(f"  静态版 Top-5:  {sorted(static_ids)}")
        print(f"  重合数：{overlap}/5")
        print()

        # 如果重合率低于 3/5，并排显示详情
        if overlap < 3:
            print("  [WARNING] 重合率低于 3/5，详情对比：")
            print("  seekdb 版:")
            for r in seekdb_results:
                print(f"    - id={r['chunk_id']}, score={r['score']:.4f}, heading={r['heading'][:40]}")
            print("  静态版:")
            for r in static_results:
                print(f"    - id={r['id']}, score={r['score']:.4f}, heading={r['heading'][:40]}")
            print()

    conn.close()
    database.close_db()

    # 平均重合率
    avg_overlap = total_overlap / total_cases if total_cases > 0 else 0
    print("=" * 70)
    print(f"平均 Top-5 重合率：{avg_overlap:.1f}/5 ({avg_overlap/5*100:.1f}%)")
    print("=" * 70)


if __name__ == "__main__":
    main()
