"""自检：规格更新并重新导入后，上下文模块必须使用**更新后的内容**。

这是验收项「上下文正确」的核心：如果模块返回固定答案（写死字符串、
或者拿旧索引凑数），本脚本会失败。

流程：
1. 用当前规格建索引 → 取上下文 → 记录是否含标记语
2. 改写规格（追加一节带唯一标记语）→ **重新索引** → 再取上下文
3. 断言：改写前不含标记语、改写后必须含
4. 还原规格 → 再索引 → 断言标记语消失（证明索引是整体替换而非累积）

用法：
    python samples/verify_context_refresh.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sdd_context.assembler import build_context  # noqa: E402
from sdd_context.chunker import collect  # noqa: E402
from sdd_context.config import SAMPLES, Settings  # noqa: E402
from sdd_context.db import SeekdbStore  # noqa: E402

SPEC = "EXP-001-expense-filter.md"
MARKER = "空部门值视为不筛选"
# 插到「§3 必须保持不变的行为」之前，让它落在 §2 筛选条件这一节内部
ANCHOR = "## 3. 必须保持不变的行为"
PATCH = f"""### 2.4 空部门值

- `department` 传空字符串时，{MARKER}（与 None 等价）
- 该约定仅影响筛选行为，不影响其他条件

"""


async def snapshot(store: SeekdbStore, settings: Settings, task: str) -> str:
    store.replace_chunks(collect(settings.workspace, generation=1))
    bundle = build_context(store, task, settings)
    return bundle.render(max_chars=settings.max_context_chars)


async def main() -> int:
    settings = Settings.load()
    task = (SAMPLES / "task.md").read_text(encoding="utf-8")
    spec_path = settings.workspace / "specs" / SPEC
    original = spec_path.read_text(encoding="utf-8")

    checks: list[tuple[str, bool]] = []
    store = SeekdbStore(settings.db_dir)
    await store.open(fresh=True)

    try:
        # --- 1. 改前 ---
        before = await snapshot(store, settings, task)
        checks.append(("改前上下文不含标记语", MARKER not in before))

        # --- 2. 改规格并重新导入 ---
        patched = original.replace(ANCHOR, PATCH + ANCHOR, 1)
        assert patched != original, "锚点未找到，规格结构可能已变"
        spec_path.write_text(patched, encoding="utf-8")
        after = await snapshot(store, settings, task)
        checks.append(("规格更新后上下文含标记语", MARKER in after))
        checks.append(("上下文确实发生变化", before != after))

        # --- 3. 还原并重新导入 ---
        spec_path.write_text(original, encoding="utf-8")
        restored = await snapshot(store, settings, task)
        checks.append(("还原后标记语消失（索引整体替换）", MARKER not in restored))
        checks.append(("还原后内容回到初始状态", restored == before))
    finally:
        spec_path.write_text(original, encoding="utf-8")
        store.close()

    print("规格刷新自检")
    print("=" * 46)
    for name, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    failed = [name for name, ok in checks if not ok]
    print("=" * 46)
    print(f"{len(checks) - len(failed)}/{len(checks)} 通过")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
