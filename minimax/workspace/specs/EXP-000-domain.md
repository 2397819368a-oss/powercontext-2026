# EXP-000 报销单领域模型

- 版本：v3
- 状态：生效中

## 1. 实体

`Expense` 表示一张报销单，字段与含义：

| 字段 | 类型 | 说明 |
|---|---|---|
| `expense_id` | `str` | 全局唯一，格式 `E-###`，创建后不可变 |
| `employee` | `str` | 提交人姓名 |
| `department` | `str` | 所属部门，英文部门名，首字母大写（如 `Engineering`） |
| `amount` | `Decimal` | 报销金额，必须非负，保留两位小数 |
| `submitted_at` | `date` | 提交日期，不含时间与时区 |
| `status` | `str` | 见状态机 |

## 2. 状态机

```
draft ──submit──> submitted ──approve──> approved
                     │
                     └──reject──> rejected
```

- 只有 `submitted` 可以被审批或驳回
- `approved` / `rejected` 是终态，不可再流转

## 3. 约束

- `amount` 使用 `Decimal`，禁止用 `float` 参与金额运算
- `department` 不能为空字符串
- `submitted_at` 是纯日期，同一天的记录不区分先后

## 4. 非目标

本规格不涉及筛选、排序、聚合逻辑，那些在 `EXP-001` 与 `EXP-002` 中定义。
