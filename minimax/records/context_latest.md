# 任务

# 任务：为报销单筛选补上部门与提交日期条件

## 背景

`expense.filters.filter_expenses` 的函数签名已经按新接口扩展，调用方
（部门月度报表 `department_monthly_report`）也已经在传 `department` 和
`submitted_from` / `submitted_to`，但筛选实现还没接上，导致报表把别的部门、
别的月份的报销单一并算进来。

## 要改什么

让 `filter_expenses` 真正按部门、按提交日期区间筛选：

- 部门：整值匹配，忽略大小写
- 提交日期：闭区间，两端都包含
- 多个条件之间是 AND

## 不能破坏什么

- 金额区间仍是闭区间
- `status` 仍是精确匹配
- 结果仍按 `(submitted_at, expense_id)` 升序
- 不得就地修改调用方传入的序列
- 不得改变函数签名

# 相关规格

## `README.md` → `当前状态` （第 6-11 行）
```
## 当前状态

`filter_expenses` 的函数签名已按新接口扩展（多了 `department` /
`submitted_from` / `submitted_to`），调用方 `department_monthly_report` 也已经
在按新签名传参，**但筛选实现还没接上**，导致部门报表把别的部门、别的月份的
报销单一并算进来。
```

## `specs/EXP-001-expense-filter.md` → `3. 必须保持不变的行为` （第 34-42 行）
```
## 3. 必须保持不变的行为

以下行为在本次改动中**一处都不能改**：

1. 金额区间仍是闭区间（`min_amount <= amount <= max_amount`）
2. `status` 仍是精确匹配
3. 结果仍按 `(submitted_at, expense_id)` 双键升序
4. 不得就地修改调用方传入的序列
5. 不得改变函数签名（调用方已按现签名传参）
```

## `specs/EXP-001-expense-filter.md` → `2. 筛选条件` （第 12-32 行）
```
## 2. 筛选条件

### 2.1 部门（`department`）

- 类型：`str | None`
- `None` 表示不按部门筛选
- 非 `None` 时，只保留 `expense.department` 与参数在**忽略大小写**后相等的记录
- 只做整值比较：不允许子串匹配、前缀匹配或模糊匹配

### 2.2 提交日期区间（`submitted_from` / `submitted_to`）

- 类型：`date | None`，两个参数相互独立
- 区间是**闭区间**：`submitted_from <= expense.submitted_at <= submitted_to`
- 只给 `submitted_from`：只约束下界
- 只给 `submitted_to`：只约束上界
- 两端都给且 `submitted_from > submitted_to` 时返回空列表，不抛异常

### 2.3 条件组合

- 所有筛选条件之间是 **AND** 关系
- 任一条件为 `None` 表示该条件不参与筛选
```

## `specs/EXP-002-report.md` → `1. 定义` （第 7-9 行）
```
## 1. 定义

部门月度报表统计**某部门在某个自然月内**提交的报销单，输出单据数与金额合计。
```

# 必须保持不变的行为

## `README.md` → `运行测试` （第 13-24 行）
```
## 运行测试

```bash
cd workspace
PYTHONPATH=src python -m pytest tests -q
```

当前预期：**5 failed, 5 passed**

- `tests/test_target.py` 的 5 条失败，全部是 `AssertionError`（行为未实现），
  不是导入错误或起不来
- `tests/test_regression.py` 的 5 条通过，是本次改动**不能破坏**的既有行为
```

# 相关代码

## `src/expense/filters.py` → `filter_expenses` （第 19-46 行）
```
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

    金额区间为闭区间；``status`` 为精确匹配。三个新参数
    （``department`` / ``submitted_from`` / ``submitted_to``）已在签名中
    就位以便调用方升级，但筛选逻辑尚未接入。
    """

    selected = [
        expense
        for expense in expenses
        if (min_amount is None or expense.amount >= min_amount)
        and (max_amount is None or expense.amount <= max_amount)
        and (status is None or expense.status == status)
    ]

    # TODO(EXP-001): department / submitted_from / submitted_to 尚未参与筛选。
    # 调用方传入时会原样放过，需要按规格补上。
    return sorted(selected, key=lambda item: (item.submitted_at, item.expense_id))
```

## `src/expense/service.py` → `department_monthly_report` （第 37-61 行）
```
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
```

## `src/expense/models.py` → `Expense` （第 21-40 行）
```
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
```

## `src/expense/service.py` → `DepartmentMonthlyReport` （第 20-26 行）
```
@dataclass(frozen=True, slots=True)
class DepartmentMonthlyReport:
    department: str
    year: int
    month: int
    total_amount: Decimal
    count: int
```

# 相关测试

## `tests/test_target.py` → `test_filter_by_submitted_period` （第 32-40 行）
```
def test_filter_by_submitted_period(expenses, ids) -> None:
    """按提交日期闭区间筛选：两端都包含。"""

    rows = filter_expenses(
        expenses,
        submitted_from=date(2026, 2, 1),
        submitted_to=date(2026, 2, 28),
    )
    assert ids(rows) == ["E-004", "E-005"]
```

## `tests/test_regression.py` → `test_input_sequence_is_not_mutated` （第 40-45 行）
```
def test_input_sequence_is_not_mutated(expenses) -> None:
    """筛选不能就地修改调用方传入的序列。"""

    before = list(expenses)
    filter_expenses(expenses, department="Engineering")
    assert expenses == before
```

## `tests/test_target.py` → `test_combined_department_period_and_status` （第 43-53 行）
```
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
```

## `tests/test_target.py` → `test_department_monthly_report_scope` （第 57-65 行）
```
def test_department_monthly_report_scope(expenses) -> None:
    """业务用例：部门月度汇总只统计该部门该月的单据。

    部门条件未实现时，汇总会把其他部门的单据一并算入。
    """

    report = department_monthly_report(expenses, department="Engineering", year=2026, month=1)
    assert report.count == 2
    assert report.total_amount == Decimal("920.00")
```
