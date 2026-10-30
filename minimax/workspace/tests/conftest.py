"""pytest 共享夹具。

测试数据刻意覆盖：两个部门 × 两个月份 × 三种状态，便于分别验证
「部门」「时间窗口」「组合条件」三类筛选。
"""

from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

# 让 `import expense` 可用（workspace/src）
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from expense.models import Expense  # noqa: E402


@pytest.fixture
def expenses() -> list[Expense]:
    return [
        Expense("E-001", "张三", "Engineering", Decimal("120.00"), date(2026, 1, 5), "submitted"),
        Expense("E-002", "李四", "Engineering", Decimal("800.00"), date(2026, 1, 20), "approved"),
        Expense("E-003", "王五", "Marketing", Decimal("300.00"), date(2026, 1, 10), "submitted"),
        Expense("E-004", "赵六", "Engineering", Decimal("50.00"), date(2026, 2, 3), "submitted"),
        Expense("E-005", "钱七", "Finance", Decimal("2000.00"), date(2026, 2, 15), "approved"),
    ]


@pytest.fixture
def ids() -> callable:
    """把筛选结果压成 id 列表，方便断言。"""

    def _ids(rows: list[Expense]) -> list[str]:
        return [row.expense_id for row in rows]

    return _ids
