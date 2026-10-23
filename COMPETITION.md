# 2026 OceanBase 数据库大赛 · PowerContext 赛道

- 参赛者：Latinaxia
- 仓库地址：https://github.com/Latinaxia/powercontext
- 基于：`oceanbase/powercontext` 的 `2026-competition` 分支
- 初始基线提交：`9db1753`

## 环境要求

- Ubuntu 24.04（WSL2）
- Python >= 3.11
- [uv](https://docs.astral.sh/uv/) >= 0.12

## 快速开始

```bash
uv sync --locked
make check
make test
```

## 评测命令

### LoCoMo-Plus（记忆检索与问答）

`benchmark/locomo_plus` 默认从 `raw.githubusercontent.com` 下载数据集。若网络不可达，
可先用镜像把数据预置到 `.cache/locomo_plus/`：

```bash
mkdir -p .cache/locomo_plus
curl -L -o .cache/locomo_plus/locomo10.json \
  https://ghproxy.net/https://raw.githubusercontent.com/xjtuleeyf/Locomo-Plus/059f4e3d38f7f1f96765e8e2cb7de3097551bffb/data/locomo10.json
curl -L -o .cache/locomo_plus/locomo_plus.json \
  https://ghproxy.net/https://raw.githubusercontent.com/xjtuleeyf/Locomo-Plus/059f4e3d38f7f1f96765e8e2cb7de3097551bffb/data/locomo_plus.json
```

```bash
uv run python -m benchmark.locomo_plus inspect
uv run python -m benchmark.locomo_plus run --profile smoke --limit 2
```

### 记忆容量

```bash
uv run python -m benchmark.memory_capacity \
  --output .artifacts/memory-capacity/run-01/sqlite.json
```

## 目录说明

| 路径 | 说明 |
| --- | --- |
| `src/` | PowerContext 核心实现 |
| `benchmark/locomo/` | LoCoMo 对话记忆评测 |
| `benchmark/locomo_plus/` | LoCoMo-Plus 事实与认知记忆评测 |
| `benchmark/memory_capacity/` | 容量、append 延迟与压缩评测（无需模型） |
| `evaluation/` | 编码与记忆评测、性能基准 |

## 进度

- [x] 创建赛题仓库并切换到 `2026-competition` 分支
- [ ] 本地环境与测试跑通
- [ ] 记录 baseline 指标
- [ ] 提交优化与复现结果