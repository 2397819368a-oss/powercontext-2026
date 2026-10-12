---
title: 图片 Source 实验数据与操作契约
description: 阶段一固定的数据类型、幂等身份、任务状态、权限、配置、预算和后续实现验收。
---

# 图片 Source 实验数据与操作契约

本契约固定实验格式 `1`，沿用「原图资产与原图 Source → 持久任务 → 解析 Source → 文字证据」链路。显式接收、视觉 worker、不可变解析、重试与文字消费见 [解析与记忆说明](phase34-usage.md)；固定 CLI 自动捕获、可选配置与精确原图查看见 [捕获与查看说明](phase56-usage.md)。

HTTP schema 的唯一来源是 [openapi/powercontext.yaml](../../../openapi/powercontext.yaml)。五个操作均在标准 paths，implemented_operations 列出完整实现，active: true 表示实验链路已实现，不等于默认开启。planned_paths 为空，不提前加入 MCP 白名单。

## 类型与来源

| 数据 | 固定类型／版本 | 内容与职责 |
| --- | --- | --- |
| 原图 Source | `experimental.image.original`，Definition `1`，captured | `name`、资产信息、capture_key、origin、context；保存一次实际上传的来源 |
| 解析 Source | `experimental.image.analysis`，Definition `1`，captured | `name`、带 Scope 原图引用、task_id、配置与摘要、模型输入摘要、上下文摘要、经校验 output |
| 资产 | `ExperimentalImageAsset` | asset_id、原始字节 SHA-256、PNG／JPEG、字节数、原始像素尺寸；不内嵌字节 |
| 任务 | `ExperimentalImageTask` | 状态、revision、累计尝试数、上限、profile_digest、解析引用、错误码、下次执行时间；task/get 填充 original_payload、analysis_payload 与最多五条 attempts；可通过解析 source_id 找回精确历史 |
| 文字投影 | `powercontext.text-evidence`，版本 `1` | 仅解析 Source 提供；身份指向精确解析记录；读取不推理 |

`source` 引用使用既有 `SourceTypeReference` 的 `source_type/source_id`。引用外包一层 `scope_id`；原图、上下文、任务与解析必须在同一 Scope。Scope 是不透明标识，不用文件路径、图片文字或模型输出推导，也不做 Unicode 或空白归一化。

`origin.kind` 为 `explicit_upload` 或 `codex`，attachment_index 为 0 至 3。显式上传可不提供 host/session/turn；Codex 自动提交必须同时提供 host_instance_id、session_id、turn_id。宿主版本和输入能力写入受管理适配器配置与运行记录，不把未知字段塞进原图 payload。

`context.text` 是实际发送给模型的有界上下文，可为空；有来源时附带同 Scope 的精确文字 Source 引用。上传时固定文本，后续不根据当前会话重新总结。宿主提示超过上限时按 Unicode 字符截断为前 2048 字符并明确记录截断；显式 API 超限直接拒绝。上下文只解释图片所属任务，不构成模型输出的事实证明。

提供 context.source 时，服务额外校验同 Scope 文字来源存在且上传者有读取权限；仅有 contribute 的调用方可以省略该引用，提交自己的文本。来源引用不能代替文本快照，也不能借摄取接口读取或泄露未授权的既有内容。

两个无模型调用的原生 Definition 在阶段二加入共同 BUILTIN_SOURCE_REGISTRY，即使功能关闭也保留，以便 Server、SDK 和 Artifact 子进程读取旧记录。不得仅在主进程注入自定义 registry，也不得删除子进程一致性校验。名称已被历史 manifest 占用时，图片接收报告冲突，不混用原生与 observation 身份。

## Canonical 身份和幂等规则

令 `H(v)` 为 RFC 8785 canonical JSON 的 UTF-8 字节 SHA-256 十六进制值，`D(v) = sha256:H(v)`。字典顺序不影响摘要；字符串内容、Scope、附件位置和模型部署变化会影响对应身份。未知字段禁止，optional 缺省和显式 null 通过同一个 schema 归一化。为保持历史原图 Source 的权威指纹，新添的三个来源字段在值为 null 时不进入 OriginalImageSource 序列化；原有字段仍保留原来的 null 形式。

