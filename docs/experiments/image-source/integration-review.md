---
title: 图片功能整合审查与验收结论
description: 草稿 PR 的实现范围、Atomic Memory 适配、真实验收证据及合并前待完成事项。
---

# 图片功能整合审查与验收结论

本实现对应 [#1932](https://github.com/oceanbase/powercontext/issues/1932)，基于 upstream/master 95bfbc9e0b59f58aee119c750c69a448b7666d0c。草稿用于讨论图片 Source 和 Codex 采集实现，完整真实验收与最终复审尚未完成，不作为合并就绪结论。

## 当前实现

图片默认关闭。普通配置向导与单份 .env 提供可选图片配置；可以明确复用支持多模态的 OpenAI Chat generation 连接，也可以独立配置 Vision endpoint。常规 server 安装包含轻量解码依赖。模型自动下载和托管未实现，连接已有本地 endpoint 与管理本地模型是不同能力。

授权附件或显式 PNG/JPEG 上传先保存资产及原图 Source，再执行可恢复的视觉解析任务。解析成为不可变 Source，通过固定文字证据投影进入现有 Artifact 流程。重试、重新解析和历史回读保持精确身份，读取不重新识图。原图不会以 Base64 文字输入普通生成器。

标准 setup codex 管理项目授权和客户端采集器，doctor codex 检查 Scope、连接及生命周期。Dashboard 提供图片列表、缩略图、任务状态、OCR 和原图；Atomic Memory revision 的 sources 可链接精确历史解析。首次插件启用和 Hook 信任仍由宿主要求。没有新增图片专用记忆确认机制，提取、协调、合并和既有写策略保持生效。首版限定文件 SQLite、静态 PNG/JPEG 和已验证的宿主表示。

## 修复与审查状态

Rebase 解决了 Atomic Memory、EvidenceChild、Dashboard 阅读页、Access 与 OpenAPI 冲突。已有 HTTP 路径与 schema 保持原样，七个图片操作及类型重新生成 Python/JS 契约。

- 补齐 Interrupt Hook 集成清单。
- 按授权时间恢复停机期间的新会话，保留已经登记的读取游标。
- 观察记录达到预算后继续投递已暂存 outbox，保持授权前隐私边界。
- watcher 停止超时后终止并回收，避免监督进程因此退出；无效配置安全关闭采集。
- 明确 Memory 协调动作的最终正文与身份要求，结构化生成拒绝空写并使用原有有限重试补全，不放宽来源和身份校验。

实际 Open Code Review 外部审查超时，24 个条目完成、9 个失败，其余未完成。返回的停止超时和异常配置问题已修复。delegate 文件筛选、规则及补查清单已建立，完整补审和修复后复审仍待完成；不能称为外部全量通过。

## 已验证内容及限制

| 验证 | 实际结果 |
| --- | --- |
| 受影响图片、Codex、契约、Dashboard、处理流程测试 | 292 项通过；与其他集合有重叠，不相加宣称独立总数 |
| provider 与配置向导 | 51 项通过 |
| watcher、生命周期与诊断回归 | 29 项通过；停止超时使用受控子进程行为模型 |
| Atomic Memory 协调与生成回归 | 14 项通过，包括空正文后的有限重试 |
| 真实 Codex CLI 0.141.0 exec | PNG/JPEG、多图、恢复、召回测试通过；模型 provider 受控 |
| 真实 Desktop 文件选择器上传 | 新 Scope 中自动形成图片任务，来源为 desktop；未手工创建 Source 或 Memory |
| 真实 MiniCPM-V-4.7-35B-A3B | 复杂图解析成功，原图回读摘要与 1,495,423 字节一致 |
| 实际 Dashboard | 可查看图片列表、缩略图、任务、OCR 与原图；新 Atomic Memory 到原图的完整桌面链路待验收 |
| 官方 Atomic Memory 迁移 | 31 条旧 entry 导入后 ready；原有 70 条 Source 的身份、payload、journal_position 和五个资产摘要保持不变，integrity_check 为 ok |
| Ruff、Linux 目标 ty、契约、锁文件、wheel | 通过；提交前文档及最后回归另见实施记录 |
| 更广兼容测试 | 231 通过、3 跳过、2 个 CLI 失败；两个失败在同版本 upstream 基线上复现 |

当前真实模型可以抽取候选，但新主干的完整后台协调和发布尚未验收通过。Flash/Pro 曾返回无效输出；空正文问题的说明及结构化重试已修复，仍需验证真实后台产物与新会话实际引用。旧 Memory 机制的成功实验记录不能代替当前 Atomic Memory 验收。

Windows 全库 ty 仍有既有 POSIX 原生扫描相关错误，Linux 目标检查通过；没有宣称 Windows 全库全部通过。完整 Python 版本矩阵与文档网站构建尚未完成。

## 合并前待完成

- 当前 Atomic Memory 的真实后台发布、精确来源以及新桌面聊天的显式检索、自动召回和实际答案。
- 数据库与图片资产配对备份的实际恢复，核查历史身份、字节、引用及索引。
- 拖入、粘贴、JPEG、多图、中断、禁用/重开，以及最终完整 Open Code Review 与修复后复审。
- 完整支持版本、文档/CI 检查，以及 macOS/Linux 生命周期和长期资源/质量测试。
- 依据维护者意见收敛公开命名、迁移和 PR 范围；managed-local 下载与运行另行实现。

操作见 [桌面指南](desktop-testing.md)，素材与预期答案见 [复杂测试案例](complex-test-case.md)。私有连接、真实附件、数据库、探测脚本和完整日志位于被忽略的本地目录，不进入 PR。
