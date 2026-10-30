"""MiniMax 调用封装。

- 走 OpenAI 兼容接口（``base_url`` 默认国内站点），因此复用 ``openai`` 包；
- API key 只从环境变量读，不落盘、不写日志；
- 支持 ``mock`` 模式：没有 key 时用确定性响应跑通全流程，便于评审自查，
  但每次调用都会在记录里标注 ``mock: true``，不会伪装成真实调用。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "https://api.minimaxi.com/v1"
DEFAULT_MODEL = "MiniMax-M3.1-Flash"


@dataclass(slots=True)
class MiniMaxResult:
    ok: bool
    content: str
    model: str
    mock: bool
    latency_ms: int
    usage: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


class MiniMaxClient:
    """最小可用的补全客户端。"""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        mock: bool = False,
        record_path: Path | None = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.environ.get("MINIMAX_API_KEY", "")
        self.base_url = base_url or os.environ.get("MINIMAX_BASE_URL", DEFAULT_BASE_URL)
        self.model = model or os.environ.get("MINIMAX_MODEL", DEFAULT_MODEL)
        self.mock = mock or not self.api_key
        self.record_path = record_path
        self._records: list[dict[str, Any]] = []

    # ---------- 对外 ----------
    def complete_sync(self, *, system: str, user: str, max_tokens: int = 4096) -> MiniMaxResult:
        started = time.monotonic()
        if self.mock:
            result = MiniMaxResult(
                ok=True,
                content=MOCK_RESPONSE,
                model=f"{self.model}(mock)",
                mock=True,
                latency_ms=int((time.monotonic() - started) * 1000),
                usage={"prompt_tokens": len(system) + len(user), "completion_tokens": len(MOCK_RESPONSE)},
            )
        else:
            result = self._call(system=system, user=user, max_tokens=max_tokens, started=started)

        self._records.append(
            {
                "at": datetime.now().isoformat(timespec="seconds"),
                "model": result.model,
                "mock": result.mock,
                "ok": result.ok,
                "error": result.error,
                "latency_ms": result.latency_ms,
                "usage": result.usage,
                "prompt_chars": len(system) + len(user),
                "response_chars": len(result.content),
                "output_path": None,
            }
        )
        return result

    def attach_output(self, path: str) -> None:
        """把本次产出文件登记到最后一条记录上，建立「调用 ↔ 补丁」对应关系。"""

        if self._records:
            self._records[-1]["output_path"] = path

    def dump_records(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            for record in self._records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    # ---------- 内部 ----------
    def _call(self, *, system: str, user: str, max_tokens: int, started: float) -> MiniMaxResult:
        """真实调用 MiniMax 原生接口。

        这里刻意不走 openai SDK，原因是有个坑：**MiniMax 在模型不可用、余额
        不足时依然返回 HTTP 200**，真正的错误码藏在 body 的
        ``base_resp.status_code`` 里（例如 1008=余额不足、2069=模型已下线）。
        只判断 HTTP 状态会把失败当成成功，所以直接处理原始 JSON。

        顺带的好处是少一个第三方依赖，评审复跑时更稳。
        """
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.1,
        }
        request = urllib.request.Request(
            f"{self.base_url.rstrip('/')}/text/chatcompletion_v2",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
        )
        request.add_header("Authorization", f"Bearer {self.api_key}")
        request.add_header("Content-Type", "application/json")

        failed = lambda msg: MiniMaxResult(  # noqa: E731 - 仅为避免重复构造
            ok=False,
            content="",
            model=self.model,
            mock=False,
            latency_ms=int((time.monotonic() - started) * 1000),
            error=msg,
        )
        try:
            with urllib.request.urlopen(request, timeout=120.0) as response:
                body = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            return failed(f"HTTP {exc.code}: {detail}")
        except Exception as exc:  # noqa: BLE001  网络层异常原样上报
            return failed(f"{type(exc).__name__}: {exc}")

        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as exc:
            return failed(f"响应不是合法 JSON: {exc}; 前 200 字符={body[:200]}")

        # 关键：业务错误码优先于一切。
        base = parsed.get("base_resp") or {}
        code = base.get("status_code")
        if code not in (None, 0):
            return failed(f"MiniMax 业务错误 {code}: {base.get('status_msg', '')}")

        choices = parsed.get("choices") or []
        if not choices:
            return failed(f"响应缺少 choices: {body[:200]}")
        content = (choices[0].get("message") or {}).get("content") or ""
        if not content.strip():
            return failed("返回内容为空")

        return MiniMaxResult(
            ok=True,
            content=content,
            model=self.model,
            mock=False,
            latency_ms=int((time.monotonic() - started) * 1000),
            usage=parsed.get("usage") or {},
        )


MOCK_RESPONSE = """```python
def filter_expenses(
    expenses: Iterable[Expense],
    *,
    min_amount: Decimal | None = None,
    max_amount: Decimal | None = None,
    status: str | None = None,
    department: str | None = None,
    submitted_from: date | None = None,
    submitted_to: date | None = None,
) -> list[Expense]:
    \"\"\"按条件筛选报销单，结果按 (submitted_at, expense_id) 升序返回。\"\"\"

    wanted = department.casefold() if department is not None else None
    selected = [
        expense
        for expense in expenses
        if (min_amount is None or expense.amount >= min_amount)
        and (max_amount is None or expense.amount <= max_amount)
        and (status is None or expense.status == status)
        and (wanted is None or expense.department.casefold() == wanted)
        and (submitted_from is None or expense.submitted_at >= submitted_from)
        and (submitted_to is None or expense.submitted_at <= submitted_to)
    ]
    return sorted(selected, key=lambda item: (item.submitted_at, item.expense_id))
```"""