| 标识 | 格式与 canonical 输入 |
| --- | --- |
| 原始资产 | `ia_` 加原始文件字节 SHA-256；资产索引与物理路径均按 Scope 分区 |
| 原图 Source ID | `img_` + H({contract_version: "1", scope_id, capture_key, attachment_index}) |
| 初始任务 ID | `it_` + H({contract_version: "1", kind: "capture", original, profile_digest, context_digest}) |
| 手动重试任务 ID | `it_` + H({contract_version: "1", kind: "retry", scope_id, parent_task_id, retry_key}) |
| 重新解析任务 ID | `it_` + H({contract_version: "1", kind: "reparse", scope_id, original_source_id, reparse_key}) |
| 解析 Source ID | `imga_` + H({contract_version: "1", task_id})；不由模型输出决定 |
| 配置摘要 | D({contract_version: "1", protocol: "openai_chat", output_schema_version: "1", profile}) |
| 上下文摘要 | D(完整 ExperimentalImageContext)；文本和来源都计入 |
| 模型输入摘要 | `sha256:` 加实际提交的预处理图片字节 SHA-256 |

capture_key 由调用方稳定生成，非空白且不超过 256 字符。旧 CLI 0.141.0 保持 `codex:` 加会话／轮次摘要的既有键和旧来源字段，维持 Server 重放兼容。现代 Codex 使用 `codex-message:` 加宿主实例、会话、轮次、消息身份的稳定摘要，附件序号由 origin 保存；不使用 prompt 或图片摘要认定消息身份。缺少宿主消息 ID 时，使用同一文件中的记录起始偏移，并校验文件身份。

origin 可选增加 `message_id`、`representation` 和 `client_variant`。`original_file` 表示验证过的磁盘原文件；`host_image` 表示宿主实际提交的图片块，可能已经缩放。Desktop 与现代 CLI 使用后者，不能把小图字节相同推广为所有原图都相同。client_variant 可表达 cli_exec／cli_tui／desktop，但捕获适配器仅开放实测版本与入口；枚举存在不等于 TUI 自动捕获已验收。

摄取重放首先按 Scope、capture_key、attachment_index 查询耐久接收记录，比较原字节摘要、声明 MIME、来源和固定上下文。相同返回原资产、Source、sequence 和初始 task_id，`replayed: true`；不同返回 409。**重放不能先选择当前配置**，否则配置改变会让一次接收变成另一任务。第一次选择的配置与响应必须持久化。

相同字节的不同上传共用同 Scope 资产，分别保留来源 Source；跨 Scope 不复用索引或读取授权。首版不缓存或复用模型解析结果，仅去重原始资产和相同请求；后续确需结果缓存时必须完整计入输入、配置和上下文摘要，并为本次来源创建自己的解析记录。

手动 retry_key、reparse_key 非空白且不超过 128 字符。同键同请求返回首次创建的任务，即使当前配置已变；同键不同请求返回 409。重试新任务固定旧失败任务配置和上下文；重新解析首次接收时固定当前配置，expected_profile_digest 不匹配时拒绝。旧失败任务、旧解析及其 Artifact 引用不修改。

一次任务最多产生一个有效解析 Source。晚到响应必须验证 claim，不能靠更换 Source ID 保存第二份结果。时间戳、用量和错误详情保存在受管理任务／尝试记录中，不参与不可变解析 payload；重放不使用当前时间重建 Source。

## 五个最小操作

所有操作 POST、application/json、current Scope 模式；授权沿用既有身份与 Access 规则，scope_id 在 body 中。五个操作已有 HTTP、Client 和 CLI 调用入口；重试和重新解析同时要求 scope.read 和 scope.contribute。

| 路径与 operationId | 输入 | 返回与权限 |
| --- | --- | --- |
| `/v1/experimental/images/capture`；experimental_capture_image | scope_id、capture_key、media_type、bytes_base64、origin、context | 202：accepted、original、asset、sequence、task_id、replayed；scope.contribute |
| `/v1/experimental/images/asset/get`；experimental_get_image_asset | scope_id、asset_id | 200：asset 与精确原始 bytes_base64；scope.read |
| `/v1/experimental/images/task/get`；experimental_get_image_task | scope_id、task_id 或 analysis_source_id，恰好一个 | 200：当前任务、历史来源与尝试；scope.read |
| `/v1/experimental/images/task/retry`；experimental_retry_image_task | scope_id、旧 task_id、retry_key | 202：新的 pending 任务；只允许 failed；scope.read 与 scope.contribute |
| `/v1/experimental/images/reparse`；experimental_reparse_image | scope_id、原图 SourceTypeReference、reparse_key、可选 expected_profile_digest | 202：以当前配置固定的新 pending 任务；scope.read 与 scope.contribute |

