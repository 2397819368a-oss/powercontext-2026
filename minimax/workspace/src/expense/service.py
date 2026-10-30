"""业务用例：部门月度报销汇总。

这个模块展示 ``filter_expenses`` 的调用方式——它已经在用新接口传
``department`` 和 ``submitted_from`` / ``submitted_to``，因此新参数
未实现时，汇总结果会把别的部门、别的月份的报销单一并算进来。
"""

from __future__ import annotations

import calendar
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .filters import filter_expenses
from .models import Expense


@dataclass(frozen=True, slots=True)
class DepartmentMonthlyReport:
    department: str
    year: int
    month: int
    total_amount: Decimal
    count: int


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    """返回某个月的闭区间首末日。"""

    if not 1 <= month <= 12:
        raise ValueError(f"month 必须在 1..12，收到 {month}")
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def department_monthly_report(
    expenses: Iterable[Expense],
    *,
    department: str,
    year: int,
    month: int,
    status: str | None = None,
) -> DepartmentMonthlyReport:
    """统计某部门某月的报销单。"""

    first, last = _month_bounds(year, month)
    rows = filter_expenses(
        expenses,
        department=department,
        submitted_from=first,
        submitted_to=last,
        status=status,
    )
    return DepartmentMonthlyReport(
        department=department,
        year=year,
        month=month,
        total_amount=sum((row.amount for row in rows), Decimal("0")),
        count=len(rows),
    )
