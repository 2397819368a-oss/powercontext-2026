"""报销单筛选。

这个模块是本次迭代的改动目标：调用方已经按新接口传参，但
``department`` / ``submitted_from`` / ``submitted_to`` 三个筛选条件
尚未接入，当前实现会忽略它们，因此带这三个条件的查询会返回多余记录。

规格见 ``workspace/specs/EXP-001-expense-filter.md``。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from decimal import Decimal

from .models import Expense


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
    """按条件筛选报销单，结果按 (submitted_at, expense_id) 升序返回。

    金额区间为闭区间；``status`` 为精确匹配；部门为忽略大小写的整值匹配；
    提交日期区间为闭区间。
    """

    if (
        submitted_from is not None
        and submitted_to is not None
        and submitted_from > submitted_to
    ):
        return []

    normalized_department = (
        department.casefold() if department is not None else None
    )

    selected = [
        expense
        for expense in expenses
        if (min_amount is None or expense.amount >= min_amount)
        and (max_amount is None or expense.amount <= max_amount)
        and (status is None or expense.status == status)
        and (
            normalized_department is None
            or expense.department.casefold() == normalized_department
        )
        and (submitted_from is None or expense.submitted_at >= submitted_from)
        and (submitted_to is None or expense.submitted_at <= submitted_to)
    ]

    return sorted(selected, key=lambda item: (item.submitted_at, item.expense_id))
