# SDD 工程上下文模块（MiniMax 赛道）

给 AI 开发助手做一个「规格驱动的上下文模块」：拿到一句开发任务，从工程资料里
检索出**要遵守的规格、要改的代码、要跑的测试**，组装成上下文交给 MiniMax 完成
代码修改，并把任务进展存进 PowerContext，重启后能接着用。

对应三项技术的分工（题面 2.2 的要求）：

| 技术 | 在本项目里实际干什么 | 不能只是「装上了」的证据 |
|---|---|---|
| **MiniMax** | 读上下文，输出目标函数的完整实现 | `records/patches/model_response.md`、`records/minimax_runs.jsonl`、`records/patches/change.diff` 三者一一对应 |
| **seekdb** | 存规格/代码/测试切块，全文检索出上下文素材 | `records/db_queries.json` 里有每条 SQL 与实际命中 |
| **PowerContext** | 按 revision 记录任务进展，重启后读回并注入上下文 | `records/state.json` + `records/state_ops.json`；`--resume` 会把上次结果写进上下文开头 |

---

## 目录

```
minimax/
├── workspace/                  # 被修改的业务工程（自带测试）
│   ├── specs/                  #   规格：EXP-000/001/002 生效，EXP-003 是无关干扰项
│   ├── src/expense/            #   代码：models / filters / service
│   └── tests/                  #   test_target（改前失败）+ test_regression（改前后都过）
├── sdd_context/                # 上下文模块本体
│   ├── chunker.py              #   切块：规格按 H2，代码按 ast 符号
│   ├── db.py                   #   seekdb 封装（ngram 全文索引）
│   ├── assembler.py            #   检索 + 组装带来源的上下文
│   ├── minimax_client.py       #   MiniMax 调用（支持 mock）
│   ├── patcher.py              #   ast 定位替换 + difflib 生成差异
│   ├── state.py                #   PowerContext 状态读写
│   └── pipeline.py / cli.py    #   主流程与命令行
├── samples/                    # 验证样例与自检脚本
├── records/                    # 运行产物（每次运行覆盖）
└── docs/one-pager.md           # 一页技术说明
```

---

## 依赖

Python 3.11+（开发验证用 3.13.14）。版本见 `requirements.txt`。

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Linux 上 `pylibseekdb` 由 pip 自动选 manylinux wheel；Windows 需 1.4.0 以上
（才有 `cp312-abi3-win_amd64`）。

---

## 配置

只有一个必填项，从环境变量读：

```bash
export MINIMAX_API_KEY="你的 key"          # 不设则自动进入 mock 模式
export MINIMAX_MODEL="MiniMax-M3.1-Flash"  # 可选，默认 MiniMax-M3.1-Flash
export MINIMAX_BASE_URL="https://api.minimaxi.com/v1"   # 可选
```

> **没有 key 也能跑通全流程**：会自动使用 `minimax_client.MOCK_RESPONSE`，
> 并在 `records/minimax_runs.jsonl` 里标记 `"mock": true`。这样评审可以先验证
> 流程与测试，再换成真实调用——不会把 mock 结果伪装成模型产出。

---

## 快速开始（干净环境四步）

```bash
# 1. 建索引（规格/代码/测试 → seekdb）
python -m sdd_context.cli index

# 2. 只看上下文（不调用模型，便于核对检索质量）
python -m sdd_context.cli context --task samples/task.md

# 3. 完整流程：检索 → 调 MiniMax → 落补丁 → 跑测试
python -m sdd_context.cli run --task samples/task.md

# 4. 确认状态可复用（读回上次进展）
python -m sdd_context.cli show-state
python -m sdd_context.cli run --task samples/task.md --resume
```

回到改动前（还原代码、清掉状态）：

```bash
python -m sdd_context.cli reset
```

---

## 验证样例

验证样例就是 `samples/task.md` 这份任务描述，配套的初始数据是 `workspace/`
（改前状态：目标功能未实现的代码 + 完整测试）。

### 要改什么

`workspace/src/expense/filters.py::filter_expenses` 加上 `department` 与
`submitted_from` / `submitted_to` 三个筛选条件。

### 哪些原有行为必须保持

金额区间闭区间、`status` 精确匹配、结果按 `(submitted_at, expense_id)` 升序、
不就地修改入参、函数签名不变。这五条在 `workspace/specs/EXP-001-expense-filter.md`
的「§3 必须保持不变的行为」里，也是 `tests/test_regression.py` 的断言内容。

### 预期结果

| 阶段 | 命令 | 预期 |
|---|---|---|
| 改动前 | `cd workspace && PYTHONPATH=src pytest tests -q` | **5 failed, 5 passed**（失败全是 `AssertionError`，不是导入错误） |
| 改动后 | 同上 | **10 passed** |
| 无关规格不被引入 | `python -m sdd_context.cli context` | 上下文里不出现 `EXP-003-audit-export`（审计导出与本次任务无关） |
| 状态复用 | `--resume` | 上下文开头出现「上一轮进展」，字符数增加 |
| 规格刷新 | `python samples/verify_context_refresh.py` | **5/5 通过**（规格改了上下文就变，还原就变回去） |

### 重置方法

```bash
python -m sdd_context.cli reset
```

