"""把检索结果组装成给模型看的上下文。

输出必须带**可核对的来源**（文件路径 + 符号 + 行号），这是验收项
「上下文正确」的硬要求；模型据此修改代码，评审据此复核。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import Settings
from .db import SeekdbStore

# 用于「必须保持不变的行为」这一节的检索词
INVARIANT_QUERY = "必须保持不变 回归 既有行为 不得"


@dataclass(slots=True)
class ContextBundle:
    """一次任务所需的全部上下文。"""

    task: str
    specs: list[dict[str, Any]] = field(default_factory=list)
    code: list[dict[str, Any]] = field(default_factory=list)
    tests: list[dict[str, Any]] = field(default_factory=list)
    invariants: list[dict[str, Any]] = field(default_factory=list)

    def sources(self) -> list[dict[str, Any]]:
        """所有被引用的来源（去重后按路径排序），供验证与记录。"""

        seen: dict[str, dict[str, Any]] = {}
        for group in (self.specs, self.code, self.tests, self.invariants):
            for hit in group:
                key = f"{hit['doc_path']}::{hit['symbol']}"
                seen.setdefault(
                    key,
                    {
                        "doc_path": hit["doc_path"],
                        "symbol": hit["symbol"],
                        "kind": hit["kind"],
                        "start_line": hit["start_line"],
                        "end_line": hit["end_line"],
                    },
                )
        return sorted(seen.values(), key=lambda item: (item["doc_path"], item["start_line"]))

    def render(self, *, max_chars: int) -> str:
        """渲染成 Markdown，超长时按组截断（规格优先保留）。"""

        parts: list[str] = [f"# 任务\n\n{self.task.strip()}\n"]

        def block(title: str, hits: list[dict[str, Any]]) -> None:
            if not hits:
                return
            parts.append(f"\n# {title}\n")
            for hit in hits:
                parts.append(
                    f"\n## `{hit['doc_path']}` → `{hit['symbol']}` （第 {hit['start_line']}-{hit['end_line']} 行）\n"
                    f"```\n{hit['body'].strip()}\n```\n"
                )

        block("相关规格", self.specs)
        block("必须保持不变的行为", self.invariants)
        block("相关代码", self.code)
        block("相关测试", self.tests)

        text = "".join(parts)
        if len(text) <= max_chars:
            return text
        return text[:max_chars].rstrip() + "\n\n...(上下文已按长度上限截断)\n"


def build_context(store: SeekdbStore, task: str, settings: Settings) -> ContextBundle:
    """按任务描述检索四类资料并组装。"""

    bundle = ContextBundle(task=task)
    bundle.specs = store.search(task, kind="spec", limit=settings.top_k_spec)
    bundle.code = store.search(task, kind="code", limit=settings.top_k_code)
    bundle.tests = store.search(task, kind="test", limit=settings.top_k_test)
    bundle.invariants = [
        hit
        for hit in store.search(INVARIANT_QUERY, kind="spec", limit=2)
        if hit["body"] not in {s["body"] for s in bundle.specs}  # 避免与规格重复
    ]
    return bundle
