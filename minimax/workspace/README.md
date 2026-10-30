# expense —— 报销单筛选与部门月度报表

一个可运行的 Python 业务工程，用来承载本次迭代任务。规格在 `specs/`，
代码在 `src/expense/`，测试在 `tests/`。

## 当前状态

`filter_expenses` 的函数签名已按新接口扩展（多了 `department` /
`submitted_from` / `submitted_to`），调用方 `department_monthly_report` 也已经
在按新签名传参，**但筛选实现还没接上**，导致部门报表把别的部门、别的月份的
报销单一并算进来。

## 运行测试

```bash
cd workspace
PYTHONPATH=src python -m pytest tests -q
```

当前预期：**5 failed, 5 passed**

- `tests/test_target.py` 的 5 条失败，全部是 `AssertionError`（行为未实现），
  不是导入错误或起不来
- `tests/test_regression.py` 的 5 条通过，是本次改动**不能破坏**的既有行为

## 规格

| 文件 | 内容 |
|---|---|
| `specs/EXP-000-domain.md` | 领域模型与状态机 |
| `specs/EXP-001-expense-filter.md` | **筛选规格（本次任务的依据）** |
| `specs/EXP-002-report.md` | 部门月度报表口径 |
| `specs/EXP-003-audit-export.md` | 审计导出（与本次任务无关，留作检索干扰项） |

## 目录

```
src/expense/
├── models.py     Expense 实体与状态常量
├── filters.py    filter_expenses  ← 本次改动目标
└── service.py    department_monthly_report（调用方）
```
