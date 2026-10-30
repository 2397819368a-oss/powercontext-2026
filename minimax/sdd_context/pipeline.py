"""主流程：索引 → 检索 → 组装上下文 → MiniMax 改代码 → 跑测试 → 记录状态。

设计要点（对应验收项）：

- **上下文正确**：每次运行都重新索引再检索，索引是整体替换，所以规格改了
  之后不可能还返回旧答案；
- **MiniMax 实际参与**：模型输出被落成补丁文件，并与其记录一一对应；
- **数据库实际参与**：检索走 seekdb 全文索引，查询语句与命中都留档；
- **状态能复用**：每阶段写一个 revision，``--resume`` 时把上次进展注入上下文。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .assembler import build_context
from .chunker import collect
from .config import Settings
from .db import SeekdbStore
from .minimax_client import MiniMaxClient
from .patcher import apply_patch, extract_code
from .state import TaskStateStore

SYSTEM_PROMPT = """你是资深 Python 工程师，正在一个既有代码库上完成一次局部修改。

严格遵守：
1. 只输出**一个** Python 代码块，内容是该函数的完整实现（含 def 行与类型注解）。
2. 不要输出解释、不要输出 diff、不要输出多个候选方案。
3. 必须保持函数签名与现有调用方一致。
4. 上下文中「必须保持不变的行为」一条都不能破坏。
5. 只用标准库，不引入新依赖。"""

TARGET_FILE = Path("src/expense/filters.py")
TARGET_SYMBOL = "filter_expenses"


@dataclass(slots=True)
class RunReport:
    task_id: str
    task: str
    stages: list[str] = field(default_factory=list)
    context_chars: int = 0
    source_count: int = 0
    sources: list[dict[str, Any]] = field(default_factory=list)
    model: str = ""
    mock: bool = False
    # 状态后端：powercontext（真用了）还是 json-fallback（本机缺依赖时的等价降级）。
    # 如实写进报告，避免「声称用了 PowerContext」与实际情况不符。
    state_backend: str = ""
    patch_applied: bool = False
    patch_error: str | None = None
    state_backend_error: str = ""
    tests_before: dict[str, Any] = field(default_factory=dict)
    tests_after: dict[str, Any] = field(default_factory=dict)
    resumed_from: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task": self.task,
            "stages": self.stages,
            "context_chars": self.context_chars,
            "source_count": self.source_count,
            "sources": self.sources,
            "model": self.model,
            "mock": self.mock,
            "state_backend": self.state_backend,
            "state_backend_error": self.state_backend_error,
            "patch_applied": self.patch_applied,
            "patch_error": self.patch_error,
            "tests_before": self.tests_before,
            "tests_after": self.tests_after,
            "resumed_from": self.resumed_from,
        }


def task_id_of(task_text: str) -> str:
    return hashlib.sha1(task_text.strip().encode()).hexdigest()[:10]


def run_pytest(workspace: Path, *, timeout: int = 300) -> dict[str, Any]:
    """在 workspace 里跑一遍测试，返回结构化结果。"""

    env = dict(os.environ)
    env["PYTHONPATH"] = str(workspace / "src")
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "tests", "-q", "--tb=no", "-p", "no:cacheprovider"],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit_code": -1, "summary": "pytest 超时", "tail": ""}

    tail = "\n".join(
        line
        for line in completed.stdout.strip().splitlines()
        if line.startswith(("FAILED", "ERROR")) or " passed" in line or " failed" in line
    )
    return {
        "ok": completed.returncode == 0,
        "exit_code": completed.returncode,
        "summary": tail.splitlines()[-1] if tail else "",
        "tail": tail,
    }


async def run(
    *,
    task: str,
    settings: Settings,
    client: MiniMaxClient,
    resume: bool = False,
    fresh_db: bool = True,
    generation: int = 1,
) -> RunReport:
    report = RunReport(task_id=task_id_of(task), task=task)

    # 收尾必须走同一个出口 finish()：证据（调用记录 + 报告）**无论成功失败都要更新**。
    #
    # 2026-10-30 修的两个 bug：
    # ① 原来只有成功分支写 run_report.md / minimax_runs.jsonl，失败时会留下上一次
    #    成功的文件，CLI 再把旧文件打印出来 —— 失败看起来和成功一模一样；
    # ② 失败的那次调用压根没进 minimax_runs.jsonl，等于把「调过但失败了」这段历史抹掉。
    # 现在任何出口都会先 call finish()，报告永远是本次运行的真实结果。
    def finish(*, close_store: bool = True) -> None:
        store.dump_query_log(settings.records / "db_queries.json")
        state.dump_log(settings.records / "state_ops.json")
        client.dump_records(settings.records / "minimax_runs.jsonl")
        (settings.records / "run_report.json").write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (settings.records / "run_report.md").write_text(render_report(report), encoding="utf-8")
        if close_store:
            store.close()

    # ---------- 4. 调模型并落补丁 ----------
    workspace = settings.workspace
    settings.records.mkdir(parents=True, exist_ok=True)

    # ---------- 1. 索引（整体替换，规格一改就不可能命中旧内容）----------
    store = SeekdbStore(settings.db_dir)
    await store.open(fresh=fresh_db)
    rows = collect(workspace, generation=generation)
    store.replace_chunks(rows)
    report.stages.append(f"indexed:{len(rows)}")

    state = TaskStateStore(settings.records / "state.json")
    report.state_backend = state.backend_name
    report.state_backend_error = state.degraded_reason
    prior = None
    if resume:
        state.restore()
        prior = await state.latest(report.task_id)
        if prior is not None:
            report.resumed_from = f"{prior.get('_revision')}"

    # ---------- 2. 组装上下文 ----------
    bundle = build_context(store, task, settings)
    rendered = bundle.render(max_chars=settings.max_context_chars)
    if prior is not None:
        rendered = (
            "# 上一轮进展（来自持久化状态）\n\n"
            f"```json\n{json.dumps({k: v for k, v in prior.items() if not k.startswith('_')}, ensure_ascii=False, indent=2)}\n```\n\n"
            + rendered
        )
    report.context_chars = len(rendered)
    report.source_count = len(bundle.sources())
    report.sources = bundle.sources()

    context_path = settings.records / "context_latest.md"
    context_path.write_text(rendered, encoding="utf-8")

    await state.record(
        report.task_id,
        {
            "stage": "context_ready",
            "context_chars": report.context_chars,
            "sources": report.sources,
            "resumed_from": report.resumed_from,
        },
    )
    state.persist()
    report.stages.append("context_ready")
    # 先落一次盘（不关库）：后续任一环节崩掉，留下的报告只走到 context_ready，
    # 一眼能看出「这次没跑完」，而不是沿用上一次的成功报告。
    finish(close_store=False)

    # ---------- 3. 基线与改动前的测试结果 ----------
    report.tests_before = run_pytest(workspace)


    result = client.complete_sync(system=SYSTEM_PROMPT, user=rendered)
    report.model = result.model
    report.mock = result.mock

    patch_dir = settings.records / "patches"
    patch_dir.mkdir(parents=True, exist_ok=True)
    (patch_dir / "model_response.md").write_text(result.content or "(空响应)", encoding="utf-8")

    if not result.ok:
        report.patch_error = result.error or "模型返回空内容"
        report.stages.append("model_failed")
        await state.record(report.task_id, {"stage": "model_failed", "error": report.patch_error})
        state.persist()
        finish()
        return report

    code = extract_code(result.content)
    if code is None:
        report.patch_error = "响应里没有可用的 Python 代码块"
        report.stages.append("patch_failed")
        await state.record(report.task_id, {"stage": "patch_failed", "error": report.patch_error})
        state.persist()
        finish()
        return report

    patch = apply_patch(
        target_file=workspace / TARGET_FILE,
        symbol=TARGET_SYMBOL,
        new_source=code,
        backup_dir=patch_dir,
    )
    report.patch_applied = patch.applied
    report.patch_error = patch.error
    if patch.applied:
        (patch_dir / "change.diff").write_text(patch.diff, encoding="utf-8")
        client.attach_output(str((patch_dir / "change.diff").relative_to(settings.records.parent)))
    report.stages.append("patched" if patch.applied else "patch_failed")

    # ---------- 5. 改后测试 ----------
    report.tests_after = run_pytest(workspace) if patch.applied else {"ok": False, "summary": "未应用补丁"}

    await state.record(
        report.task_id,
        {
            "stage": "verified" if report.tests_after.get("ok") else "tests_failed",
            "patch_applied": report.patch_applied,
            "patch_error": report.patch_error,
            "tests_before": report.tests_before.get("summary"),
            "tests_after": report.tests_after.get("summary"),
        },
    )
    state.persist()
    report.stages.append("verified" if report.tests_after.get("ok") else "tests_failed")

    # ---------- 6. 记录落盘 ----------
    finish()
    return report


def verdict(report: RunReport) -> str:
    """一句话结论：成功 / 失败原因。

    放在报告最显眼的位置，避免「补丁未应用 + 测试没跑」被当成一次成功运行。
    """

    if report.patch_error:
        return f"失败（{report.patch_error}）"
    if not report.patch_applied:
        return "失败（补丁未应用）"
    if not report.tests_after.get("ok"):
        return f"失败（改动后测试未通过：{report.tests_after.get('summary', '未采集')}）"
    return "成功（补丁已应用且改动后测试全通过）"


def render_report(report: RunReport) -> str:
    """人类可读的一次运行报告。"""

    before = report.tests_before.get("summary", "（未采集）")
    after = report.tests_after.get("summary", "（未采集）")
    lines = [
        "# 运行报告",
        "",
        f"- 任务 ID：`{report.task_id}`",
        f"- 结论：**{verdict(report)}**",
        f"- 阶段：{' → '.join(report.stages)}",
        f"- 上下文：{report.context_chars} 字符，引用 {report.source_count} 处来源",
        f"- 模型：`{report.model}`" + ("（**mock**，非真实调用）" if report.mock else ""),
        f"- 状态后端：`{report.state_backend or '（未知）'}`"
        + (f"（本机缺少 powercontext，已降级：{report.state_backend_error}）" if report.state_backend_error else ""),
        f"- 补丁：{'已应用' if report.patch_applied else '未应用'}"
        + (f"（{report.patch_error}）" if report.patch_error else ""),
        "",
        "## 测试",
        "",
        "| | 结果 |",
        "|---|---|",
        f"| 改动前 | {before} |",
        f"| 改动后 | {after} |",
        "",
        "## 引用的来源",
        "",
    ]
    for source in report.sources:
        lines.append(
            f"- `{source['doc_path']}` → `{source['symbol']}`（第 {source['start_line']}-{source['end_line']} 行，{source['kind']}）"
        )
    return "\n".join(lines) + "\n"

