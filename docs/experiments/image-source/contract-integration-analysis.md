---
title: 图片 Source 契约与现有系统接入分析
description: 数据、权限、生成消费、证据回溯和 API 生成边界，阶段二至四接入状态与约束。
---

# 图片 Source 契约与现有系统接入分析

图片证据链可以接入当前架构，但不能仅增加两个 Source schema 和一次模型调用。原图过滤、Topic Memory 投影、解析到原图的证据关系、子进程 Source 注册和迟到结果通知都有明确接入点。

本分析说明基础架构和阶段二至六的接入规则。显式图片摄取与回读见 [阶段二说明](phase2-usage.md)，后台解析、消费与回溯见 [解析与记忆说明](phase34-usage.md)，固定 CLI 捕获、可选引导与证据查看见 [捕获与查看说明](phase56-usage.md)。35B 实际 worker 与 CLI 文件附件链路已验收。最终字段、操作和预算以 [数据与操作契约](contracts.md) 为准。

## 契约声明与运行时激活

严格的数据类型和完整的操作语义先于运行时激活。后续接入图片处理时，不能改变契约确定的身份、权限和成功含义。

实验数据 schema 位于唯一契约源 `openapi/powercontext.yaml` 的 `components.schemas`，通过已有生成器产生传输类型。capture、asset/get、task/get、task/retry 和 reparse 五个操作均位于标准 `paths`，已绑定 Server、Client 和权限。`x-powercontext-experimental-image-source.planned_paths` 为空；active 表示实现完成，不表示图片开关默认开启。图片操作没有加入默认 MCP 工具列表。

实验扩展保存格式和能力声明。方法、唯一 operationId、请求与响应引用、权限、状态码和实验格式版本由契约检查验证；HTTP 路由、图片回读和任务恢复另以实际行为验收，不能只凭生成通过宣称可用。

阶段二和三实现对应操作时，将其完整声明移入标准 `paths`，同步生成 Python 与 JavaScript 契约、绑定 Server 路由、Client 和权限，并执行行为验收。不能同时保留两份可各自修改的操作定义。阶段一没有必要用返回占位值的路由填补覆盖检查。

## Source 模型、身份与持久化

### 可以复用的契约

