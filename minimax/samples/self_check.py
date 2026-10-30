"""一键自检：把题面的 6 项必做逐条跑出来。

会执行一次完整流程（索引 → 检索 → 调模型 → 落补丁 → 跑测试），因此**会修改**
`workspace/src/expense/filters.py`。跑完用 `python -m sdd_context.cli reset` 还原。

    python samples/self_check.py

没有 MINIMAX_API_KEY 时会走 mock 分支，此时「MiniMax 实际参与」一项会标
SKIP 而不是 PASS——不把 mock 当真实调用。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sdd_context.assembler import build_context  # noqa: E402
from sdd_context.chunker import collect  # noqa: E402
from sdd_context.config import SAMPLES, Settings  # noqa: E402
from sdd_context.db import SeekdbStore  # noqa: E402
from sdd_context.minimax_client import MiniMaxClient  # noqa: E402
from sdd_context.pipeline import run  # noqa: E402
from sdd_context.state import TaskStateStore  # noqa: E402

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
UNRELATED_SPEC = "EXP-003"


async def main() -> int:
    settings = Settings.load()
    task = (SAMPLES / "task.md").read_text(encoding="utf-8")
    results: list[tuple[str, str, str]] = []

    def add(item: str, status: str, note: str = "") -> None:
        results.append((item, status, note))

    # ---------- 1. 能够运行 ----------
    store = SeekdbStore(settings.db_dir)
    try:
        await store.open(fresh=True)
        rows = collect(settings.workspace, generation=1)
        store.replace_chunks(rows)
        add("1. 能够运行", PASS if len(rows) > 0 else FAIL, f"索引 {len(rows)} 块")
    except Exception as exc:  # noqa: BLE001
        add("1. 能够运行", FAIL, f"{type(exc).__name__}: {exc}")
        return _report(results)

    # ---------- 3a. 上下文正确（来源 + 不引入无关规格）----------
    bundle = build_context(store, task, settings)
    rendered = bundle.render(max_chars=settings.max_context_chars)
    has_sources = len(bundle.sources()) > 0 and all(
        s["doc_path"] and s["symbol"] and s["start_line"] > 0 for s in bundle.sources()
    )
    no_unrelated = UNRELATED_SPEC not in rendered
    add(
        "3. 上下文正确（来源）",
        PASS if has_sources else FAIL,
        f"{len(bundle.sources())} 处来源，全部带路径/符号/行号",
    )
    add("3. 上下文正确（不引入无关资料）", PASS if no_unrelated else FAIL, f"未出现 {UNRELATED_SPEC}")

    # ---------- 4. 数据库实际参与 ----------
    store.dump_query_log(settings.records / "db_queries.json")
    log = json.loads((settings.records / "db_queries.json").read_text(encoding="utf-8"))
    db_ok = bool(log) and all(entry["sql"] and "ctx_chunks" in entry["sql"] for entry in log)
    hit_total = sum(len(entry["hits"]) for entry in log)
    store.close()
    add("4. 数据库实际参与", PASS if db_ok else FAIL, f"{len(log)} 条查询，共命中 {hit_total} 次")

    # ---------- 5/6. 跑完整流程 ----------
    client = MiniMaxClient(record_path=settings.records / "minimax_runs.jsonl")
    report = await run(task=task, settings=settings, client=client, resume=False)

    before_failed = not report.tests_before.get("ok", True)
    after_passed = report.tests_after.get("ok", False)
    add(
        "6. 代码修改有效",
        PASS if (before_failed and after_passed) else FAIL,
        f"改前「{report.tests_before.get('summary')}」→ 改后「{report.tests_after.get('summary')}」",
    )

    # ---------- 2. MiniMax 实际参与 ----------
    run_log = settings.records / "minimax_runs.jsonl"
    records = []
    if run_log.exists():
        records = [json.loads(line) for line in run_log.read_text(encoding="utf-8").splitlines() if line.strip()]
    last = records[-1] if records else {}
    linked = bool(last.get("output_path")) and (settings.records.parent / last["output_path"]).exists()
    if not records:
        add("2. MiniMax 实际参与", FAIL, "没有调用记录")
    elif last.get("mock"):
        add("2. MiniMax 实际参与", SKIP, "本次为 mock 调用（未配置 MINIMAX_API_KEY），不计为真实参与")
    else:
        add(
            "2. MiniMax 实际参与",
            PASS if (last.get("ok") and linked) else FAIL,
            f"模型 {last.get('model')}，{last.get('latency_ms')} ms，产出与 change.diff 对应",
        )

    # ---------- 5. 状态能够复用 ----------
    state_file = settings.records / "state.json"
    restored_ok = False
    note = "没有 state.json"
    if state_file.exists():
        fresh = TaskStateStore(state_file)
        count = fresh.restore()
        latest = await fresh.latest(report.task_id)
        restored_ok = count > 0 and latest is not None and latest.get("stage") in {
            "verified",
            "tests_failed",
        }
        note = f"重启后读回 {count} 个任务，最新 stage = {latest.get('stage') if latest else None}"
    add("5. 状态能够复用", PASS if restored_ok else FAIL, note)

    return _report(results)


def _report(results: list[tuple[str, str, str]]) -> int:
    print("\n6 项必做自检")
    print("=" * 72)
    for item, status, note in results:
        print(f"  [{status}] {item}")
        if note:
            print(f"         {note}")
    print("=" * 72)
    failed = [r for r in results if r[1] == FAIL]
    skipped = [r for r in results if r[1] == SKIP]
    print(f"通过 {len(results) - len(failed) - len(skipped)} / 跳过 {len(skipped)} / 失败 {len(failed)}")
    if skipped:
        print("提示：跳过项需配置 MINIMAX_API_KEY 后重跑本脚本才能计入。")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
