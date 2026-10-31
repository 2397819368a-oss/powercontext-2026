# powercontext-2026（OceanBase 大赛 · 团队仓库）

这是 PowerContext 大赛的团队仓库，**每个合作方赛道一个子目录**：

| 目录 | 赛道 | 状态 |
|---|---|---|
| `minimax/` | MiniMax 赛道（规格驱动的工程上下文模块） | ✅ 完整实现，7 项自检全过，含真实调用记录 |
| `qoder/` | Qoder 赛道（Obsidian 笔记库检索助手） | ✅ 完整实现，含在线站点与开发记录截图 |
| `dify/` `gaode/` `stepfun/` | 其他合作方赛道 | 占位 |

## 直接跑测试

仓库根目录已配好 `pytest.ini`，在根目录执行 `pytest` 即可跑 MiniMax 赛道的
业务工程测试（预期 `10 passed`）：

```bash
pip install -r minimax/requirements.txt
pytest
```

## 各赛道的入口说明

- **MiniMax**：见 [`minimax/README.md`](minimax/README.md)，一键自检 `python minimax/samples/self_check.py`
- **Qoder**：见 [`qoder/README.md`](qoder/README.md)，在线站点地址与运行说明都在里面
