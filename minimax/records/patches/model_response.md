```python
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
```