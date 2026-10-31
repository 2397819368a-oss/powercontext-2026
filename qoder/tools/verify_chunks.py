"""验证 chunk 内容与原文件一致性（严格模式）。

随机抽取或全量扫描 chunk，按 line_start-line_end 从原文件截取内容，
与数据库中的 content 逐字比对（strip 后）。

检查项：
- 内容严格相等（去掉尾部空行后 strip 比对）
- line_start 行以 "## " 开头且 heading 匹配（heading 非空时）
- line_end 不超过文件总行数
- line_start > 0, line_end > 0, line_start <= line_end

退出码：
- 0: 全部通过
- 1: 存在 BAD 或异常
"""

import argparse
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app import database


def verify_chunk(chunk_id: int, content: str, line_start: int, line_end: int,
                 heading: str, file_path: str) -> tuple[bool, list[str]]:
    """验证单个 chunk，返回 (is_ok, errors)。"""
    errors = []
    
    # 检查行号基本合法性
    if line_start <= 0 or line_end <= 0:
        errors.append(f"行号 <= 0: {line_start}-{line_end}")
    if line_start > line_end:
        errors.append(f"行号倒置: start={line_start} > end={line_end}")
    
    # 读取原文件
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception as e:
        errors.append(f"无法读取文件: {e}")
        return False, errors
    
    total_lines = len(lines)
    
    # 检查行号越界
    if line_end > total_lines:
        errors.append(f"行号越界: end={line_end} > 文件总行数={total_lines}")
    
    # 检查 heading 行（line_start 行应以 "## " 开头且 heading 匹配）
    if heading and line_start >= 1 and line_start <= total_lines:
        start_line = lines[line_start - 1].rstrip("\n")
        if not start_line.startswith("## "):
            errors.append(f"起始行非 H2: {repr(start_line[:40])}")
        else:
            actual_heading = start_line.lstrip("#").strip()
            if actual_heading != heading:
                errors.append(f"Heading 不匹配: DB={repr(heading)}, 文件={repr(actual_heading)}")
    
    # 严格内容比对
    try:
        seg = [ln.rstrip("\n") for ln in lines[line_start - 1 : line_end]]
        # 去掉尾部空行
        while seg and seg[-1].strip() == "":
            seg.pop()
        expected = "\n".join(seg).strip()
        db_content = content.strip()
        
        if expected != db_content:
            errors.append("内容不匹配（严格比对）")
            # 找首个差异位置
            min_len = min(len(expected), len(db_content))
            diff_pos = -1
            for i in range(min_len):
                if expected[i] != db_content[i]:
                    diff_pos = i
                    break
            if diff_pos < 0 and len(expected) != len(db_content):
                diff_pos = min_len
            if diff_pos >= 0:
                ctx_start = max(0, diff_pos - 20)
                ctx_end = min(min_len, diff_pos + 20)
                errors.append(f"  首个差异位置 {diff_pos}:")
                errors.append(f"    DB:   ...{repr(db_content[ctx_start:ctx_end])}...")
                errors.append(f"    文件: ...{repr(expected[ctx_start:ctx_end])}...")
    except Exception as e:
        errors.append(f"比对出错: {e}")
    
    return len(errors) == 0, errors


def main():
    parser = argparse.ArgumentParser(description="验证 chunk 与原文件一致性（严格模式）")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--sample", type=int, default=0, help="随机抽样数量")
    group.add_argument("--all", action="store_true", help="全量扫描")
    args = parser.parse_args()
    
    if not args.all and args.sample <= 0:
        args.sample = 5
    
    database.init_db()
    conn = database.get_connection()
    
    try:
        rows = database.fetchall(conn, """
          SELECT nc.id, nc.content, nc.line_start, nc.line_end, nc.heading, n.file_path
          FROM note_chunks nc
          JOIN notes n ON n.id = nc.note_id
        """)
        
        if not rows:
            print("数据库为空，无 chunk 可验证")
            sys.exit(0)
        
        if args.all:
            selected = rows
            print(f"全量验证 {len(rows)} 个 chunk...\n")
        else:
            sample_size = min(args.sample, len(rows))
            selected = random.sample(rows, sample_size)
            print(f"从 {len(rows)} 个 chunk 中随机抽取 {sample_size} 个进行验证...\n")
        
        ok_count = 0
        bad_count = 0
        
        for row in selected:
            chunk_id, content, line_start, line_end, heading, file_path = row
            is_ok, errors = verify_chunk(chunk_id, content, line_start, line_end, heading, file_path)
            
            file_name = Path(file_path).name
            if is_ok:
                print(f"[OK]   chunk {chunk_id}: {file_name} 行 {line_start}-{line_end}")
                ok_count += 1
            else:
                print(f"[BAD]  chunk {chunk_id}: {file_name} 行 {line_start}-{line_end}")
                for err in errors:
                    print(f"       {err}")
                bad_count += 1
        
        print(f"\n{'='*60}")
        print(f"验证结果: {ok_count} OK / {bad_count} BAD")
        
        sys.exit(1 if bad_count > 0 else 0)
    
    finally:
        conn.close()
        database.close_db()


if __name__ == "__main__":
    main()
