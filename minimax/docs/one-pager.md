# 一页技术说明

## 1. 问题

AI 改代码时最容易出错的不是写不出代码，而是**不知道约束在哪**：规格散在文档里、
测试散在测试目录里、上次做到哪了没人记得。这个模块要解决的就是「把该看的资料
按本次任务挑出来，连同来源一起交出去」。

## 2. 模块结构

```
任务描述
   │
   ├─[chunker]──────── 规格按 H2 切、代码按 ast 符号切 ──┐
   │                                                    ▼
   │                                            [seekdb] ngram 全文索引
   │                                                    │
   ├─[assembler]── 分别检索 spec/code/test ──────────────┘
   │                 + 单独检索「必须保持不变的行为」
   ▼
带来源的上下文（文件 + 符号 + 行号）
   │
   ├─[minimax_client]── 只输出目标函数的完整实现
   ├─[patcher]───────── ast 定位替换 + difflib 生成差异
   ├─[pytest]────────── 改前 5 failed → 改后 10 passed
   └─[state]─────────── PowerContext 按 revision 记录每阶段
```

## 3. 上下文是怎么选出来的

**不是把所有资料塞给模型**，而是四路分别检索后按类型配额取前若干条
（spec 4 / code 4 / test 4）：

1. 用任务描述整体做一次 ngram 全文检索，按 `kind` 过滤，取相关性前 k；
2. 「必须保持不变的行为」用固定检索词（`必须保持不变 回归 既有行为 不得`）
   单独查一次，并去掉与已选规格重复的块；
3. 拼装时每块都带 `doc_path → symbol （第 x-y 行）`，供模型引用、供评审复核；
4. 总长度超过上限时按「规格优先」截断。

**索引是整体替换**：每次运行先 `DELETE` 再插入，所以规格改了之后不可能还命中
旧内容——`samples/verify_context_refresh.py` 就是验证这一点（5/5 通过）。

## 4. 三项技术分工

| 技术 | 调用点 | 承担的作用 | 可核对的记录 |
|---|---|---|---|
| seekdb | `db.py::SeekdbStore.search` | 存 35 个切块；四路检索的素材全部来自它 | `records/db_queries.json` |
| MiniMax | `minimax_client.py::complete_sync` | 读上下文，产出 `filter_expenses` 的完整实现 | `records/minimax_runs.jsonl` + `patches/model_response.md` |
| PowerContext | `state.py::TaskStateStore.record` | 每阶段追加一个 revision；`--resume` 时读回并注入上下文 | `records/state.json` + `state_ops.json` |

三者不是并列的「都用了一下」，而是串在一条链上：**seekdb 出素材 → MiniMax 出
代码 → PowerContext 记住做到哪了**。

## 5. 三个关键取舍

**不让模型输出 diff。** 模型写的行号几乎总对不上，评审一跑就崩。改成「模型只
输出目标符号的完整实现，本地用 `ast` 定位行范围再替换」，差异由 `difflib` 生成，
所以 `change.diff` 一定是真实的。

**修改前的失败必须是断言失败。** 如果新参数根本不在签名里，改前测试会因为
`TypeError` 崩掉——那是环境错误，不是功能未实现。所以 `filter_expenses` 的签名
**已经包含**三个新参数，只是实现里忽略它们（留了 `TODO(EXP-001)`）。

**备份分两级。** `patches/original/` 只在第一次写，保存最初的原始文件，`reset`
依赖它；每次运行前另存一份带时间戳的中间态。只做单级覆盖会在第二次运行时把原始
版本冲掉——这个坑实际踩过。

## 6. 没做的事

不支持语义检索（关键词匹配，同义改写召回不到）；不支持一次改多个符号；
不支持跨仓库。这些不在本次任务范围内，列在这里是为了让边界清楚。
