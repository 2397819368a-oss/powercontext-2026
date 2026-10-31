"""ngram 必要性对照：同一份数据，唯一变量是全文索引的分词器。

用法：
    python tools/ngram_compare.py [关键词]

输出示例：
    ngram 索引命中: 19
    默认分词器命中: 0

原理：先在现库（content 列带 WITH PARSER ngram 的全文索引）里统计命中数，
再建一张结构与内容都相同、但全文索引**不带 ngram** 的临时表，跑同一条 MATCH 查询。
两者之差即为 ngram 分词器的贡献。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app import database
from backend.app.database import escape_literal

DEFAULT_KEYWORDS = ("重排", "混合检索", "缓存", "ngram", "策略")


def count_in(cursor, table: str, keyword: str) -> int:
    sql = "SELECT COUNT(*) FROM %s WHERE MATCH(content) AGAINST(%s)" % (
        table,
        escape_literal(keyword),
    )
    row = database.fetchone(cursor, sql)
    return int(row[0])


def main() -> int:
    keywords = sys.argv[1:] or list(DEFAULT_KEYWORDS)
    head = keywords[0]

    database.init_db()
    conn = database.get_connection()
    try:
        n_ngram = count_in(conn, "note_chunks", head)

        database.execute(conn, "DROP TABLE IF EXISTS tmp_no_ngram")
        database.execute(
            conn,
            "CREATE TABLE tmp_no_ngram (id INT PRIMARY KEY, content TEXT, "
            "FULLTEXT KEY ft(content))",
        )
        database.execute(
            conn,
            "INSERT INTO tmp_no_ngram (id, content) SELECT id, content FROM note_chunks",
        )
        n_default = count_in(conn, "tmp_no_ngram", head)

        print("关键词 %r" % head)
        print("  ngram 索引命中 : %d" % n_ngram)
        print("  默认分词器命中 : %d" % n_default)

        if len(keywords) > 1:
            print("--- 其余关键词（ngram）---")
            for kw in keywords[1:]:
                print("  %s -> %d" % (kw, count_in(conn, "note_chunks", kw)))

        database.execute(conn, "DROP TABLE IF EXISTS tmp_no_ngram")
    finally:
        conn.close()
        database.close_db()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
