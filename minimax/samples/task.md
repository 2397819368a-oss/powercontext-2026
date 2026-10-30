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