bytes_base64 仅接受原始 Base64 编码，不接受 data URI、文件路径或远程 URL。Server 严格解码并检查实际格式、尺寸和预算；不依据文件后缀或声明 MIME 判断安全接收。PNG 动画、JPEG 多帧及其他容器不属于首版静态单图支持范围。

| 返回 | 意义 |
| --- | --- |
| 202 accepted | 原资产、原图 Source、任务和幂等响应均已耐久保存；解析和 Memory 尚未完成 |
| 401／403 | 既有鉴权或 Scope 权限拒绝；先授权再查询内容，不泄露其他 Scope 的存在性 |
| 404 | 当前授权 Scope 内没有对应资产、任务或来源 |
| 409 | 摄取／操作键冲突、状态不允许、配置摘要不符或类型名称冲突 |
| 413 | 原始字节、请求体或解码像素超预算 |
| 415 | 不支持的实际格式或 MIME 与实际格式不符 |
| 422 | 请求结构、Base64、图片解码、上下文引用或业务字段无效 |
| 410 | 曾保存的原材料已明确移除或不可恢复；保留精确来源，不重定向到别的图片 |
| 503 | 功能关闭、图片配置／格式不可用、队列满、资产预算不足或授权服务不可用；用有界 error_code 区分 |
| 500 | 内部存储失败或已保存资产摘要校验失败；不返回伪造成功 |

asset/get、task/get 在功能关闭时仍可使用；没有模型凭据不影响已保存证据的读取。授权服务不可用仍为 503 并拒绝访问。read 校验资产摘要，损坏材料不能作为正常图片返回。

孤儿维护失败时拒绝新 capture、retry 和 reparse，但不因维护错误阻断已保存任务和 retained 原图的读取；格式不兼容、授权失败和实际资产损坏仍按原规则拒绝。请求体预算在路由的 root_path 前缀移除后执行，带前缀部署与普通部署一致。

同一消息最多拆分四个单图请求；顺序和独立接收结果都要显示。第五张明确未接受，不能静默忽略。独立请求没有消息级原子性。

## 图片、输出和初始资源预算

以下是本地 SQLite 实验的保护默认值，依据有界传输、单图任务和避免无界存储选择，**没有性能测量结论**。常规配置可以调低；涉及 schema 硬上限的调高必须先修改契约和兼容说明。

| 预算 | 初始默认／硬上限 | 实施边界 |
| --- | --- | --- |
| 原始单图 | 8 MiB／8 MiB | Base64 最大 11,184,812 字符；服务解码后再次校验 |
| HTTP 请求体 | 12 MiB／12 MiB | 在 JSON 解析前进行流式字节限制，也处理无 Content-Length 的请求 |
| 图片像素 | 20,000,000；单边 16,384 | 检查尺寸后再解码，拒绝超限或损坏图 |
| 单图／单轮附件 | 1／4 | 单图独立任务，无跨图联合推理 |
| 待处理任务 | 每 Scope 256，全局 1024 | pending、running、retry_wait 总和；检查与接受事务一致 |
| 原始资产空间 | 全局 1 GiB | 按持久资产实际字节计数并原子预留；同 Scope 去重不重复计费 |
| 图片 worker | 并发 1 | 按索引查有界待执行队列，Scope 公平轮转，不扫全库 Source |
| 单次模型超时 | 120 秒／300 秒 | 不在 DB 事务中等待网络 |
| 自动总尝试数 | 3／5 | 包括所有实际 provider 请求；SDK 隐式重试关闭 |
| 输出 token | 4096／8192 | 请求中显式限制；服务仍验证输出大小 |
| 解析 JSON | 64 KiB UTF-8 | 在解析输出前限制，超限失败 |
| 文本上下文 | 2048 字符 | image-only 允许空文本 |
| OCR／视觉描述 | 16,384／4096 字符 | 均为 schema 上限，不能用截断掩盖无效模型输出 |
| 实体／关系／不确定项 | 各 16 条 | label 上限 512；entity_id 上限 64 |
| 孤立资产清理 | 单批 100，至少 24 小时宽限 | 排除有效引用、活跃写入和预留；保护跨进程接收 |

