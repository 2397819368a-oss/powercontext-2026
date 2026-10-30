"""回归测试：本次改动**不能**破坏的既有行为。

**预期：在改动前后都必须全部通过。** 任何一条变红，说明改动越界。
"""

from __future__ import annotations

from decimal import Decimal

from expense.filters import filter_expenses


def test_amount_range_is_closed_interval(expenses, ids) -> None:
    """金额区间是闭区间：等于边界值的记录要保留。"""

    rows = filter_expenses(expenses, min_amount=Decimal("120.00"), max_amount=Decimal("800.00"))
    assert ids(rows) == ["E-001", "E-003", "E-002"]


def test_status_filter_is_exact_match(expenses, ids) -> None:
    rows = filter_expenses(expenses, status="approved")
    assert ids(rows) == ["E-002", "E-005"]


def test_no_condition_returns_everything(expenses, ids) -> None:
    """不带任何条件时必须原样返回全部记录。"""

    rows = filter_expenses(expenses)
    assert ids(rows) == ["E-001", "E-003", "E-002", "E-004", "E-005"]


def test_result_is_sorted_by_submitted_at_then_id(expenses) -> None:
    """排序规则：(submitted_at, expense_id) 升序。"""

    rows = filter_expenses(expenses)
    keys = [(row.submitted_at, row.expense_id) for row in rows]
    assert keys == sorted(keys)


def test_input_sequence_is_not_mutated(expenses) -> None:
    """筛选不能就地修改调用方传入的序列。"""

    before = list(expenses)
    filter_expenses(expenses, department="Engineering")
    assert expenses == before

