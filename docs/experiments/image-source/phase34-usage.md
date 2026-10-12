---
title: 图片 Source 解析、证据与记忆使用说明
description: 阶段三、四的真实模型验证、后台解析、恢复重试、文字消费和精确引用回读。
---

# 图片 Source 解析、证据与记忆使用说明

显式上传 PNG 或 JPEG 后，服务先耐久保存原图和任务，再由独立后台 worker 调用 Vision 模型。合法结果保存为不可变的 experimental.image.analysis，提供文字证据，供既有 Memory、Handoff、Topic Memory 等流程使用。解析成功与记忆生成是两个结果；原图、解析来源和已经引用它的记忆分别保留。

本实验仅在独立 worktree 和文件 SQLite 上运行，默认关闭。可选 Codex CLI 自动捕获、配置引导与原图查看见 [捕获与查看说明](phase56-usage.md)；不管理模型权重。功能边界以 [建设指南](README.md) 和 [固定契约](contracts.md) 为准。

## 配置与启动

日常运行只使用 worktree 根目录的 `.env`。普通配置向导提供可选图片步骤；本机配置了独立 MiniCPM-V-4.7-35B-A3B Vision、DeepSeek 普通生成和 BGE-M3 embedding，实验服务端口为 18171。API Key 仅由本地配置解析器加载，不写入 Source、任务快照或公开日志。新部署默认关闭图片功能。

需要在该实验环境持续使用时，将 POWERCONTEXT_SERVER_IMAGES_ENABLED 改为 true，然后重启服务：

```powershell
cd D:/code/git-project/powermem/powermem-image-source-experiment
uv sync --locked --extra server --extra cli
uv run --locked --no-sync powercontext server run --env-file .env
```

不要复用主 checkout 的数据库、配置或服务。Vision 配置与普通 generation 角色分开：只配置 Vision 可以保存图片解读；自动生成记忆还需要既有的普通生成配置和处理机制。文字检索、历史解读和原图回读不要求视觉模型在线。配置文件修改在服务重启后生效。

## 上传、查询和核查

```powershell
uv run --locked powercontext --server-url http://127.0.0.1:18171 --json image upload .powercontext/image-source-experiment/example.png --scope-id <Scope ID> --capture-key screenshot-1 --context-text "本次报错截图"
uv run --locked powercontext --server-url http://127.0.0.1:18171 --json image task <task_id> --scope-id <Scope ID>
uv run --locked powercontext --server-url http://127.0.0.1:18171 --json image source <analysis_source_id> --scope-id <Scope ID>
uv run --locked powercontext --server-url http://127.0.0.1:18171 image download <asset_id> --scope-id <Scope ID> --output .powercontext/image-source-experiment/restored.png
```

upload 的 202 表示原图、来源和任务已经保存。task 查询可看到 pending、running、retry_wait、succeeded 或 failed，以及有界错误码和逐次尝试。succeeded 才有 analysis_payload；readability 为 unreadable 的成功结果只保留审计，不进入文字生成。

任务查询同时返回精确 original_payload、analysis_payload 和 attempts。Memory 是独立的 atomic-memory Artifact；从精确 Artifact revision 的 sources 中取得解析 source_id 后，可直接使用 image source，或以下 SDK 调用找到对应历史任务、解读和原图资产：

```python
from powercontext.http import ExperimentalGetImageTaskRequest

task = await client.get_image_task(
    ExperimentalGetImageTaskRequest(
        scope_id=scope_id,
        analysis_source_id=analysis_source_id,
    )
)
```

task_id 与 analysis_source_id 必须恰好指定一个。Source 引用统一使用 source_type/source_id；Source payload 的 name 是类型声明。所有读取按 Scope 授权，分享一个 Artifact 不授予它所在 Scope 的原图访问权，Scope 管理权限也不自动等于内容读取权限。

## 自动重试与重新解析

```powershell
uv run --locked powercontext --server-url http://127.0.0.1:18171 --json image retry <failed_task_id> --scope-id <Scope ID> --retry-key retry-1
uv run --locked powercontext --server-url http://127.0.0.1:18171 --json image reparse <original_source_id> --scope-id <Scope ID> --reparse-key reparse-1
```

同一 capture_key 重放返回首次接收结果，不新建解析。retry 只接受失败任务，另建任务并沿用旧配置和上下文；reparse 另建任务，固定当前配置，保留旧任务、解读和 Artifact 引用。操作键在同 Scope、同操作内幂等；同键改变目标或参数返回 409。可以使用 expected_profile_digest 防止重新解析时误选配置。

旧任务只能使用 endpoint、deployment_id、model ID 均匹配的当前凭据。修改模型或地址后，服务不会把新 Key 发送给旧地址；旧任务明确失败，用户可选择 reparse。轮换 Key 本身不会改变解析身份。

鉴权 header 的名称、控制字符及 HTTP 客户端要求的 ASCII 编码在请求前验证。无效配置使任务明确失败，provider_requests 为 0，不停止其他任务的处理；密钥不进入错误信息。