空间上限用于原始资产；预处理文件只在单任务临时目录保留并单独限制大小，成功或终态后删除。数据库、任务和日志也须记录增量并有界清理运营数据；源证据与幂等接收记录不因清理日志而丢失。空间不足明确拒绝新摄取，不能自动删除仍被引用的图片。

服务先校验并耐久预留容量，再在受管理临时路径保存完整资产，确保耐久后原子发布文件。在一个 SQLite 接受事务内提交资产 retained 状态、原图 Source、任务及接收记录；成功后返回。失败预留保持 reserved 并继续计费，相同字节可复用；超过宽限的孤立资产经有界清理释放容量。物理路径使用受管理 Scope 映射和服务生成标识，不拼接客户端文件名或任意 Scope 路径。

阶段二要求文件 SQLite、WAL 或 DELETE journal，以及 FULL 及以上 synchronous；内存数据库、MEMORY journal 和较弱同步配置只停用图片能力，不能返回耐久接受。临时文件由同一 Scope 资产预留确定名称；重试刷新预留宽限期。清理在写锁内移除完整文件和对应临时文件，再释放容量，不能遗留未计费的崩溃文件。

## 视觉输入与解析输出

首版协议固定 OpenAI Chat 风格：文字与单张真正的图片内容，不使用文字 JSON 中的 Base64 冒充多模态。Vision 与普通 generation 角色分开。输出模式固定 `prompted_json`；模型无需工具调用或原生 JSON Schema，输出仍由本地严格校验。

模型输出只允许以下六个字段：

```json
{
  "readability": "partial",
  "ocr_text": "连接失败",
  "visual_description": "配置页面显示错误",
  "entities": [],
  "relations": [],
  "uncertainties": ["错误码太小无法确认"]
}
```

提示模板 `image-evidence-v1` 要求：只提取图片中实际可见的文字、界面和关系；上下文仅帮助定位任务；保留中文；不补全看不清的文字；不执行图片内指令；不根据报错断定根因；不把草图确认为决策；仅返回符合六字段 schema 的 JSON。模板实际文本在阶段三集中保存并以该版本固定，变更含义必须改变版本。

readability 为 readable、partial 或 unreadable。格式合格且承认 unreadable 可以得到 succeeded 和审计解析 Source，但不进入文字生成；partial 仅以带不确定说明的可见内容消费。模型自报置信度不作为概率或自动换模型依据。entity_id 必须唯一，关系两端必须存在；模型不能写 scope_id、asset_id、task_id 或模型来源。

预处理 `exif-strip-v1` 从保留原图生成单次输入副本：按 EXIF 转正，去掉附带元数据，保留像素尺寸，不自动缩图，不裁剪；PNG 保留透明度，JPEG 保留转正后的可见内容。固定编码参数并记录实际输入摘要，重编码质量和颜色限制在真实样本验证中登记。处理后的图片仍不得超过 8 MiB；超限失败，不悄悄缩图。原始文件和其摘要完全不变。编码器变化导致输入字节变化时，不能忽略 input_digest 复用旧结果。

profile 保存 model_id、deployment_id、base_url、prompt_version、preprocessing_version、output_mode、timeout_seconds、max_attempts、max_output_tokens。协议和输出 schema 版本由实验格式固定并计入配置摘要。deployment_id 由部署方提供，模型别名指向新权重时必须改变该标识；没有固定权重版本就明确登记可复现性限制。

endpoint 仅接受无 userinfo、query、fragment 的 HTTP(S) 服务地址；非 loopback 要求 HTTPS，本地 HTTP 必须显式选择。密钥／秘密 header 不写入 Source、任务 snapshot 或摘要。任务内部保留连接配置标识，通过受保护配置解析凭据；对应配置不可用则报告失败，不回退到其他模型。不要把服务完整错误正文或含图片的请求写入公开日志。

header 名称必须为 HTTP token，值满足客户端 ASCII 编码及控制字符约束。无效 header 为前置永久配置失败，实际网络尝试数为 0；不得让配置编码或协议异常停止整个 worker。

