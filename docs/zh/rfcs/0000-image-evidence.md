---
title: 图片证据与 Artifact 集成
---

- 提案名称：`image_evidence_artifact_integration`
- 起始日期：2026-10-13
- RFC PR：[oceanbase/powercontext#0000](https://github.com/oceanbase/powercontext/pull/0000)
- Tracking Issue：[#1932](https://github.com/oceanbase/powercontext/issues/1932)
- 实验实现：[草稿 PR #1956](https://github.com/oceanbase/powercontext/pull/1956)
- 相关 RFC：[Source Definition](1400_source_definition_and_observation_model.md)、
  [Source 访问](1458_artifact_generation_source_access.md)、[Artifact Family](1549_artifact_family_unification.md)、
  [处理 Supervisor](1515_artifact_processing_supervisor.md)、[数据库迁移](1771-unified-database-migrations.md)、
  [Atomic Memory](1809-atomic-memory.md)、[Artifact 检索](1858-artifact-search.md)。

# 摘要

在正常的 PowerContext 使用流程中加入可选图片证据采集。提交的 PNG/JPEG 字节通过受管理资产保存，并由属于 Scope
的不可变 Source 描述。后台识图后，将通过校验的解释保存为新的 `image-analysis` Artifact Family，复用既有版本、
lineage、权限和读取契约。每个成功的解释版本发布一个确定性的文字证据 Source，使现有 Source 驱动的 Atomic Memory
流程能够消费。调度复用 Artifact Processing Supervisor；服务端图片域存储提出三表方案，替代实验实现的七张表。

本 RFC 提出架构和验收要求，不发布实验 API，也不代表实现已获批准。本地模型自动下载和运行属于后续能力。

# 动机

Agent 对话中包含截图、流程图和图片需求。文字采集会遗漏这些内容；Agent 偶然生成的总结也不能保留精确原材料。
同时保存图片和经过校验的解释，可以让新会话检索有用事实，并让人核查原始证据。

功能涉及存储、后台执行、宿主集成和公共 API，需要先说明如何接入既有 Source/Artifact 框架，以及新增存储为什么必要。
[维护者反馈](https://github.com/oceanbase/powercontext/pull/1956#issuecomment-6140580696)明确要求评估这些边界，并逐表说明必要性。

# 使用说明

## 正常使用

图片能力默认关闭。用户在正常配置流程中可以开启图片解释，并明确选择已经验证多模态能力的普通生成连接，或独立视觉
endpoint。独立 endpoint 可以是在线服务，也可以是用户已经运行的本地服务。向导说明哪个服务会接收图片和附带文本。
Agent 自身使用的模型不自动成为 PowerContext 可用的推理连接。

用户照常启动 PowerContext。标准宿主 setup 管理采集配置和采集器生命周期。首批自动采集目标是经过实测的 Codex
Desktop/CLI 版本；显式 CLI/SDK 上传提供独立入口。宿主要求的插件启用和 Hook 信任仍是首次使用步骤。正常使用不需要
维护另一份图片环境文件，也不需要手动启动采集器。

以一张包含有效部署限制、划掉的旧值和未确认假设的操作手册截图为例：

1. 获授权的采集保存宿主实际提交的图片表示，以及会话、消息来源。
2. 耐久接收后，Dashboard 显示已保存图片和处理状态。
3. 视觉模型给出可见文字、图形关系和不确定项。解释是机器阅读结果，不是已经批准的决策；划掉内容和未确认陈述保留限定。
4. 现有 Memory 流程按自身策略生成零条、一条或多条 Atomic Memory。
5. 后续会话可以召回相关 Memory；证据链接指向精确解释版本，再在权限允许时查看原始提交图片。

接收、解释、Memory 发布和新会话实际召回是不同结果。Dashboard 分别展示；解释成功不表示一定生成 Memory。
视觉能力可以正常工作，而普通生成模型或 Memory 自动处理仍未配置。

## 用户控制

采集授权持久保存，并对应具体项目绑定、Scope 和 Server 目的地。Server 开启功能不代表允许读取宿主历史对话。
关闭采集停止新增收集，已有证据仍可读取；重新开启不导入关闭期间的消息。

重试沿用失败请求固定的解释配置。重新解析明确选择新的配置，并增加新的解释版本。两者均不修改已经引用的历史证据。
配置变化影响新请求，不静默替换已接收工作的后端。本地请求不会自动回退到在线服务。

## 首期范围

先验收静态 PNG/JPEG 和文件 SQLite 部署。其他已支持数据库的文字操作照常使用；图片接收在保存字节或创建 Source
之前明确拒绝尚未支持的存储。每个请求处理一张图片和有界附带文本。自动采集仅支持通过真实宿主进程实测的版本和附件形式。

视频、音频、PDF、图片 embedding、多图联合推理、任意模型托管和新的 Memory 审批流程不在本 RFC 范围内。
本地权重下载和受管理推理进程属于后续工作。

# 参考级说明

## 当前契约与所需扩展

核对的代码基线为 master
[`8970a970`](https://github.com/oceanbase/powercontext/tree/8970a970c50be930fdea0e627f5299e41dcbc5ad)；实验实现为
[`21df99c8`](https://github.com/knqiufan/powercontext/tree/21df99c841b154975e838fff6618a91c8c191350)。RFC 中描述的目标能力
不等于这份基线已经实现了该能力。

| 职责 | 既有契约 | 本提案扩展 |
| --- | --- | --- |
| 精确采集证据 | Source Definition registry、`pc_sources`、Scope journal、相同写入重放及冲突 | 原图类型与资产描述，不在 Source JSON 中内嵌字节 |
| 解释内容和历史 | Artifact 内容、head 和精确 `ArtifactRef` 版本 | 注册 `image-analysis` Family，由图片域发布 |
| 来源链 | Source/Artifact lineage 和发布记录 | 原图 Source → 精确解释版本 → 文字发布 → Memory |
| Memory 模型输入 | 仅从 Source 抽取、命名文字证据 | 确定性解释发布 Definition；原图不进入文字抽取 |
| 调度 | Scope 调用意图、各 Family 独立预算、任期及 fencing | 图片解释 binding 和 worker；逐图片进度属于图片域 |
| 查看 | 通用 Source/Artifact 读取、Dashboard 证据遍历 | 图片与解释渲染，以及有权限的资产读取 |
| 推理 | 配置的生成角色与用量记录 | 明确视觉角色、真正多模态输入、输出校验及请求预算 |

当前 `SourceRef` 为显式 Scope 内的 `(source_type, source_id)`；原生采集拒绝同身份的冲突 payload。本提案采用这个已经
实现的契约，不以完整实现 RFC 1400 的 observation 身份模型为前置条件。图片处理关闭时仍保留 Definition 版本和原始身份。

主要源码依据：

- [Source 模型](https://github.com/oceanbase/powercontext/blob/8970a970c50be930fdea0e627f5299e41dcbc5ad/src/powercontext/sources/models.py)
  与 [repository](https://github.com/oceanbase/powercontext/blob/8970a970c50be930fdea0e627f5299e41dcbc5ad/src/powercontext/builtin/persistence/sources.py)。
- [Artifact 模型](https://github.com/oceanbase/powercontext/blob/8970a970c50be930fdea0e627f5299e41dcbc5ad/src/powercontext/artifacts/models.py)
  与 [共享表](https://github.com/oceanbase/powercontext/blob/8970a970c50be930fdea0e627f5299e41dcbc5ad/src/powercontext/builtin/persistence/tables.py)。
- [处理意图](https://github.com/oceanbase/powercontext/blob/8970a970c50be930fdea0e627f5299e41dcbc5ad/src/powercontext/builtin/persistence/processing_intents.py)、
  [Supervisor](https://github.com/oceanbase/powercontext/blob/8970a970c50be930fdea0e627f5299e41dcbc5ad/src/powercontext/builtin/runtime/artifact_processing.py)
  和 [Atomic 抽取](https://github.com/oceanbase/powercontext/blob/8970a970c50be930fdea0e627f5299e41dcbc5ad/src/powercontext/builtin/artifacts/atomic_memory/extraction.py)。

## Artifact 边界的方案比较

| 方案 | 收益 | 成本与限制 |
| --- | --- | --- |
| 原图与解释均保留为 Source，沿用实验结构 | 消费侧改动较少；保留采集与派生证据 | 解释历史、任务查看和 lineage 需要图片专用约定；Artifact 内容与版本仅在下游使用 |
| 原图与解释均成为 Artifact | 全链路复用版本和目录 | 把采集附件当作生成内容，仍需二进制资产边界，并需为当前 Memory 抽取提供 Source 输入 |
| 原图 Source + 图片解释 Artifact + 文字发布 | 派生内容复用 Artifact 历史和 lineage，同时保留采集与 Source-only Memory 契约 | 需要新 Family、确定性发布适配器，以及原子发布和防循环规则 |

推荐第三种方案。图片解释 Artifact 不等于 Atomic Memory：它描述可见材料，包括不确定、已失效或不值得记住的内容。
复用 Memory、Profile 或其他无关 Family 会混入不同领域规则。新增 Family 复用的是存储和基础设施，不是另一类制品的含义。

图片域拥有格式验证、二进制资产耐久性、视觉配置、有界模型请求和解释发布。传输与宿主适配器转换提交，不建立另一套权限、
Source 身份、Memory 治理或调度系统。

## 精确证据与发布

```text
获授权附件／显式上传
  → 受管理资产 + 原图 Source + 耐久处理请求
  → 既有 Supervisor → 图片解释 worker
  → image-analysis Artifact 版本 + 解释发布 Source
  → 既有 Source 驱动的 Atomic Memory 抽取与协调

Memory → 发布 Source → 精确解释 Artifact 版本 → 原图 Source → 资产
```

原图 Definition 拟命名为 `image.original`。类型化 payload 保存带 Scope 的资产描述、稳定 capture key 和附件位置、
宿主及消息来源、实际提交表示、有界上下文快照及获授权的上下文 Source 引用。字节身份与事件身份不同：相同字节可共用
同 Scope 的资产，不同提交仍保存各自 Source。

一个 `image-analysis` Artifact 对应一个原图 Source。版本内容保存精确原图、schema/prompt 版本、后端和模型部署身份、
固定输入及上下文摘要、通过校验的输出。输出区分可读程度、可见文字、视觉描述、实体、关系和不确定项。配置与运营数据
不保存密钥；图片或附带文字中的指令不能修改执行策略。解释产生时间不是事实生效时间。

首次成功创建版本 1。重新解析沿用既有 head 乐观并发前置条件追加版本；一个原图同时最多接收一个非终态请求。
图片解释写入由图片域负责，首期不开放通用内容替换。head 已变化或 fence 失效时整次发布失败，不能覆盖较新解释，
也不能只发布文字 Source 而缺少对应 Artifact 版本。

每个提交版本发布一个 `image.interpretation` Source，包含带 Scope 的精确 `ArtifactRef`、原图 Source 引用、已校验文字
快照、投影版本和内容摘要。快照是派生视图，解释 Artifact 才是其权威；校验拒绝不一致的快照。稳定身份由带 Scope 的
Artifact ID 和版本派生。读取与投影不调用视觉模型，也不跟随可移动的 head。

Artifact 版本、lineage、发布 Source、请求结果和 Memory dirty 通知一同提交。重复完成返回相同版本与 Source。
上下文 Source 帮助解释，不因此成为图片陈述的证明。解释 lineage 引用原图和上下文 Source，不引用自身的发布 Source，
避免形成循环。Memory 证据包含发布 Source 和精确解释版本；逐节点核对权限，不能替换为最新版本。

原图 Source 明确不进入文字生成输入。解释发布后进入后续 Source window，即使 Memory 已经推进过原图的 journal
位置也不会遗漏。Memory 现有准入、写策略、协调和零候选行为继续生效。重新解析时间不能让旧事实变新；同图的多个解释
不能当作独立佐证或重复召回权重。
发布的类型化元数据保留已知的原始记录时间和原始 journal 位置。新的 journal 位置用于唤醒消费者，不作为图片事实的
回退时间。缺少明确事实日期和记录日期时，协调仍保留原证据顺序；解释晚完成不能因此压过较新的文字事实。

原图和解释发布 Source 由图片域写入。通用 Source/observation 提交不能伪造 retained 资产描述、接收回执或解释发布。
采集／发布边界在提交前校验资产的精确 Scope 和状态，以及被引用 Artifact 的精确版本和摘要。

## 存储对照与三表方案

实验实现新增七张服务端表。职责映射如下；本对照不把实验物理结构当作公共契约。

| 实验表 | 职责 | 拟处理方式与理由 |
| --- | --- | --- |
| `pc_experimental_image_state` | 格式版本、资产与队列总数、worker owner/lease、公平轮转位置 | 格式进入正常迁移；worker 任期与公平性交给 Supervisor；容量进入 capacity 表 |
| `pc_experimental_image_scope_budgets` | Scope 待处理数量 | 全局与 Scope 的图片准入预算合并进入 capacity 表 |
| `pc_experimental_image_assets` | Scope 字节身份、预留与已保存资产描述 | 保留资产表，但字节在事务中保存，不再需要文件预留／孤立状态 |
| `pc_experimental_image_captures` | 耐久采集摘要与重放回执 | 合并为类型化处理请求回执；Source 本身不能重放固定任务和响应 |
| `pc_experimental_image_tasks` | 固定输入、状态、发布结果 | 处理请求表；领域进度不同于合并的 Scope 调用意图 |
| `pc_experimental_image_attempts` | 有界模型尝试与结果 | 放入每个请求的有界尝试列表；拟定最多五次尝试，不单独建表 |
| `pc_experimental_image_operations` | 重试／重新解析键到请求的映射 | 同一请求表中的类型化键和唯一约束 |

以下物理名称用于说明方案，仍需正常迁移评审：

1. **`pc_image_assets`**：以 `(scope_id, byte_digest)` 标识资产，保存有界原始字节、媒体类型、尺寸、大小和创建信息。
   复用关系数据库的二进制编码和精确摘要校验。Source/Artifact JSON 保持较小；独立资产身份让多个采集 Source 共用
   同一个字节对象，不重复保存字节。
2. **`pc_image_processing_requests`**：保存类型化 capture/retry/reparse 身份与请求摘要、原图 Source、固定配置和
   上下文、接收回执、状态、尝试预算、有界尝试、claim token 和精确发布结果。唯一键保证重放和每原图最多一个活跃请求；
   due/state/Scope 索引支持有界发现。既有 processing intent 刻意不保存逐图片 payload、队列状态或重试历史，塞入这些
   字段会改变 Supervisor 契约。这些行是领域命令记录，不是另一套通用 Job 系统。
3. **`pc_image_capacity`**：明确区分全局与 Scope 预算行，保存 retained 字节数和活跃请求计数。接收在事务中锁定并
   更新适用计数，与资产和请求插入一同提交。Scope settings 保存配置，不是事务性容量账本。每次上传重算全部
   资产会扫描不断增长的集合，也不能安全替代并发容量预留。

原图和解释 Source、Artifact 版本与 head、lineage、处理任期复用既有表。新的 capacity 表不保存 worker owner 或
schema 版本。尝试记录是有界运营数据，不是 Artifact 版本；不包含密钥、原始 prompt、Base64 或无界 provider 错误。

也核对了已有二进制存储：
[SkillPackageRepository](https://github.com/oceanbase/powercontext/blob/8970a970c50be930fdea0e627f5299e41dcbc5ad/src/powercontext/builtin/persistence/skill_packages.py)
已经保存并验证 Scope 内的不可变归档字节。但 canonical ZIP／目录树身份、必需的 `SKILL.md`、manifest 和 4 MiB 解压
大小限制属于 Skill 契约，不是图片资产契约。复用二进制编码及摘要模式，不把截图伪装成 Skill 或塞入 `pc_skill_packages`。
多个所有者确实具有相同耐久和失败语义时，后续可以抽取通用资产基础层。

首期有界 SQLite 部署推荐把图片字节保存到数据库。资产、Source、请求和容量在同一 DB 事务中提交，消除实验的文件发布
间隙、孤立预留和 DB／文件目录配套恢复要求。BLOB 写入会增加数据库和 WAL 大小，也可能延长 SQLite 写锁占用；需要测量
准入／读取延迟及增长，不能宣称一定更快。外部文件或对象存储作为备选，只有测量收益能够支持其恢复复杂度时再选择。

首期拟定：每图 8 MiB、JSON 请求 12 MiB、解码后 2,000 万像素、每消息四个附件、retained 资产 payload 合计
1 GiB、每 Scope 256 个活跃请求、全局 1,024 个。准入／解码并发单独有界；视觉并发初始为一，自动尝试默认三次、硬上限五次。输出在发布前
限制为 64 KiB 并严格校验。这些是保护默认值，不是已经测量的性能结论；运营者可以调低。

准入采用固定锁顺序和数据库约束，相同字节不重复计费。解码和模型工作在事务之外，有界 BLOB 插入属于接收事务。
启动恢复可在开放准入前，依据资产和请求事实分批修复派生计数，不生成新的 Source/Artifact 身份。日常队列和
列表使用索引与 keyset 分页，不扫描全部 Source。扩展性结论需先测量计数锁竞争、行与文件增长、恢复成本。
修复或恢复可能分批访问所有资产和请求：分页有界不等于总工作量有界，整体仍为 O(N)。该成本和准入就绪门控需要与日常
增量上传分别测量。

宿主本地 outbox 与这三张 Server 表分开。其耐久 jobs/attachments/watches 解决临时文件存活和客户端增量采集。
既有宿主 checkpoint 契约能保持精确输入时可复用；远端 Server 不能替代客户端暂存队列。客户端存储需要单独说明和限制。

## 接收事务与恢复

| 边界 | 必须保存的事实 | 恢复方式 |
| --- | --- | --- |
| 准入之前 | 已校验权限、实际格式、预算和支持的存储 | 拒绝时没有已接受 Source 或请求 |
| 接收响应 | 在一个事务中提交资产字节、容量、原图 Source/journal 位置、固定请求及回执、dirty intent | 回滚不留下半接收资产；响应丢失重放原回执，不重新选择配置 |
| 模型调用开始 | 调用网络之前提交 fenced claim 和尝试计数 | 崩溃或未知结果仍消耗一次尝试，没有免费隐式重试 |
| 解释成功 | 原子提交版本、精确文字发布、结果及 dirty intent | 重放返回相同证据；旧 claim 无法发布 |
| 解释失败 | 保留稳定失败和实际尝试次数 | 显式重试或有界自动策略；原图仍能查看 |

请求状态为 `pending`、`running`、`retry_wait`、`blocked`，以及终态 `succeeded`、`failed`、`superseded`。
活跃预算计入前四种状态；claim／尝试更新推进请求状态版本。retry 针对失败请求。显式 reparse 只有提供活跃请求的精确
请求／状态前置条件，才能替换它，否则返回冲突。旧请求进入 superseded、旧 claim 失效、新请求接收一同提交。
旧响应晚到不能发布，已经开始的模型调用仍然计数。这样可以替换不可用的固定配置，不静默修改已接收工作，也不让图片永久锁住。

capture key 由 Scope 和附件位置限定。retry/reparse key 由操作类型及原图／父请求身份限定。规范请求摘要包含字节、
provenance 和固定上下文；同键不同请求返回冲突。重试保持失败配置和上下文，重新解析在接收时固定当前选定配置，并检查
调用方的预期 Artifact 版本及配置摘要。操作重放沿用首次回执，即使之后配置已经改变。

图片 binding 使用现有 Scope single-flight 调用和 Family worker 预算。有界 pending provider 发现到期图片请求并标记
领域 dirty；请求接收也在事务中标记。调用 generation 是唤醒计数，不是逐图片任务 ID。处理器保留未到重试时间的请求，
并保留或重建唤醒，不能把未完成的领域工作确认成已完成。

提交时重新检查任期 fence 和请求 claim。不增加图片全局持久 worker lease 或第二个 Supervisor。worker 可以只依据
已经验证的视觉连接构建，不要求普通文字生成。关闭采集保留暂存；关闭解释阻止新模型调用，不影响历史读取。权限失效会
阻止发布或回读，不把工作转投其他 Scope 或目的地。一个关闭的项目不能占满有资格投递的客户端页面。

保证的是持久写入幂等，不是外部推理恰好执行一次。provider 可能已经处理，却没有返回响应。实际请求计费、有界重试和
精确提交证据才是承诺。
SDK/provider 自动重试需要关闭或计入同一尝试账本；调用模型修复格式同样消耗一次尝试。
耐久接收要求文件数据库以及验收过的 journal／synchronous 设置。较弱或不支持的部署拒绝图片准入，普通文字保持原有契约。

## 公共操作与查看

复用并扩展现有类型化目录，不在任务读取中再次返回整份图片内容：

| 公共需求 | 拟使用入口 |
| --- | --- |
| 提交字节并固定首次解释 | 新图片接收命令，返回耐久原图 Source 和处理请求回执 |
| 读取精确字节／缩略图 | 带权限的资产读取；缩略图是派生表示，不能冒充原图 |
| 查看／列出运行进度 | 有界分页的图片处理请求读取与列表；结果为精确 Artifact 引用 |
| 显式重试／重新解析 | 一个解释请求命令，区分操作类型、幂等键和所需前置条件 |
| 原材料列表／读取 | 既有 `list_sources` / `get_source`，支持图片 Definition |
| 解释列表／读取／历史 | 既有 `list_artifacts`、`get_artifact`、`get_artifact_revision`、`list_artifact_revisions`，扩展 Family |
| 健康与调度 | 在既有处理／配置诊断中增加图片 binding 就绪状态和积压 |
| Memory 证据 | 扩展既有 Dashboard 证据渲染与遍历，支持原图和解释节点 |

图片专用操作共五个：capture、asset read、request read、request list、analysis request。具体名字和路由在实现的
OpenAPI 契约中确定，不直接推广实验的七操作接口。新 Family 需要显式 runtime/HTTP schema、reader/processor 注册，
以及 SDK/JS binding 生成。通用操作是可复用契约，不表示未知 Family 已经可以直接使用。

接收要求 Scope contribute，读取遵守相应内容权限。重试和重新解析还需读取原材料的权限。某个 Artifact 的分享不授予
它的 Source 所在 Scope 或原图的读取权限。共享 Memory 可以仍然可见，上游证据明确显示不可用。不开放文件系统路径，
不接受任意客户端路径、外部图片 URL 抓取或无权限保护的静态目录。

Dashboard 按原图组织处理状态和解释版本，复用现有目录与证据组件，支持从 Memory 查看精确解释和原图。普通 Agent
召回继续使用文字并遵守 Memory 策略，图片解释不自动成为第二个默认召回 Family。列表、打开、缩略图生成和召回均不识图。

## 配置与宿主生命周期

使用一个能力开关和一种明确的后端选择，与采集授权分开：

| 选择 | 必需配置 | 语义 |
| --- | --- | --- |
| 关闭 | 无 | 保持既有文字行为；已有图片证据仍可读取 |
| `reuse_generation` | 既有连接及明确验证的多模态能力 | 复用 endpoint/model/credentials，不默认认为模型支持图片 |
| `dedicated_endpoint` | 视觉 endpoint、精确模型、本地保存的鉴权 | 独立在线或用户管理的本地推理连接 |

向导和 `.env` 描述可选配置；文字用户不强制安装图片解码器、Torch/CUDA，也不自动下载模型。密钥通过固定的连接身份
在执行时解析，可以轮换密钥，但不能静默改变模型或部署身份。固定连接不可用时显示 blocked/error 结果。
provider 提供实际部署身份时应记录；不提供不可变模型 revision 时明确标记未知，不能声称已固定权重。请求固定的是
声明的配置，历史回读依赖已保存输出，不依赖能够重现同一次推理。

标准 setup/doctor 负责 Codex 采集安装、授权和诊断。采集器及时暂存提交的图片块，记录稳定宿主／会话／消息来源，
有界重试投递。它保存宿主实际提交的字节，可能已经缩放；Dashboard 准确标注表示。新的宿主格式在验收前明确拒绝或诊断。
setup 保留正常对话采集，并复用现有服务生命周期；用户不单独管理图片 daemon。

## 兼容、迁移与交付

三张领域表和 Family 注册接入正常的版本化迁移，不增加独立 schema version 或自动升级子系统。正式发布明确初始化／
升级支持的图片存储；不兼容时拒绝准入，普通文字仍可用。

未合并实验格式不是已经发布的兼容承诺，不直接覆盖初始化为新布局。若导入实验数据，保留精确原图／解释 Source ID、
字节、payload、journal 位置；两个实验 Definition 继续可读。新解释 Artifact 可以引用旧 Source，但不能把旧机器解释
改写成由新配置生成的内容。在专用转换器通过验收之前，首期允许不修改数据库并明确拒绝导入。实验数据库单独保留。

首期备份／恢复遵循数据库的一致备份边界，包含图片 BLOB。恢复在开放准入前核对资产描述和摘要。材料丢失或损坏明确
返回不可用／完整性结果，不能跳转到别的资产。首期空间不足拒绝新增，不删除历史证据。如果改选外部文件存储，DB／资产
配套备份、耐久预留、文件发布和崩溃安全的孤立清理才成为额外必需契约及验收项。

RFC 通过后按可审查的增量交付：（1）领域／存储／Artifact 发布和显式上传；（2）标准宿主生命周期及 Dashboard/SDK 集成；
（3）真实宿主、模型及恢复验收。RFC 接受与实现批准是独立步骤。#1956 保留为等待按设计调整的实验草稿。

## 验收标准

| 范围 | 必需证据 |
| --- | --- |
| 默认关闭和正常安装 | 文字启动行为保持；可选向导／env、正常服务生命周期与宿主诊断；未配置 Memory 时视觉仍能工作 |
| 真实宿主 | 已支持 Desktop 拖拽／文件选择／粘贴和 CLI 入口；验证实际附件与 Hook 时序，不能只手动调用 Hook |
| 耐久接收 | 精确提交表示经响应丢失、临时文件消失、kill 进程和重启仍能查看 |
| 重放与并发 | 同请求同回执；冲突拒绝；并发准入遵守预算；每原图一个活跃解释；旧 fence/head 不发布 |
| 视觉失败 | 无效／过大输出、超时、provider 崩溃遵守实际请求预算；其他 Scope/Family 继续推进 |
| Source/Artifact 发布 | 版本／视图／结果原子提交；重新解析后仍可精确回读历史；无循环，读取不推理 |
| 权限 | 跨 Scope 拒绝、权限撤销、分享 Artifact 无原图访问；不泄露字节或元数据 |
| Memory 和新会话召回 | 真实生成产生受治理的 Atomic Memory；新的实际 Agent 会话使用召回证据；零 Memory 可观察；迟到解释不能让旧事实变新 |
| 客户端恢复 | 离线积压、容量满、授权关闭／重开、endpoint/Scope 变化和采集器替换保留授权与进度 |
| 存储／迁移 | 正常新建／升级；明确拒绝实验格式；数据库备份／恢复精确 BLOB、损坏和事务中途崩溃恢复 |
| 资源成本 | 测量请求／行／文件增长、预算锁竞争、索引分页及恢复成本；验证各项上限 |

受控 provider 证明请求和恢复行为；真实模型执行证明推理兼容；真实宿主执行证明输入与生命周期支持，分别报告。
既有实验结果不证明这次 Artifact 调整、本地模型运行、所有宿主形式或其他图片数据库后端已经通过验收。

# 缺点

增加一个 Artifact Family 和确定性 Source 发布带来集成工作。二进制数据会增大数据库，并需要明确容量管理；方案仍需三张领域表。
保存的识图结果可能错误，也可能包含敏感文字。在线推理有费用和延迟。精确 provenance 和 Memory 策略降低歧义，但不能
保证识别正确，也不能证明每项内容都应该写成 Memory。

# 理由与备选方案

上面的比较推荐组合方案：当前 Memory 抽取消费 Source，而 Artifact 版本已适合表达派生内容。仅保留 Agent 总结会失去
精确图片证据。纯 OCR 可作为后续验证过的解释器，但会遗漏图形关系。每次召回重新识图重复付费，也可能改变历史含义。
在 Artifact/Source JSON 中直接存 Base64 会放大无关读取，仍不能解决二进制耐久预留和读取授权。

两表方案可以在每次准入时重算预算，但缺少等价的原子预留账本时，要么扫描不断增长的资产，要么存在超额接收风险，因此
提出第三张 capacity 表。如果明确的保留／查询需求超出有界请求记录，再评估独立尝试／操作表。不为减少数量，把无关业务
状态塞入既有表或 Supervisor intent。
实验的外部文件方案让大字节不进入数据库，但需要配套快照和跨资源崩溃恢复。单独的图片 BLOB 表沿用 Skill package
二进制存储模式，同时保留不同领域含义。这是首期推荐；任何存储介质的成本都需要测量，不能预先断定可接受。

不做该功能时，用户继续手动转写图片，证据链缺少精确视觉材料。默认关闭和窄首期范围限制对不需要该能力的安装的影响。

# 相关工作

上述 PowerContext RFC 定义采集与派生内容的分离、Family 领域规则、精确来源、Source-only Atomic 抽取和共享调度。
#1932 记录用户需求和后端选项；#1956 是包含两个原生 Source Definition、独立图片 worker 和七张元数据表的可行性实验。
其代码和验收记录是设计评审的输入，不是已批准的上游架构。

# 未决问题

本 RFC 合并前需要确定：

1. 接受 `image-analysis` Family 与确定性 Source 发布的组合方案，或选择 Source-only 方案并明确等价的历史／查看契约。
2. 接受资产／请求／容量三表边界和数据库字节保存，或指出能提供同等原子准入和逐图片重放保证的已有契约或外部存储方案；
   任何合并都必须保留上述不变量。
3. 确认文件 SQLite 和验收过的 Codex 版本作为首期范围，并确定发布前必须实测的 Desktop/CLI 入口形式。

provider/model 验收、具体公共路由命名、迁移 revision 分配属于已接受契约内的实现评审事项，不因此扩张范围或降低验收。

# 未来可能性

受管理本地视觉可增加明确后端模式：用户授权下载，固定模型和 runtime digest，记录就绪状态，限制磁盘与计算资源，支持
中断下载恢复，并让自有 runner 接入 Server 生命周期。它沿用同一 Artifact/证据链；已经运行的本地 endpoint 仍属于
`dedicated_endpoint`。MiniCPM-V-4.7-1B 是候选，取决于获授权且可下载的权重、许可证、runtime 兼容以及硬件和识别质量
实测；私有 API 可用不等于可以作为分发默认值。本 RFC 验收不包含模型下载、runner 或隐式在线回退。
本地视觉运行在 PowerContext Server 所在机器，不代表远端采集、普通生成和 embedding 全部离线；各阶段目的地仍需明确。

其他数据库后端、区域级证据定位、纯 OCR 和图片检索需要各自验收；重要公共契约变化需要后续设计评审。