429、5xx、超时和断连在剩余预算内重试；退避先为 2 秒、再为 8 秒，Retry-After 最多 30 秒。默认总尝试数 3，硬上限 5，没有 SDK 隐式修复或重试。无效 JSON、截断输出、实体引用错误、缺少匹配配置及输入超预算为明确失败，不制造占位 Source。

## 恢复与记账

所有进程通过同一 SQLite owner 租约协调，只允许一个有效解析 worker；按 Scope 轮转选取待执行任务。请求超时加 30 秒为执行租约，每 10 秒续期，空闲 owner 租约为 30 秒。网络和预处理在数据库事务外执行。提交成功时，解析 Source、任务终态、队列容量释放和 Topic Memory 水位通知在同一事务中保存。

短暂的 SQLite BUSY／LOCKED 会触发 worker 清理后重新取得 owner，等待从 1 秒增加到最多 30 秒；续期遇到同类锁错误时每秒再试，仍需通过租约与 fencing 校验。它不增加任务的模型调用预算。失去 owner 后不能写回旧响应；未知数据库错误和记录损坏仍明确停止 worker，不无限尝试。

正常关闭停止领取新任务，等待当前有超时限制的调用和数据库操作完成，再释放 owner。待处理任务保留至下一次启用；关闭可能等待当前请求的剩余超时。进程被终止时，需要等旧租约过期才能回收，花掉的尝试仍计入预算，有余量则重新排队，耗尽则失败。陈旧响应不能写入有效解析，也不能再次释放预算。不保证模型调用 exactly-once，中断后可能发生重复请求与费用。

attempt_count 是已领取的尝试数，含未进入网络的前置失败。每条尝试保存状态、输入摘要、HTTP 状态、错误码、延迟和提供方实际报告的 token。provider_requests 为 0 表示没有发起调用，1 表示已知调用尝试，null 表示调用边界或中断后无法确认；token 未知也为 null，均不伪装成零。既有统计增加 image_parsing 用量分类，逐次任务记录是更完整的审计依据，统计队列是尽力记录的运营数据。

已知阶段二格式在真实 SQLite 写事务中增加任务元数据，保留原 Source、资产、接收回执和任务 ID；故障中途回滚后可以重新启动并继续读取。未知版本、缺少核心字段或不完整格式只停用图片能力，不静默重写证据。任务查询使用真实读事务保证状态与尝试记录来自同一快照。

启动时清理孤儿预留失败，只阻止新提交。已接受任务及 retained 原图继续按权限回读；任务状态可以随后台解析进展变化，原图和成功解析保持不可变。带非空 root_path 的部署同样在 JSON 解码前执行 12 MiB 请求体限制，并继续校验 Base64 编码预算和 8 MiB 解码字节预算。

## 文字证据如何进入现有流程

原图始终不进入文字生成，自动消费者过滤它并推进窗口。Readable／partial 解析提供固定文字投影，说明它是机器解读，包含 OCR、可见描述、实体关系及不确定项；投影不含图片字节、密钥、服务地址或本地路径，读取不再识图。部分可读不代表根因或草图决策已经得到确认。

解析以完成时的新 Source sequence 通知下游，因此原图已经被过滤过，也不会漏掉迟到的解读。Atomic Memory 的 Source-only 抽取输入使用固定图片文字证据，随后按现有协调、合并和发布机制生成独立 Artifact。Handoff 与 Topic Memory 使用同一文字视图。Experience、Skill、Profile、Dream、Review 沿用共同生成准入与证据边界。

EvidenceResolver 增加解析到原图的边，原图是 lineage_only。解析是可用文字证据的 root；服务通过原图记录中的「Scope + 原始字节摘要」决定独立证据组，同图重复上传、重试和重新解析保持各条记录，但不增加独立佐证数量。读取上游逐节点执行授权，不信任模型输出或任意 metadata 的归组声明。

## 实际验证与限制

MiniCPM-V-4.7-35B-A3B 已通过真实 Chat 多模态请求和实际后台 worker，识别自制中文截图中的 IMG-034、E429，返回严格六字段 JSON。后台成功调用报告输入 1133 token、输出 200 token、模型请求 2133 毫秒，完整上传至回读检查 3.349 秒。部署标识是本地标签，不能证明固定权重；这些数值是一个样本，不是性能基准。

真实识图结果也已进入受控 Memory 生成链路。重新启动服务并关闭视觉模型后，文字查询得到一条相关记忆，沿引用准确读回同一版解析和原图身份，这一检查没有再次调用模型。

本地验收使用实际 SQLite、HTTP 和本地受控 provider，覆盖有限重试、超时、配置变化、历史回读、同图归组、公平轮转、正常关闭和迟到结果。另有真实子进程终止后等待租约到期、重启并完成新尝试的验证。陈旧响应与迁移失败采用明确标注的故障注入。Memory、Handoff、Topic Memory 使用受控生成器验证存储、消费和引用链路，不代表普通生成模型的质量验收。

固定 Codex CLI 文件附件、可选配置引导和原图查看已接入，见 [捕获与查看说明](phase56-usage.md)。1B、桌面上传／粘贴、复杂小字／模糊图质量、端侧显存、真实断电与上游部署未验收。下一阶段进行工作图片质量、配对召回收益和备份恢复评估。完整检查和代码审查见 [实施记录](implementation-log.md)。