首版只配置一个当前 Vision 连接。连接标识由 base_url 和 deployment_id 的 canonical 摘要确定；旧任务只能使用与该标识和目标 model ID 匹配的当前受保护凭据。凭据轮换不改变连接身份；换 endpoint、部署或模型后不能把新凭据发送给旧任务的地址。找不到匹配连接时报告配置不可用，用户可显式重新解析。首版不增加多连接或密钥管理产品。

## 任务与恢复

| 转移 | 规则 |
| --- | --- |
| 接收 → pending | 所有原图、任务及接收记录已保存，attempt_count 为 0 |
| pending／到期 retry_wait → running | 原子获取 claim、提升 revision、记录本次尝试，再发出模型请求 |
| running → retry_wait | 断连、超时、429／5xx 等临时错误且预算有余；退避初值 2 秒、8 秒，Retry-After 最多 30 秒 |
| running → failed | 永久错误、无效 JSON／schema、输入预算超限、凭据不可用或尝试耗尽 |
| running → succeeded | claim 仍有效，解析 Source、任务状态和下游通知同一事务提交 |
| 未成功任务 → cancelled | 后续受管理取消或不可恢复来源删除；首版不新增取消 HTTP 操作 |
| 重启发现遗留 running | 回收旧 claim；有预算则重新排队，耗尽则 failed；已计入的尝试不倒扣 |

任务 revision 单调增加，attempt_count 不超过自己的 max_attempts。succeeded 才有 analysis 引用且 error_code、next_attempt_at 为空；retry_wait 才有 next_attempt_at；failed 必须有有界错误码。格式验证通过不替代这些跨字段不变量。

自动重试仍使用同一任务与配置；手动重试另建任务、重新分配有界预算，旧失败任务不修改。重新解析固定当前配置并保留历史成功解析。首版不保证模型调用 exactly-once：请求发出后进程中断，恢复可能重复付费，但只允许一个有效解析 Source。

claim 需要租约和 fencing 校验，执行租约为请求超时加 30 秒，每 10 秒续期；全局 owner 保证一个有效 worker，空闲租约 30 秒。关闭开关不新领任务，已有请求完成当前耐久提交；关机中断仍按旧 claim 恢复。取消／配置变化不能让陈旧响应覆盖新状态。推理期间不持有数据库事务。

SQLite BUSY／LOCKED 的元数据故障支持在清理旧 session 后按 1 秒至 30 秒退避重新获取 owner；续期暂时阻塞时每秒再试。owner 失效后必须重新取得所有权，陈旧 claim 仍被 fencing 拒绝。未知数据库错误和损坏记录不自动无限重试；元数据恢复不另增隐式模型请求预算。

解析写入使用现有 SourceRepository 的冲突、journal 和 dirty 规则，并在新插入时调用 processing_pending.raise_source 提升 Topic Memory 水位；与任务成功确认共享事务。结果使用完成时的新 sequence，不修改原图 sequence；原图已被过滤并推进过的消费者仍能看到迟到解析。

## 文字消费、证据与权限

原图必须在共同 generation eligibility 规则中明确排除。没有 projection 不足以阻止它进入当前生成器；自动窗口跳过原图但推进 through，显式请求原图参与文字生成时拒绝。失败任务不制造可消费的占位 Source。

有效解析以固定文字投影提供 OCR、视觉描述、关系和不确定项，标明这是机器解读，不把不可见根因写成事实。投影不含图片字节、密钥或本地路径，不随当前模型配置变化。readability 为 unreadable 的结果只保留审计和回图路径。

Memory 和 Handoff 接入共同 projection；Topic Memory 当前的原生读取分支需要明确增加兼容入口。Experience、Profile、Dream、Skill、Review 检查共同准入和证据路径，不能只验证 Memory 一条路线。具体入口见 [接入分析](contract-integration-analysis.md)。

EvidenceResolver 为解析 Source 增加到原图的 upstream，原图为 lineage_only，不进入文字模型；解析作为可用文字证据的 root。服务按「Scope + 原始资产摘要」保守归组：不同上传、重试和重新解析仍各保留记录，但不能作为多份独立佐证。身份、归组和 lineage 由服务确定，不能信任模型或任意 metadata。

