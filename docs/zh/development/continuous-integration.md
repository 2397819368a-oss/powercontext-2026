---
title: 持续集成
description: PR 如何按改动路径选择检查，以及如何执行全量验证。
---

# 持续集成

PR 通过 `.github/ci-paths.toml` 选择耗时检查。Main、E2E harness、Native personal service、
ZCode acceptance 和 Kimi Code acceptance 共用 `ci-changes.yml` 工作流。
每个范围判断任务都会在 Actions 摘要中输出 **CI scope** 表，列出各组检查是否运行。

Main 的 quality 任务和许可证检查对所有 PR 执行。quality 包含格式、依赖锁、生成契约、类型检查，
以及 `make check` 和 `make contract-test` 调用的专项测试。

## 改动与检查的对应关系

| 改动路径 | 额外执行的检查 |
| --- | --- |
| `docs/`、`website/`、根目录 README、文档资源 | 网站 lint、测试、构建，以及 Windows 链接验证。 |
| 单个宿主集成，例如 `integrations/pi/` | Python 单元和 E2E 测试矩阵、Windows 可移植性检查，以及该宿主的包或验收检查。 |
| `evaluation/` | 评测项目单元测试；ZCode guidance 还会触发 ZCode 验收。 |
| 持久化和数据库回归测试 | Python 检查，以及 SeekDB、OceanBase 验证任务。 |
| `e2e/bub/` 或 `integrations/bub/` | Harness 验证及 SQLite、OceanBase 验收；集成目录还会触发 Python 检查。 |
| 原生服务回归测试 | Python 检查，以及 Linux、macOS、Windows 的真实服务生命周期测试。 |
| 公共 guidance 模板或辅助代码 | Python 检查，以及 shared 规则中列出的宿主检查。 |
| 运行时代码、OpenAPI、公共依赖、CI 规则、公共 fixture 或未分类路径 | 范围选择器管理的全部检查。 |

跨多个范围的改动取检查集合的并集。插件中的 `SKILL.md` 是宿主输入，会触发宿主测试。
Desktop、Skill guidance validation 和 SeekDB 锁诊断继续使用各自的路径过滤。

推送到 `main`、`master` 时，范围选择器管理的检查全部执行。手动和定时执行也会选中所有检查组，
同时保留工作流已有的 suite、installer 等显式参数。需要在指定分支执行全量验证时，
可以对 Main 或 Native personal service 使用 **Run workflow**。PR 新增提交会取消其旧的 harness 执行。

## 维护路径归属

模式遵循 Python `fnmatch` 规则，`*` 可以匹配路径分隔符。公共输入放在 `full`，各组专用输入放在
`suites`，明确豁免的仓库说明文件放在 `ignore`，宿主公共 guidance 输入放在 `shared`。
未分类路径会选择全部检查。登记新的公共输入时，需要覆盖其所有消费者。

范围判断使用 PR head 与合并基点之间的完整 Git diff，以 NUL 分隔文件名，覆盖 PR 的所有提交、
删除文件和重命名前后的路径，不受 GitHub 路径过滤的文件数量限制。无法获取 diff 时执行全量检查。
若范围判断任务本身失败，依赖它的验证任务仍会尝试全量执行，范围判断的失败状态也会保留。

检查在 job 层选择。如果分支保护要求矩阵展开后的单项检查，需要先核对这些要求：
GitHub 跳过矩阵时可能只生成一个跳过状态，不生成每个展开项的状态。新增必需检查时，
也应避免让它所在的工作流被路径过滤整体跳过。参见
[GitHub 必需检查说明](https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/troubleshooting-required-status-checks)。

修改规则后运行：

```bash
uv run pytest -q tests/test_ci_changes.py tests/test_workflow_actions.py tests/test_e2e_workflow_contract.py
uv run python scripts/check_workflow_actions.py .github/workflows .github/actions
actionlint
```
