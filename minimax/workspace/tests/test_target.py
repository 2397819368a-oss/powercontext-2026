"""目标功能测试：本次迭代要实现的行为。

**预期：在改动前的代码上全部失败，在改动后全部通过。**

失败原因必须是「行为未实现」（断言不成立），不能是导入错误、
缺少依赖或无法启动——否则不算有效的功能失败证据。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from expense.filters import filter_expenses
from expense.service import department_monthly_report


def test_filter_by_department(expenses, ids) -> None:
    """按部门筛选：只保留指定部门的报销单。"""

    rows = filter_expenses(expenses, department="Engineering")
    assert ids(rows) == ["E-001", "E-002", "E-004"]


def test_department_match_is_case_insensitive(expenses, ids) -> None:
    """部门名匹配不区分大小写（规格 EXP-001 §2.1）。"""

    rows = filter_expenses(expenses, department="engineering")
    assert ids(rows) == ["E-001", "E-002", "E-004"]


def test_filter_by_submitted_period(expenses, ids) -> None:
    """按提交日期闭区间筛选：两端都包含。"""

    rows = filter_expenses(
        expenses,
        submitted_from=date(2026, 2, 1),
        submitted_to=date(2026, 2, 28),
    )
    assert ids(rows) == ["E-004", "E-005"]


def test_combined_department_period_and_status(expenses, ids) -> None:
    """多个条件之间是 AND 关系（规格 EXP-001 §2.3）。"""

    rows = filter_expenses(
        expenses,
        department="Engineering",
        submitted_from=date(2026, 1, 1),
        submitted_to=date(2026, 1, 31),
        status="submitted",
    )
    assert ids(rows) == ["E-001"]



def test_department_monthly_report_scope(expenses) -> None:
    """业务用例：部门月度汇总只统计该部门该月的单据。

    部门条件未实现时，汇总会把其他部门的单据一并算入。
    """

    report = department_monthly_report(expenses, department="Engineering", year=2026, month=1)
    assert report.count == 2
    assert report.total_amount == Decimal("920.00")