所有摄取、原图回读、任务和重新解析都按 Scope 授权；管理员权限不自动等于内容读取权限。只获某个 Artifact 分享权限的用户不能因此读取原图所在整个 Scope。来源遍历逐节点授权，缺权限明确不可读，不开放裸资产 URL 或无鉴权静态目录。

## 开关、配置与兼容

下表已接入 ServerSettings，供图片接收预算和非秘密 Vision 快照使用；只有启用且拥有 worker 角色的实验服务才调用视觉模型。图片模块属于独立 images 设置，模型角色按现有 inference 命名方式增加 vision 字段。

| 设置字段 | 环境变量 | 默认 |
| --- | --- | --- |
| images.enabled | POWERCONTEXT_SERVER_IMAGES_ENABLED | false |
| images.asset_directory | POWERCONTEXT_SERVER_IMAGES_ASSET_DIRECTORY | 独立 .powercontext/image-source-experiment/assets/ |
| images.worker_concurrency | POWERCONTEXT_SERVER_IMAGES_WORKER_CONCURRENCY | 1 |
| images.max_pending_per_scope | POWERCONTEXT_SERVER_IMAGES_MAX_PENDING_PER_SCOPE | 256 |
| images.max_pending_total | POWERCONTEXT_SERVER_IMAGES_MAX_PENDING_TOTAL | 1024 |
| images.max_asset_bytes_total | POWERCONTEXT_SERVER_IMAGES_MAX_ASSET_BYTES_TOTAL | 1,073,741,824 |
| inference.vision_model | POWERCONTEXT_SERVER_INFERENCE_VISION_MODEL | 空；精确服务 model ID，由专用 Chat 适配器构造 |
| inference.vision_base_url | POWERCONTEXT_SERVER_INFERENCE_VISION_BASE_URL | 空 |
| inference.vision_deployment_id | POWERCONTEXT_SERVER_INFERENCE_VISION_DEPLOYMENT_ID | 空 |
| inference.vision_headers | POWERCONTEXT_SERVER_INFERENCE_VISION_HEADERS | 空；受保护 SecretStr 值，禁止回显 |
| inference.vision_timeout_seconds | POWERCONTEXT_SERVER_INFERENCE_VISION_TIMEOUT_SECONDS | 120 |
| inference.vision_max_attempts | POWERCONTEXT_SERVER_INFERENCE_VISION_MAX_ATTEMPTS | 3 |
| inference.vision_max_output_tokens | POWERCONTEXT_SERVER_INFERENCE_VISION_MAX_OUTPUT_TOKENS | 4096 |

宿主自动采集默认关闭，Server 开关不等于宿主自动开启。CLI 可显式设置 POWERCONTEXT_CODEX_CAPTURE_IMAGES；Desktop 使用 `image_capture_config.py` 持久记录准确工作目录、Scope 和 Server URL 的授权。保存的关闭状态优先于继承的环境 opt-in。Scope 禁用默认回退，旧任务不能转投新的地址或 Scope。

已验证运行时为 CLI 0.141.0 exec、0.162.0-alpha.2 exec、0.162.0-alpha.17.2 exec，以及两个明确 alpha 版本的 source=vscode／originator=Codex Desktop 运行内核。CLI 的 Submit 注册当前轮次，Stop／Interrupt 补充；Desktop 只查询启用项目的本地索引元数据，并增量读取对应文件。已有会话首次启用从 EOF 开始；启用之后新建的会话允许捕获首轮。关闭再开启重置授权时期，旧读取游标退休，不补导关闭期间的消息。桌面界面验收和未知版本支持状态见 [宿主证据](codex-capture-evidence.md)。

客户端 SQLite Outbox 使用版本 2，暂存图片字节与游标推进在同一事务内完成。上传成功后清除字节，保留接收回执与字节摘要，同一消息／位置换图不能静默当作重放。网络失败保留 bytes，恢复后继续投递；同地址同 Scope 重新授权可发送关闭前已暂存的材料。早期内部版本 1 不自动迁移或抹除，明确拒绝；旧 JSON 完整回执支持重放，部分旧检查点不保证自动接续，需核查 Server 或显式上传。既有 Server Source、资产和任务读取不受客户端状态版本影响。

