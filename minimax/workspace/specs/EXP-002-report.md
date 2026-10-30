# EXP-002 部门月度报表规格

- 版本：v2
- 状态：生效中
- 适用模块：`expense.service.department_monthly_report`

## 1. 定义

部门月度报表统计**某部门在某个自然月内**提交的报销单，输出单据数与金额合计。

## 2. 统计口径

- 「某月」指自然月闭区间：从当月 1 日到当月最后一日
- 只统计 `submitted` 与 `approved` 两种状态
- `draft` 与 `rejected` 不计入
- 金额合计保留两位小数

## 3. 实现约束

- 必须复用 `filter_expenses` 完成筛选，不得另写一套筛选
- 部门维度与时间窗口同时生效
- 月份非法（不在 1..12）时抛 `ValueError`

## 4. 输出

`DepartmentMonthlyReport`：部门名、年、月、金额合计、单据数。
