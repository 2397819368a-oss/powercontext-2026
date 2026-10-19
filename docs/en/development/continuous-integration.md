---
title: Continuous integration
description: How pull requests select checks by changed paths and how to run full validation.
---

# Continuous integration

Pull requests select expensive checks through `.github/ci-paths.toml`. The Main, E2E harness,
Native personal service, ZCode acceptance, and Kimi Code acceptance workflows share the
`ci-changes.yml` workflow. Each detection job publishes a **CI scope** table in its Actions summary.

The Main quality job and license check run on every PR. Quality includes formatting, lock and
generated-contract checks, type checks, and the focused tests invoked by `make check` and
`make contract-test`.

## Which checks run

| Changed paths | Additional checks |
| --- | --- |
| `docs/`, `website/`, root READMEs, documentation assets | Website lint, tests, build, and Windows link validation. |
| One host integration, such as `integrations/pi/` | Python unit/E2E matrix, Windows portability, and that host's package or acceptance checks. |
| `evaluation/` | Evaluation unit tests; ZCode guidance also selects ZCode acceptance. |
| Persistence and database regression tests | Python checks and the SeekDB/OceanBase validation jobs. |
| `e2e/bub/` or `integrations/bub/` | Harness validation and SQLite/OceanBase acceptance; the integration also selects Python checks. |
| Native service regression tests | Python checks and native service lifecycle tests on Linux, macOS, and Windows. |
| Shared guidance templates or helpers | Python checks and the host checks listed in the shared guidance rule. |
| Runtime source, OpenAPI, shared dependencies, CI rules, shared fixtures, or unclassified paths | All checks managed by the selector. |

Changes spanning several areas select the union of their checks. Packaged `SKILL.md` files are
host inputs and select host tests even though they are Markdown. Desktop, Skill guidance validation,
and SeekDB lock diagnostics retain their own path filters.

Pushes to `master` run all checks managed by the selector. Manual and scheduled runs
also select all suites, while preserving each workflow's explicit suite or installer inputs.
Use **Run workflow** on Main or Native personal service to request full validation on a selected branch.
New commits to a PR cancel its superseded harness run.

## Maintain path ownership

Patterns follow Python `fnmatch` semantics; `*` can match path separators. Keep shared inputs in
`full`, suite inputs in `suites`, and explicitly exempt repository prose in `ignore`. Shared host
guidance inputs use `shared`. An unclassified path selects every suite rather than bypassing checks.
Add all consumers when registering a new shared input.

The selector compares the PR head with its merge base using a complete, NUL-delimited Git diff.
It includes every PR commit, deletions, and both paths of a rename, without GitHub's path-filter
file-count limit. An unavailable diff selects every suite. If detection itself fails, dependent
validation jobs attempt full execution and the detection failure remains visible.

Checks are selected at the job level. If branch protection requires individual expanded matrix
checks, review those requirements before enabling conditional matrices: GitHub can report a single
skipped matrix job rather than each expanded check. Keep workflow-level path filters out of newly
required checks as well. See [GitHub's required-check guidance](https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/troubleshooting-required-status-checks).

Validate routing changes with:

```bash
uv run pytest -q tests/test_ci_changes.py tests/test_workflow_actions.py tests/test_e2e_workflow_contract.py
uv run python scripts/check_workflow_actions.py .github/workflows .github/actions
actionlint
```
