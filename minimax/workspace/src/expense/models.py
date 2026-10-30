"""报销单领域模型。

业务语义见 ``workspace/specs/EXP-000-domain.md``。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

# 状态机：草稿 → 已提交 → 已审批 / 已驳回
STATUS_DRAFT = "draft"
STATUS_SUBMITTED = "submitted"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"

VALID_STATUSES = (STATUS_DRAFT, STATUS_SUBMITTED, STATUS_APPROVED, STATUS_REJECTED)


@dataclass(frozen=True, slots=True)
class Expense:
    """一张报销单。``amount`` 用 Decimal 避免浮点累计误差。"""

    expense_id: str
    employee: str
    department: str
    amount: Decimal
    submitted_at: date
    status: str = STATUS_SUBMITTED

    def __post_init__(self) -> None:
        if not self.expense_id:
            raise ValueError("expense_id 不能为空")
        if not self.department:
            raise ValueError("department 不能为空")
        if self.amount < 0:
            raise ValueError("amount 不能为负")
        if self.status not in VALID_STATUSES:
            raise ValueError(f"未知状态: {self.status}")
