---
title: "桌面范围统计"
description: "精确范围的原生统计、隔离与兼容资格。"
---

# 桌面范围统计

桌面通过原生 `get_stats` 命令调用 `POST /v1/stats`。渲染层只提供当前
generation 和 `today`、`7d`、`30d` 周期；原生层取得当前 Scope，构造精确、
单范围的 selection。渲染层不能传入地址、范围列表或 all/subtree 模式。

连接必须具有匹配合同且已验证该操作的兼容资格。每次读取沿用连接边界的身份
复核策略，保留 Server 的授权失败结果，并遵守现有传输超时与 1 MiB 响应上限。
权限拒绝后不会扩大查询范围。响应的 selection、范围 ID 和周期必须与请求一致。
按照公共合同，合计及各范围的召回估算器均可为空。

统计有独立的取消通道。新的统计读取、`cancel_stats_reads` 或 generation
变化都会取消待完成统计；Memory 读取的取消不会影响统计。已提交的 Memory
写入继续绑定原目标。

操作目录和可达 wire 类型通过 `pnpm --dir desktop generate` 生成；IPC 声明通过
Rust `export_ipc` example 生成。原生的精确选择投影复用具体生成字段类型，同时
保留可空估算器并禁止扩大 selection。不改变 Server API 或历史资格记录。

已验证的 Server、合同、wheel 标识和实际验证边界见
[资格记录](../../../desktop/VALIDATION.md#current-scope-statistics-qualification)。
模拟测试验证本地处理；构建 wheel 的独立夹具另行验证实际 SQLite 协议行为。
Windows/macOS 安装后的页面验收仍由平台 gate 负责。
