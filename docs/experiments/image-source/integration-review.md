---
title: 图片功能整合审查与验收结论
description: 图片实现范围、review 修复状态、RFC 架构差异及合并前待完成的验收。
---

# 图片功能整合审查与验收结论

本实现对应 [#1932](https://github.com/oceanbase/powercontext/issues/1932)，基于 upstream/master `558b8262d0f332a1eef8759faf3423fce5357155`。PR #1956 用于审查图片 Source 和 Codex 采集实现；架构对齐和完整真实验收尚未完成，不作为合并就绪结论。

## 当前实现

图片默认关闭。普通配置向导与单份 .env 提供可选图片配置；可以明确复用支持多模态的 OpenAI Chat generation 连接，也可以独立配置 Vision endpoint。常规 server 安装包含轻量解码依赖。模型自动下载和托管未实现，连接已有本地 endpoint 与管理本地模型是不同能力。

授权附件或显式 PNG/JPEG 上传先保存资产及原图 Source，再执行可恢复的视觉解析任务。解析成为不可变 Source，通过固定文字证据投影进入现有 Artifact 流程。重试、重新解析和历史回读保持精确身份，读取不重新识图。原图不会以 Base64 文字输入普通生成器。

标准 setup codex 管理项目授权和客户端采集器，doctor codex 检查 Scope、连接及生命周期。Dashboard 提供图片列表、缩略图、任务状态、OCR 和原图；Atomic Memory revision 的 sources 可链接精确历史解析。首次插件启用和 Hook 信任仍由宿主要求。没有新增图片专用记忆确认机制，提取、协调、合并和既有写策略保持生效。首版限定文件 SQLite、静态 PNG/JPEG 和已验证的宿主表示。

## 修复与审查状态

主干集成保留 Atomic Memory、EvidenceChild、Dashboard 阅读页、Access 与 OpenAPI 契约。已有 HTTP 路径与 schema 保持原样；七个图片操作及 Python/JS/Desktop 类型由官方脚本生成。

当前契约包含召回通道权重、score 约束、启动上下文、Working Note 和统一 Candidate。图片采集与 Codex 启动上下文共用正常会话入口，原图和不可读解析仍不能进入生成流程，Working Note 的资格限制保持生效。Dashboard 保留审核页、Scope 读取和历史版本，并支持 Memory 精确图片证据链接。Desktop 的操作摘要及类型由合并后的 OpenAPI 生成；Memory 延迟审计使用主干实现，MiniCPM 适配保留。

- 补齐 Interrupt Hook 集成清单。
- 按授权时间恢复停机期间的新会话，保留已经登记的读取游标。
- 观察记录达到预算后继续投递已暂存 outbox，保持授权前隐私边界。
- watcher 停止超时后终止并回收，避免监督进程因此退出；无效配置安全关闭采集。
- 明确 Memory 协调动作的最终正文与身份要求，结构化生成拒绝空写并使用原有有限重试补全，不放宽来源和身份校验。
- 暂存容量不足时保留读取位置，投递后自动续读；兼容仍在库中的旧容量错误记录，保留授权时期检查和未投递材料。
- 在投递分页之前筛选项目授权，关闭项目的积压不会占据其他项目的投递页；每次发送前重新核对授权。

2026-10-25 核对 GitHub 上的三条代码 review：容量压力保留读取游标并兼容旧错误状态；授权筛选发生在投递分页前，发送前再次检查；builtin extra 声明直接依赖 platformdirs，CI 保留 builtin 并加入 images 隔离安装。对应行为与实际 wheel 隔离安装的验证结果见实施记录。三条线程仍未关闭，本次核对没有代审查者标记解决。

此前 Open Code Review 的外部执行和 delegate 补审范围分别记录于实施记录。此次范围是既有 review 和主干冲突涉及的行为，不是对整个 PR 的新一轮完整审查。

## 架构反馈状态

[RFC #1972](https://github.com/oceanbase/powercontext/pull/1972) 已提交，包含 Artifact 复用、存储职责及表结构比较，并有 2026-10-15 的批准记录；截至本次核对仍未合入。RFC 提议使用 `image-analysis` Artifact、确定性发布 Source，以及 `pc_media_assets`、`pc_media_processing_requests`、`pc_media_capacity` 三张共享媒体表。

当前运行时仍把解析结果保存为不可变 Source，使用七张 `pc_experimental_image_*` 表。因此“先完成 RFC 设计审查、复用 Artifact、收敛新增表”的反馈尚未全部落实到实现。此次 rebase 保留已有身份、恢复和历史读取行为，架构调整需要单独实现并验证迁移及兼容策略。

## 已验证内容及限制

| 验证 | 实际结果 |
| --- | --- |
| 2026-10-25 review 核对 | 采集投递与 Desktop watcher 37 项通过；Python 3.11、3.14 的 builtin/images 四组真实 wheel 隔离安装均通过 |
| 2026-10-25 主干集成 | 图片、权限、Memory 协调、HTTP 资源限制及契约集合 257 项通过；唯一失败为 Windows 测试路径过长，该项使用短临时目录重跑通过。没有修改生产逻辑或断言 |
| 2026-10-25 构建与检查 | Desktop lint、98 项 UI 测试和 build 通过；Python/JS/Desktop 生成检查、两份 lock 检查及 Linux 目标 ty 通过。原生 Windows ty 的 34 项诊断位于 PR 未改动的代码中，其他 pre-commit hooks 通过 |

当前真实模型可以抽取候选，但新主干的完整后台协调和发布尚未验收通过。Flash/Pro 曾返回无效输出；空正文问题的说明及结构化重试已修复，仍需验证真实后台产物与新会话实际引用。旧 Memory 机制的成功实验记录不能代替当前 Atomic Memory 验收。

Windows 本地全库 ty 仍有既有 POSIX 原生扫描相关错误，Linux 目标检查通过；没有宣称本地 Windows 全库全部通过。已有 GitHub CI 通过不能替代真实图片、普通模型和新桌面会话的独立验收。

## 合并前待完成

- 当前 Atomic Memory 的真实后台发布、精确来源以及新桌面聊天的显式检索、自动召回和实际答案。
- 数据库与图片资产配对备份的实际恢复，核查历史身份、字节、引用及索引。
- 拖入、粘贴、JPEG、多图、中断、禁用/重开，以及最终完整 Open Code Review 与修复后复审。
- 新提交的文档/CI 检查，以及 macOS/Linux 生命周期和长期资源/质量测试。
- 对齐 RFC #1972 的解析 Artifact、共享媒体存储及 Memory 时间依据，确定现有实验记录的迁移和兼容策略；收敛公开命名与 PR 范围。managed-local 下载与运行另行实现。

操作见 [桌面指南](desktop-testing.md)，素材与预期答案见 [复杂测试案例](complex-test-case.md)。私有连接、真实附件、数据库、探测脚本和完整日志位于被忽略的本地目录，不进入 PR。
