---
title: 图片 Source 配置、采集与查看
description: 普通安装向导、标准 Codex 接入、自动生命周期和图片材料入口。
---

# 图片 Source 配置、采集与查看

图片实验默认关闭。普通 config init 提供可选识图配置，setup codex 保存项目授权并管理客户端采集器。日常保持正常 Server 启动及 Codex 使用方式；源码安装、具体配置和验收命令见 [桌面验收指南](desktop-testing.md)，复杂图与答案见 [测试案例](complex-test-case.md)。

| 功能 | 当前行为 |
| --- | --- |
| 配置 | 单份 .env；独立 Vision 或显式复用支持图像输入的 OpenAI Chat generation |
| 依赖 | 轻量解码库包含在常规 server extra；不安装模型权重、Torch 或 CUDA |
| Codex 授权 | setup 授权当前项目，默认关闭；首次或变更 Hook 由用户信任 |
| 客户端运行 | setup 管理启动项和监督进程；会话启动恢复、子采集器故障重启，关闭最后一个项目后停止 |
| Scope | 捕获时使用真实会话／项目绑定；已暂存任务保留接收时的目标 |
| 耐久接收 | 先保留图片字节、原图 Source 与任务，再异步识图 |
| 解析 | 严格校验多模态输出，保存不可变解析 Source，保留原图和模型来源 |
| Memory／Topic | 可读解析进入已有文字证据流程；不改变 families、调度或确认机制 |
| Dashboard | 新增图片材料列表、缩略图、分页和状态，点开原图与解析；Memory 原始材料支持准确图片来源 |
| 诊断 | doctor codex 检查授权、采集器、Scope 和图片服务；image probe 验证模型实际能力 |

当前验证覆盖 CLI 0.141.0 exec 文件附件、真实桌面上传的既有材料、实际 MiniCPM-V 35B、普通生成和 embedding、后端搜索及浏览器回图。复杂测试图的本轮真实模型验证使用显式上传，不能用它替代新 GUI 拖图测试。新桌面会话对 Memory 的实际使用需要单独验收。

PNG/JPEG 每图最多 8 MiB、20 百万像素，边长不超过 16384；每轮最多四图。文件 SQLite 是当前图片后端。宿主记录采用精确版本准入，不扫描授权前或关闭期间历史。交互 CLI TUI、未知版本、其他 Agent、OceanBase／seekdb、PDF／视频和多模态向量检索不在当前自动适配范围。

关闭项目采集执行 powercontext setup codex --no-capture-images；源码安装加 --source ./integrations/codex。卸载插件前先撤销授权。已有 Source、资产和回执不会删除。历史分析引用保持原身份，重试和重解析按 [解析与记忆说明](phase34-usage.md) 创建新的任务，不覆盖旧证据。