`records/patches/original/filters.py` 保存**最初**的原始文件，reset 从它还原；
每次运行前的中间态另有 `<时间戳>-filters.py` 备份，可逐轮回溯。

---

## 调用入口

CLI 是主要入口，`sdd_context.assembler.build_context()` 与
`sdd_context.state.TaskStateStore` 是可编程接口——如果评审想接自己的 Agent，
直接调用这两个即可，不必走命令行。

| 命令 | 作用 |
|---|---|
| `index` | 重建索引（整体替换，不做增量合并） |
| `context` | 打印组装后的上下文 + 引用来源统计 |
| `run` | 完整流程；`--resume` 注入上次进展 |
| `show-state` | 从磁盘读回状态并打印（验证持久化） |
| `reset` | 还原代码 + 删除状态 |

---

## 验证方案

代码结果按实际执行的测试判定，测试集在改动前后完全一致：`tests/` 下 10 个
用例没有一条被删除、跳过或改预期。`test_target.py` 的 5 条在改动前因**行为未
实现**而失败（断言不成立），不是缺依赖或起不来。

---

## 已知限制

1. **中文检索必须用 ngram 分词**。seekdb 默认分词器对中文返回 0 命中，建表时
   必须 `FULLTEXT KEY ... WITH PARSER ngram`。
2. **seekdb 的 Windows 构建不认非 ASCII 路径**（`seekdb_open failed: code=-1`）。
   库目录含中文时自动退到 `C:/Temp`；Linux 无此问题。
3. **`Cursor.execute` 不支持参数绑定**，只接受纯 SQL 字符串，所有值经
   `db.literal()` 转义。
4. **规格按二级标题切块**。按三级标题切会让两行的小节拼不过长节（实测 2.98
   vs 22.63），新增的短小节几乎进不了 top-k。
5. **一次只处理一个目标符号**（`TARGET_SYMBOL = filter_expenses`）。跨文件、
   多符号的改动不在本次范围。
6. **不做语义检索**，是关键词全文检索。同义改写（比如任务只说「科室」而规格写
   「部门」）可能召回不到。

---

## 最近一次真实调用的结果

`records/` 里留的就是一次**真实**运行（不是 mock）的完整证据：

| 项 | 结果 |
|---|---|
| 模型 | `MiniMax-M3.1-Flash`（`mock=false`） |
| 延迟 | 8813 ms |
| token | prompt 2288 / completion 1246（其中 reasoning 945）|
| 产出 | `records/patches/change.diff`，测试 5 failed → 10 passed |
| 自检 | **7 PASS / 0 SKIP / 0 FAIL** |

模型自发加了「`submitted_from > submitted_to` 时返回空列表」的判断，超出规格要求。
`workspace/` 已还原到改动前状态（5 failed / 5 passed），以便自己复现整条链路。

---

## 真实调用前的体检（2026-10-30 实测）

**别拿到 key 就直接跑流水线。** 先跑 `samples/probe_real.py`，它会逐个试 Candidate
模型并报告真实可用性。原因见下面两条实测教训：

1. **`MiniMax-M2` / `M2.1` / `M2.5` / `M2.7` 全部已下线。** 即使 `/v1/models`
   仍把它们列出来，调用也只会得到 `status_code=2069 no longer available`。
   **列出 ≠ 可用。** 本仓库默认模型已改为 `MiniMax-M3.1-Flash`。
2. **MiniMax 的业务错误藏在 HTTP 200 里面。** 余额不足、模型下线都返回
   **HTTP 200**，真正的错误码在响应体的 `base_resp.status_code`
   （`1008` = 余额不足，`2069` = 模型已下线）。只判断 HTTP 状态码会把失败
   当成成功，所以 `minimax_client._call` 直接解析原始 JSON，先看业务码。

### 用法

```bash
export MINIMAX_API_KEY=<你的密钥>
SDD_DB_DIR=C:/Temp/probe_db python samples/probe_real.py   # 先体检
python samples/self_check.py                               # 再自检
```

密钥建议放在本仓库根目录的 `.env.local`（已在 `.gitignore` 中），**不要提交、
不要贴进聊天窗口或第三方网页**。

### 自检结果的诚实口径

- 没有 `MINIMAX_API_KEY` 时走 mock，「MiniMax 实际参与」一项标 **SKIP**，不伪装成 PASS；
- 真实调用失败时报告顶部会写**结论：失败（原因）**，`records/minimax_runs.jsonl`
  里也会留下这次失败记录，**不会被忽略或覆盖成上一次的成功结果**。

---

## 已知限制（补充）

7. **Windows 下 seekdb 的日志文件不会随 `close()` 立即释放**，紧接着
   `rmtree` 会报 `WinError 32`。现在 `open(fresh=True)` 会重试若干次，失败则
   复用目录继续跑 —— 因为 `replace_chunks` 是「先清空再插入」，复用不会残留旧索引。
8. **PowerContext 是可选依赖**：装了用它做状态后端，没装自动降级为等价的纯
   Python 实现（见 `sdd_context/state.py`）。降级会如实显示在运行报告的
   「状态后端」一行。这样在缺少私有依赖的机器上项目依然能完整跑起来。
