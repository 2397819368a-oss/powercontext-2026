# dify · 记仇酒馆

> **赛道**：Dify × PowerContext
> **一句话**：用 Dify 编排对话流程，用 PowerContext 存长期记忆，
> 让一个纯本地 3B 模型演活一个「记仇、认账不认脸」的酒馆老板。

**从 `提交材料.md` 开始看**（作品说明、实测数据、截图/录像建议）。
**照着 `app/docs/RUNBOOK.md` 复现**（逐条命令 + 全部踩坑记录）。
设计取舍写进 `app/docs/DESIGN.md`。

## 文件地图

```
dify/
├─ 提交材料.md                       ← 先看这个
├─ README.md                         ← 本文件
├─ app/
│  ├─ docs/DESIGN.md                 设计取舍与「为什么这么做」
│  ├─ docs/RUNBOOK.md                运行手册（复现的唯一入口）
│  ├─ dsl/grudge-tavern.chatflow.yml Dify Chatflow 工作流（8 节点，一键导入）
│  ├─ seed/world.mjs                 酒馆世界设定（单一的真相来源）
│  ├─ seed/seed-demo.mjs             灌种子记忆（演示账本，幂等）
│  ├─ seed/seed.mjs                  开发用账本
│  └─ tools/
│     ├─ pc.mjs                      PowerContext REST 封装
│     ├─ dify-bootstrap.mjs          建账号 → 清理 → 注入 scope/token → 导入 → 发布 → 取 Key
│     ├─ dify-setup-model.mjs        装模型插件 + 注册本地模型 + 设为默认
│     ├─ dify-smoke.mjs              端到端验收（三轮，9 项断言）
│     └─ simulate.mjs                不依赖 Dify 的最小链路版（快速定位问题）
├─ deploy/
│  ├─ docker-compose.override.yaml   镜像换国内源 + 端口改成 8090
│  ├─ pull-images.sh                 串行拉镜像（并发会慢 10 倍）
│  └─ embed_server.py                本地向量服务（e5-small ONNX + mean pooling + L2）
└─ pc-config/powercontext.env.example  PowerContext 配置模板
```

## 关于密钥

仓库里的东西**不含任何真密钥**：

- `pc-config/powercontext.env.example` 的 `AUTH_TOKEN` 是占位符。
- `app/dsl/grudge-tavern.chatflow.yml` 的 `PC_SCOPE` / `PC_TOKEN` 是 `REPLACE_WITH_YOUR_*`
  占位符——工作流导出会把 HTTP 节点的 Bearer 值明文写进 YAML，不脱敏等于泄漏。
- `app/.difykey` 等运行时产物由 `.gitignore` 排除。

`dify-bootstrap.mjs` 会在导入前把真值注入进去，不需要手工改 YAML。

## 实测结果

本机（Windows 11 + Docker Desktop 4.97 / WSL2，全程本地模型，无 GPU）：

```
node tools/dify-smoke.mjs
结果：9 passed, 0 failed      # 连续三轮均为 9/9
```

三个case考察的是「蓝色油纸伞」这个**种子记忆里没有**的标记物：
写入 → 同会话含糊提问能否说出伞名 → **开新会话**能否说出伞名。
检索模式 `hybrid`（全文 + 向量 + RRF）。详见 `提交材料.md` §4。
