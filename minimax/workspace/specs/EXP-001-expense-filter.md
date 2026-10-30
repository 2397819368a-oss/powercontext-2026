# EXP-001 报销单筛选规格

- 版本：v1
- 状态：生效中
- 适用模块：`expense.filters.filter_expenses`

## 1. 适用范围

`filter_expenses` 是报销单查询的唯一入口，供部门报表、审批队列、
审计导出等场景复用。任何调用方都不得自行实现筛选逻辑。

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

## 3. 必须保持不变的行为

以下行为在本次改动中**一处都不能改**：

1. 金额区间仍是闭区间（`min_amount <= amount <= max_amount`）
2. `status` 仍是精确匹配
3. 结果仍按 `(submitted_at, expense_id)` 双键升序
4. 不得就地修改调用方传入的序列
5. 不得改变函数签名（调用方已按现签名传参）

## 4. 验收标准

- `tests/test_target.py` 全部通过
- `tests/test_regression.py` 全部通过，且一条都不许被删除或跳过