预算为每张 8 MiB、每条消息前四张、每记录 48 MiB、每次文件读取 64 MiB。待上传图片总量 128 MiB；最多 128 个任务游标和 128 个会话观察记录；每启用项目查询最多 32 条近期未归档索引记录。队列满时明确报错，不清除未投递材料。Hook 默认处理预算 6 秒，最大 8 秒，命令超时 10 秒；后台不等待 Vision 或 Memory。详情见 [桌面验收指南](desktop-testing.md)。

MiniCPM-V-4.7-1B、MiniCPM-V-4.7-35B-A3B 保留为候选显示名称，公开配置默认为空。用户私有配置决定实际 endpoint、model ID 和部署版本。配置向导在用户选择实验后提供已验证的面壁 35B 候选，显示保存位置、实际 endpoint、宿主范围和静态校验状态；真实识图通过独立的 image probe 显式执行，不自动上传。证据查看通过现有 Scope 授权 API 回读精确历史任务和原图，不开放静态目录，不调用视觉模型。

关闭时不创建图片目录、扫描历史、启动视觉 worker 或要求凭据；仍保留 Definition 和历史读路径。启用缺配置时只报告图片能力不可用，不阻止普通文字服务启动。新摄取必须在接收前能固定有效配置快照，但无需联网探测模型。

只支持独立 SQLite。实验数据库单独记录格式版本 1，不把它与现有 DB schema 版本混用。未知实验格式只禁用图片能力，不静默覆盖、改身份或重写历史 Source；正常文字功能继续。已知阶段二布局通过显式 SQLite 写事务增加任务字段与尝试／操作表，保留所有来源及回执身份，旧任务最多 1024 项；不扫描全库 Source。中途失败回滚后可重启继续迁移。混合或未知布局明确拒绝。

备份包含一致的 SQLite 快照和其资产清单；先暂停图片写入或建立受保护快照，再逐项复制、校验摘要。分别复制运行中的 DB 和资产目录不能宣称一致备份。自动清理仅处理可证明无引用且过宽限的孤立文件；首版五个操作不提供证据删除，手工删除损坏材料不得破坏身份或返回正常成功。

## 阶段一证据与后续验收

生成 DTO 和身份测试覆盖精确字节／中文 roundtrip、严格字段、图片-only 上下文、身份隔离、摘要稳定、操作引用和 Scope 授权声明。阶段二至四另有实际 SQLite、HTTP、loopback CLI、真实子进程终止与 MiniCPM-V 后台识图证据。完整检查以实施记录为准。

模型结构化输出、资产摘要／身份匹配、引用类型与 Scope 一致性、Codex origin 完整性、像素乘积、实体关系和任务状态均由相应服务边界执行语义校验。生成 DTO 只提供字段、类型和长度校验，不能替代它们。

| 阶段 | 必须取得的行为证据 |
| --- | --- |
| 二 | 实际 HTTP + SQLite 接收和原图 read；删原文件仍可读；同键重放／冲突；半写失败；禁用／越权／满预算拒绝 |
| 三 | 真正多模态请求与至少一个真实模型；有限尝试；进程重启；陈旧 claim；新重试／重解析保留旧记录；成功事务与通知 |
| 四 | 纯原图窗口推进；迟到解析被消费；Topic Memory 和证据 upstream；历史 Artifact 回读；同图不重复计权；跨 Scope 授权 |
| 五 | 固定版本真实 CLI 图片触发服务接收；原图摘要相同；Stop、原文件消失及未支持方式结果明确 |
| 六、七 | 默认关闭、引导、原图核查、备份恢复；真实样本质量和开启／关闭配对收益 |

实际检查命令、失败修复、最终结果和未完成验收持续写入 [实施记录](implementation-log.md)。阶段零至六按固定支持范围完成；多端扩展已加入 CLI 三个明确版本、Desktop 内核及用户实际桌面单 PNG 发送的采集验收。真实普通模型自动 Memory、后端检索和原始材料回图已通过。界面输入组合与全新桌面聊天的实际自动召回待验收，1B、TUI、未知版本与其他 Agent 未开放。下一步继续界面组合、桌面召回验收和阶段七的质量与收益评估。

任务查询使用真实 SQLite 读快照。attempt_count 计入所有已领取尝试；provider_requests 为 0、1 或 null（调用边界与中断后未知），未知 token 为 null。严格 JSON 不修复代码围栏、未转义控制字符或额外字段；解析深度等解码错误归一为失败，不停止整个 worker。
