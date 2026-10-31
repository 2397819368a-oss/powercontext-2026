"""测试检索引擎：关键词命中、过滤生效、top_k、ngram 必要性对照。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.app.database import (
    get_connection,
    execute,
    fetchall,
    escape_literal,
    close_db,
    init_db,
)
from backend.app.search.engine import search


@pytest.fixture(scope="module")
def conn():
    """获取数据库连接，模块级复用。"""
    init_db()
    c = get_connection()
    yield c
    c.close()
    close_db()


# ── (a) 关键词能命中 ──────────────────────────────────────────

class TestKeywordHits:
    """验证中文关键词在 ngram 索引上能命中。"""

    def test_重排_hits(self, conn):
        results = search(conn, "重排")
        assert len(results) > 0, "搜索'重排'应有命中"

    def test_缓存_hits(self, conn):
        results = search(conn, "缓存")
        assert len(results) > 0, "搜索'缓存'应有命中"

    def test_混合检索_hits(self, conn):
        results = search(conn, "混合检索")
        assert len(results) > 0, "搜索'混合检索'应有命中"

    def test_score_positive(self, conn):
        results = search(conn, "重排", top_k=1)
        assert len(results) > 0
        assert results[0]["score"] > 0, "score 应为正数"


# ── (b) 过滤生效 ──────────────────────────────────────────────

class TestFilters:
    """验证结构化过滤条件真正缩小结果集。"""

    def test_dir_type_filter(self, conn):
        target_dir = "12-原子卡片"
        results = search(conn, "缓存", filters={"dir_type": target_dir})
        assert len(results) > 0, "过滤后应有结果"
        for r in results:
            assert r["dir_type"] == target_dir, (
                f"dir_type 过滤不生效：期望 {target_dir}，实际 {r['dir_type']}"
            )

    def test_year_filter(self, conn):
        results = search(conn, "缓存", filters={"year": 2026})
        assert len(results) > 0
        for r in results:
            assert r["year"] == 2026, f"year 过滤不生效：实际 {r['year']}"

    def test_year_range_filter(self, conn):
        results = search(conn, "缓存", filters={"year_from": 2025, "year_to": 2026})
        for r in results:
            assert 2025 <= r["year"] <= 2026

    def test_tags_filter(self, conn):
        results = search(conn, "OceanBase", filters={"tags": "seekdb"})
        for r in results:
            assert "seekdb" in (r["tags"] or ""), f"tags 过滤不生效：{r['tags']}"

    def test_filter_reduces_count(self, conn):
        all_results = search(conn, "缓存", top_k=100)
        filtered = search(conn, "缓存", filters={"dir_type": "12-原子卡片"}, top_k=100)
        assert len(filtered) <= len(all_results), "过滤应缩小或保持结果集"
        assert len(filtered) < len(all_results), "过滤应真正缩小结果集"


# ── (c) top_k 生效 ────────────────────────────────────────────

class TestTopK:
    """验证 top_k 限制结果数量。"""

    def test_top_k_1(self, conn):
        results = search(conn, "缓存", top_k=1)
        assert len(results) <= 1

    def test_top_k_5(self, conn):
        results = search(conn, "缓存", top_k=5)
        assert len(results) <= 5

    def test_top_k_descending_score(self, conn):
        results = search(conn, "缓存", top_k=10)
        scores = [r["score"] for r in results]
        assert scores == sorted(scores, reverse=True), "结果应按 score 降序"

    def test_empty_keyword(self, conn):
        results = search(conn, "")
        assert results == []


# ── (d) ngram 必要性对照 ──────────────────────────────────────

class TestNgramNecessity:
    """证明 ngram 解析器对中文检索是必要的。

    建一张临时表，使用默认分词器（不带 WITH PARSER ngram），
    灌入同样的数据，断言中文关键词在该表上命中 0 条，
    而在 ngram 表上命中 >= 3 条。
    """

    TEMP_TABLE = "test_no_ngram"

    @pytest.fixture(autouse=True)
    def setup_temp_table(self, conn):
        """创建临时表并灌入数据，测试结束后清理。"""
        execute(conn, f"DROP TABLE IF EXISTS {self.TEMP_TABLE}")
        execute(conn, f"""
          CREATE TABLE {self.TEMP_TABLE} (
            id BIGINT AUTO_INCREMENT,
            content LONGTEXT,
            PRIMARY KEY (id),
            FULLTEXT KEY ft_content (content)
          )
        """)

        rows = fetchall(conn, "SELECT id, content FROM note_chunks LIMIT 100")
        for row in rows:
            cid, content = row[0], row[1]
            execute(
                conn,
                f"INSERT INTO {self.TEMP_TABLE} (id, content) "
                f"VALUES ({cid}, {escape_literal(content)})"
            )
        conn.commit()

        yield

        execute(conn, f"DROP TABLE IF EXISTS {self.TEMP_TABLE}")
        conn.commit()

    def test_chinese_zero_hits_without_ngram(self, conn):
        """默认分词器下，纯中文关键词应命中 0 条。"""
        rows = fetchall(conn, f"""
          SELECT COUNT(*) FROM {self.TEMP_TABLE}
          WHERE MATCH(content) AGAINST('重排')
        """)
        count = rows[0][0]
        assert count == 0, (
            f"非 ngram 表搜索'重排'应命中 0 条，实际 {count} 条"
        )

    def test_chinese_hits_with_ngram(self, conn):
        """ngram 表下，同样的中文关键词应命中 >= 3 条。"""
        rows = fetchall(conn, """
          SELECT COUNT(*) FROM note_chunks
          WHERE MATCH(content) AGAINST('重排')
        """)
        count = rows[0][0]
        assert count >= 3, (
            f"ngram 表搜索'重排'应命中 >= 3 条，实际 {count} 条"
        )

    def test_ngram_vs_non_ngram_gap(self, conn):
        """对照：同一关键词，ngram 表命中数 >> 非 ngram 表。"""
        keyword = "缓存"

        non_ngram = fetchall(conn, f"""
          SELECT COUNT(*) FROM {self.TEMP_TABLE}
          WHERE MATCH(content) AGAINST('{keyword}')
        """)[0][0]

        ngram = fetchall(conn, f"""
          SELECT COUNT(*) FROM note_chunks
          WHERE MATCH(content) AGAINST('{keyword}')
        """)[0][0]

        assert non_ngram == 0, f"非 ngram 表应 0 命中，实际 {non_ngram}"
        assert ngram >= 3, f"ngram 表应 >= 3 命中，实际 {ngram}"
        assert ngram > non_ngram, "ngram 表命中数应严格大于非 ngram 表"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