| 当前事实 | 实现位置 | 对图片功能的约束 |
| --- | --- | --- |
| SourceRef 只有 `source_type` 和 `source_id` | [models.py 第 32 行](../../../src/powercontext/sources/models.py#L32) | Scope 由调用和仓库分区提供；原图引用必须同时明确所属 Scope，且与解析记录相同 |
| Source 带 Definition 版本和 materialization | [models.py 第 60 行](../../../src/powercontext/sources/models.py#L60) | 原图和解析记录均使用 captured；不把宿主临时路径保存为可长期读取的 referenced 材料 |
| Definition 绑定 adapter、版本和可选 projection | [definitions.py 第 58 行](../../../src/powercontext/sources/definitions.py#L58) | 两个正式类型分别拥有 schema/read 契约，只有解析类型提供文字证据能力 |
| Registry 按输入类、Source 类和名称唯一注册 | [definitions.py 第 95 行](../../../src/powercontext/sources/definitions.py#L95) | 不复用 ContentCapture 输入类来假冒图片；Definition 名称和版本要稳定 |
| 原生 Source 读取要求已注册 Definition | [sources.py 第 412 行](../../../src/powercontext/builtin/persistence/sources.py#L412) | 关闭图片开关不能移除 schema 和读取定义，否则历史图片 Source 无法解码 |
| 原生 Source 读取验证声明版本与当前 Definition 一致 | [definitions.py 第 149 行](../../../src/powercontext/sources/definitions.py#L149) | 首版保留版本 1 的解释器；不通过修改版本或默认字段静默重写已有记录 |

首版固定使用 `experimental.image.original` 和 `experimental.image.analysis` 两个内置原生 Source 类型，Definition 版本均为 `1`。它们是实验分支中的普通类型定义，不需要建立可执行插件加载机制。远程 observation 机制仍用于既有外部 Source，宿主传入的图片字节先经过图片摄取服务，而不是直接注册一个客户端拥有写入权的“解析结果类型”。

传输层原图引用使用带 Scope 的包装，内部复用当前 `SourceTypeReference` 的 `source_type/source_id` 字段，不使用历史 `name` 字段，不修改运行时既有 SourceRef。

### Canonical payload 与冲突

当前 SourceRepository 在 [sources.py 第 101 行](../../../src/powercontext/builtin/persistence/sources.py#L101) 使用同一个数据库连接完成身份检查、journal 分配和 Source 写入；在 [第 142 行](../../../src/powercontext/builtin/persistence/sources.py#L142) 拒绝同一身份的不同值。在 [第 445 行](../../../src/powercontext/builtin/persistence/sources.py#L445) 使用 RFC 8785 JSON 编码保存 envelope。

图片服务应复用这条提交路径，同时在操作边界补齐下列规则：

- 相同稳定摄取键及附件位置必须对应同一来源和同一原始字节摘要；不同内容返回冲突，不随机生成另一个 ID 规避冲突。
- 请求重放比较 canonical 接收内容，服务第一次接受的时间、推导尺寸和生成 ID 返回原值。重放时重新赋当前时间，会将本应幂等的提交变成冲突。
- 原图 Source 保存永久资产标识、摘要、MIME、字节数、尺寸及来源。任务状态、重试次数、当前错误等可变信息由任务表负责，不修改不可变 Source。
- 每个解析记录包含精确原图引用、实际解析配置标识和经严格校验的结果。输出不同的重新解析必须使用新的任务代次和解析 Source ID，旧结果及其 Artifact 引用保持可读。
- 分析复用与来源保存分开：相同字节可以复用 Scope 内资产；不同会话的来源记录仍独立保存。即使复用模型结果，也要生成指向本次原图来源的解析记录。

Pydantic 数据验证与仓库相等比较不能替代业务身份规则。未知字段、格式版本、无效原图引用和错误解析结构必须在写入前拒绝；模型输出不能决定 Scope、原图身份、模型来源或任务成功状态。

### Native 与 observation 的安全边界

远程 SourceObservation 保存声明式 payload 和预计算 projection。它在 [observations.py 第 124 行](../../../src/powercontext/sources/observations.py#L124) 有独立 envelope，读取与投影不会加载外部 worker 代码。提交入口会校验 manifest、大小、schema 和文字证据身份：[relational.py 第 845 行](../../../src/powercontext/builtin/runtime/relational.py#L845)、[第 2046 行](../../../src/powercontext/builtin/runtime/relational.py#L2046)。

注册入口拒绝替换当前 active Definition：[relational.py 第 2028 行](../../../src/powercontext/builtin/runtime/relational.py#L2028)。图片的两个内置名称由服务保留，包括功能关闭时。历史 manifest 已占用这些名称时，接收图片前报告明确冲突，不能让原生和 observation 两种表示共用同一持久身份，不能接受一个没有托管资产的伪造原图记录。

## 文件、数据库与队列提交边界

### 接收成功意味着什么

文件系统不共享 SQLite 事务。应先在服务管理的路径耐久保存完整资产并校验摘要，再在一个数据库事务内保存资产索引、原图 Source、pending 任务和幂等响应记录，全部完成后才返回接受成功。

不得先提交 captured Source，再尝试复制原文件。文件保存失败时不能返回成功；数据库提交失败后留下的孤立文件允许有界清理。目录由服务生成，不能使用上传文件名、Scope 字符串或 Source ID 直接拼接不受校验的本地路径。

单次接收只有一张图片时最容易保证完整语义。同一宿主消息中的多图按附件位置分别调用，保存每个结果；不能宣称多张独立请求具有消息级原子提交，也不能静默部分成功。

### 解析任务成功意味着什么

任务的 `succeeded` 表示新的解析 Source 已耐久保存，不能表示 Memory 已生成。视觉解析在数据库事务之外执行；模型响应通过校验后，在一个事务内完成解析 Source 写入、任务成功确认和下游通知。

重启恢复与并发执行需要稳定 task_id、状态转换和 claim 标识。进程中断后可以恢复 pending 和遗留 running，但旧 claim 不能在恢复后覆盖新 claim 的结果。模型响应丢失时允许再次调用，不能承诺供应商请求 exactly-once。

自动退避重试保留同一个任务身份并累计尝试次数。手动重试创建新任务，固定使用被重试失败任务的原图、上下文和解析配置，旧失败任务保持终态。重新解析同样创建新任务，但在接收时选择并固定当前配置。这三种操作的身份和预算不同，不能都简单实现成“设置 pending”。

配置快照保存 provider 协议、endpoint、精确模型 ID、部署版本标识、schema、提示模板、预处理和实际上下文摘要。它不保存密钥和 Authorization header。密钥通过受保护的凭据配置取得；旧快照找不到对应连接配置时报告配置不可用，不改用当前默认模型。

默认并发、尝试上限、每次执行超时和排队上限是初始保护预算，阶段一无需声称已测得最佳值。应避免 SDK 重试、worker 重试和结构化修复形成未计入总预算的乘法放大；尝试次数要包含实际发出的 provider 请求，未知用量保持未知。

### 通知与迟到结果

SourceRepository 的 [sources.py 第 168 行](../../../src/powercontext/builtin/persistence/sources.py#L168) 会在新 Source 写入时为 Memory、Topic Memory、Experience 和 Profile 标记 dirty；幂等重放不会重复标记。

此外，统一 SourceStore 写入路径还会提升 Topic Memory 的 pending source 水位：[relational.py 第 1533 行](../../../src/powercontext/builtin/runtime/relational.py#L1533)。已有跨仓库事务示例同时调用 `add_with_status` 和 `processing_pending.raise_source`：[relational.py 第 1309 行](../../../src/powercontext/builtin/runtime/relational.py#L1309)。

因此，图片解析完成时若为保持任务同事务而直接调用 SourceRepository，应在新插入时补齐对应 pending 通知。解析结果使用完成时的新 journal position，不能修改原图的旧 position，也不倒退普通文字游标。

原图仍保留 journal 来源，生成消费者过滤原图后推进窗口到原有 through。失败任务没有解析 Source，不能在文字流程中留下等待模型的占位项。后来的成功解析追加新位置，确保已处理过原图位置的消费者仍有机会发现它。

## 生成准入与各消费者

### 原图必须显式过滤

共同准入规则在 [source_eligibility.py](../../../src/powercontext/builtin/source_eligibility.py) 排除原图和 unreadable 解析，放行具备固定文字投影的 readable／partial 解析，并保留 ContentSource 的 internal 规则。缺少文字投影本身不会自动阻止任意新类型进入生成流程，图片用途需要显式判断。

[GenerationSourceAccess](../../../src/powercontext/builtin/persistence/generation_sources.py#L34) 对显式引用拒绝整个不合格请求，对 journal 窗口过滤不合格项。阶段二已将原图设为仅可溯源：显式要求它参与文字生成时返回 `source_not_eligible`，自动窗口跳过它；正式原图读取接口仍然允许有权限的读取。阶段四已放行有有效文字投影的可读解析，Unreadable 保留审计。

不要把任意客户端 `metadata` 中的“仅来源”标志当作权威规则。原图用途由受管理的类型和服务规则确定；解析结果只有在 schema 和关联校验成功后才成为可消费证据。

### 兼容清单

| Consumer | 当前行为与代码 | 必要改动或验收 |
| --- | --- | --- |
| Memory | [composition.py 第 221 行](../../../src/powercontext/builtin/runtime/composition.py#L221) 优先使用 Definition 的文字投影；[relational.py 第 1824 行](../../../src/powercontext/builtin/runtime/relational.py#L1824) 通过 GenerationSourceAccess 读窗口 | 解析 Source 注册固定投影，原图被过滤；没有可消费 Source 的窗口也要正常推进 |
| Topic Memory | [topic_memory_processing.py](../../../src/powercontext/builtin/runtime/topic_memory_processing.py) 对图片解析的原生分支使用共同文字投影 | 与 Memory、Handoff 的文字内容一致，保留旧类型读法与失败语义；完成解析时提升 pending 水位，消费迟到结果 |
| Handoff | [composition.py 第 234 行](../../../src/powercontext/builtin/runtime/composition.py#L234) 优先使用同一文字投影；SourceCatalog 的生成读取在 [relational.py 第 1492 行](../../../src/powercontext/builtin/runtime/relational.py#L1492) 执行准入 | 直接传原图不能触发视觉推理；有效解析参与交接，来源引用仍精确 |
| Dream / Experience / Skill / Review | [resolver.py 第 344 行](../../../src/powercontext/builtin/evidence/resolver.py#L344) 读 Source 并使用文字投影；Dream 通过 [service.py 第 301 行](../../../src/powercontext/builtin/dream/service.py#L301) 调同一 resolver | 增加解析到原图的来源关系、同链归组、权限和不可变引用；不能以 metadata 字段替代证据图 |
| Experience 自动窗口 | [relational.py 第 1921 行](../../../src/powercontext/builtin/runtime/relational.py#L1921) 过滤 eligible rows | 检查纯原图窗口推进及解析迟到后的新位置消费，不能因为原图类型未知而阻塞 |
| Profile | Source 写入时也会标记该 binding dirty | 与其他可消费窗口一并检查原图排除；不因图片存在就自动确认用户偏好或长期事实 |

固定的 `powercontext.text-evidence` 形状在 [projections.py 第 21 行](../../../src/powercontext/sources/projections.py#L21) 声明。解析投影中的 `source_type/source_id` 指向解析记录，不把原图身份冒充解析身份。content 要区分可见文字、视觉描述、解释和不确定项，metadata 仅保存有界来源说明；原图字节、临时路径和凭据不进入下游文字模型。

同一精确解析记录每次投影返回相同值。适配器 read 和 projection 都只读已经保存的数据，不调用模型，不依赖当前 Vision 配置。只有摄取与任务执行受开关控制；功能关闭后，既有文字解析证据可以按普通历史 Source 继续读取和消费。

## 原图回溯与证据独立性

EvidenceResolver 在受管理解析类型上返回精确原图引用作为 upstream，原图作为 `lineage_only` 节点，保留摘要和身份，不进入模型文字投影。其他 Source 保持既有来源规则，不遍历任意 metadata。沿途逐个执行权限检查，不能为图片绕过授权。

图片解析的 `root_identity` 由服务读取原图记录后按 Scope 和原始资产摘要确定。同一原始证据的多个解析结果使用同一 group 标识；不能把模型输出或客户端 metadata 中的“独立性”当成证明。

归组和模型准入是两个不同职责。可以让解析记录继续承担可消费 root、原图只保存 upstream 来源，用共同根身份消除重复独立计权。若改成其他节点角色，应同步验证 [root_ids 第 479 行](../../../src/powercontext/builtin/evidence/resolver.py#L479) 的遍历和 pinned manifest 验证，不能让正常图片证据突然失去根来源。

按同一 Scope 和资产摘要保守归组可以避免“重复上传同一图”产生多份独立佐证；每次原图来源和每个解析 Source 仍分别保留。换模型重新解释图片不构成新的原始事实。

## 内置子进程与关闭后的可读性

内置 Artifact worker 固定使用 BUILTIN_SOURCE_REGISTRY：[family_processing.py 第 90 行](../../../src/powercontext/builtin/runtime/family_processing.py#L90)。组合层会拒绝普通子进程不能重建的自定义 Source registry：[composition.py 第 671 行](../../../src/powercontext/builtin/runtime/composition.py#L671)，对应行为测试在 [test_processing_source_registry.py](../../../tests/builtin/runtime/test_processing_source_registry.py)。

阶段二接入原生类型时，要把无模型调用的两个 Definition 注册到同一内置 registry，让 API、普通 Artifact worker 和实际 SDK 读路径都能够重建它们。不能仅在主 Server 注入额外 registry，也不能为了让实验启动而删除该配置校验。

图片 worker 使用独立任务表和 SQLite owner 租约保证一个有效解析 worker，不虚构 Artifact family。开关关闭时不接收新图片、不开始新视觉调用、不扫描宿主附件；已接受的任务保持可恢复的状态，已保存的原图与解析仍能通过正常授权读路径回读。

不支持的新实验数据格式在接收前拒绝。发现既有实验库格式不兼容时只禁用图片能力并报告具体版本问题，避免使正常文字服务不可用。没有明确迁移和回读保证前，不宣称其他数据库后端支持图片。

## Scope 权限

当前访问资源只有 Server、Scope 和 Artifact：[authz/models.py 第 53 行](../../../src/powercontext/server/authz/models.py#L53)。Source 属于 Scope，Dream 对 Source 的读取也是 Scope 权限：[dream_access.py 第 54 行](../../../src/powercontext/server/dream_access.py#L54)。图片实验不需要增加一个可独立共享的 Asset AccessProfile。

| 图片操作 | 权限 | 额外约束 |
| --- | --- | --- |
| 接收原图 | scope.contribute | 校验已存在并允许使用的 Scope；禁止上传字段或图片内容推导权限 |
| 读原图资产 | scope.read | 查询同时按 Scope 与 asset_id；不设无鉴权静态目录或仅凭摘要即可读的 URL |
| 查询任务与解析状态 | scope.read | 任务与原图都在同一 Scope；错误和 snapshot 去掉密钥与请求正文 |
| 重试失败任务 | scope.read 与 scope.contribute | 状态校验、稳定幂等键和预算；不能改变原图或旧 snapshot |
| 重新解析 | scope.read 与 scope.contribute | 创建新代次；使用显式选择的有效解析配置，不覆盖旧证据 |

五个操作均采用 POST 和 current Scope 模式，`scope_id` 在请求 body 中提供。固定操作与路径分别为：

| operationId | 路径 |
| --- | --- |
| experimental_capture_image | /v1/experimental/images/capture |
| experimental_get_image_asset | /v1/experimental/images/asset/get |
| experimental_get_image_task | /v1/experimental/images/task/get |
| experimental_retry_image_task | /v1/experimental/images/task/retry |
| experimental_reparse_image | /v1/experimental/images/reparse |

读取某个共享 Artifact 的权限不意味着能读其整个 Scope 中的图片。原图核查需要自己的 Scope read；跨来源 Scope 引用在首版拒绝。管理角色也不隐含内容权限，现有 [ROLE_ACTIONS 第 361 行](../../../src/powercontext/server/authz/models.py#L361) 把 scope.admin 与 scope.read 分开。

新增操作移入标准 paths 后，沿用 `x-powercontext-access` 和 `_add_route` 的授权 dependency：[app.py 第 4335 行](../../../src/powercontext/server/app.py#L4335)、[第 4443 行](../../../src/powercontext/server/app.py#L4443)。访问控制不可用必须 fail closed，并声明相应 503；不能因功能是本地实验就绕过现有访问模式。

## API 生成与传输选择

### 为什么阶段一不激活路由

生成器在 [generate_api.py 第 224 行](../../../scripts/generate_api.py#L224) 遍历标准 paths 并生成 Operation；它不会绑定实际 Server endpoint。Server 通过 [app.py 第 1485 行](../../../src/powercontext/server/app.py#L1485) 直接发布生成的 canonical OpenAPI，只针对既有可选 Handoff Report 路径做开关过滤。

所以，把尚未实现的图片路径加入标准 paths，至少会让 `/openapi.json` 和 API 参考宣称存在实际会返回 404 的操作。即使生成与 schema equality 检查通过，也不是可用性保证。

MCP 使用 [mcp.py 第 223 行](../../../src/powercontext/server/mcp.py#L223) 的显式 operationId 白名单；单纯增加标准 OpenAPI path 不会自动增加 MCP 工具。但一旦加入白名单，OpenAPIProvider 会根据 canonical schema 生成该工具，它可能调用一个不存在的路径。默认 MCP 工具集合受 [test_mcp.py 第 73 行](../../../tests/test_mcp.py#L73) 的精确行为断言保护，不能删掉这项断言来提前暴露图片操作。

现有 [test_api_contract.py 第 973 行](../../../tests/test_api_contract.py#L973) 检查 Server 发布 canonical schema；它不能替代每个新路由的执行测试。阶段一保留既有检查，阶段二增加实际 HTTP 接收、回读和禁用/越权场景，不能把待实现路径加入跳过集合后宣称完整绑定通过。

### JSON Base64 适合首版有界传输

现有生成器只接受引用 component 的 application/json 请求和成功响应：[generate_api.py 第 417 行](../../../scripts/generate_api.py#L417)、[第 469 行](../../../scripts/generate_api.py#L469)。Client 也按 JSON 序列化和校验响应：[client.py 第 1232 行](../../../src/powercontext/client/client.py#L1232)。

仓库已有精确包字节的有界 Base64 下载与上传先例：[OpenAPI 第 2386 行](../../../openapi/powercontext.yaml#L2386)、[app.py 第 3391 行](../../../src/powercontext/server/app.py#L3391)、[第 3410 行](../../../src/powercontext/server/app.py#L3410)。图片首版可使用同样的 JSON 字节传输边界：

- 接收请求用单张 `bytes_base64` 和声明 MIME，服务严格 Base64 解码后校验真实图片格式、大小和像素。
- 资产回读返回精确原始字节的 `bytes_base64`、摘要、MIME 和尺寸；Client 解码并验证摘要，需要时写为文件或显示。
- Base64 仅是传输格式，不保存进 Source payload 或文字投影。Source 保存资产引用，模型调用读取受管理资产构造真正的图片内容。
- 限制编码字符串长度和解码字节数；基于字节上限计算 Base64 上限 `4 * ceil(n / 3)`。还要有整段 HTTP body 的有限预算，避免完成 JSON 解析后才发现大请求。
- 不能依赖正则证明 Base64 可解码，也不能依据 MIME 或文件扩展名证明图片有效。Base64 错误、损坏图片和像素超限在耐久接收前拒绝。

二进制或 multipart 可以在后续明确需求下加入专用传输适配，但必须正规声明契约并支持相应 Client 与生成行为。隐藏一个实际业务下载路径来绕过主 OpenAPI，不符合当前 API 来源规则。本实验阶段一无需为原生 binary 下载扩大通用生成器范围，也不承诺标准图片 URL 已实现。

## 必要影响清单与验收边界

| 阶段 | 需要修改或补齐的边界 | 必须保护的外部行为 |
| --- | --- | --- |
| 一 | 唯一 OpenAPI 中的实验 component、未激活操作声明、生成模型、契约说明 | 严格数据校验、引用与权限一致；默认可调用路由和 MCP 工具没有未实现图片项 |
| 二 | 图片接收服务、SQLite 资产与任务索引、托管目录、原生 Definitions、Client、HTTP 和 CLI | 接收后精确原图可读；重复请求返回同一结果；冲突、超限、损坏及越权请求不产生被接受记录 |
| 三 | 独立 Vision 适配、配置 snapshot、解析 schema、worker 与状态转换、同事务成功提交 | 真图片输入；失败原图保留；重启恢复；陈旧 claim 不写入；重试和重新解析身份明确 |
| 四 | 准入规则、Topic Memory 图片消费、证据关系与归组、pending 水位通知 | 原图不进入文字生成且不阻塞游标；迟到解析被消费；旧 Artifact 可回读精确解析与原图；不重复独立计权 |
| 五 | 默认关闭的固定 CLI 附件 Hook、Scope 绑定、增量定位及回执 | 只接收当前明确轮次的原文件；重放保留身份；失败不阻塞宿主或伪造接收；真实 CLI 与服务联调 |
| 六 | 可选配置模块、显式合成图探测、Memory／Artifact 材料入口、原图资产响应 | 无隐式模型请求；历史引用打开精确解读；接收、解析与 Memory 分开显示；Scope 授权与 UI 点击链路完整 |

阶段一相关检查可以执行 `make api-generate`、`make contract-test`、实验 DTO 和操作声明的 focused tests。MCP 精确工具集合、Source 持久化与 generation admission 的现有测试也应保留。不能为本阶段尚未实现的文件事务、模型输出、重启和宿主能力填“通过”。

后续新行为测试应通过真实 HTTP、持久化和公开 Source/Artifact 接口检查结果，避免锁定 adapter 私有调用次数或模块位置。文件中断和进程恢复测试验证真实本地存储行为；mock provider 与直接 hook 输入分别只能证明本地错误处理和适配逻辑。

图片 HTTP、SQLite、CLI 的阶段二验收与代码审查结果见 [实施记录](implementation-log.md)。本文件中的代码接入分析不替代运行证据，也不构成模型、视觉 worker 或宿主自动捕获验收。
