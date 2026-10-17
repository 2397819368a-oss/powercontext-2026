---
title: 快速开始
description: 通过配置向导安装完整记忆能力，接入 Codex，并验收 Source、Topic Memory 与跨会话召回。
---

# 快速开始

本页从安装开始，带你完成一次真实的记忆体验：在 Codex 中讨论项目，看到原始输入进入 Source、主题记忆生成并演进，
再在新会话中找回决策。以下命令使用 PowerContext 1.3.0，Agent 插件使用对应的
`powercontext-v1.3.0` tag。

需要 macOS 或 Linux、Python 3.11+、Git、[uv](https://docs.astral.sh/uv/getting-started/installation/) 和已安装的 Codex CLI。
完整记忆还需要可用的 Generation 和 Embedding 模型 API；准备好各自的地址、模型名和 API key。
Codex 或 Claude 的订阅登录不会自动为 PowerContext Server 提供这些 API。只有 Agent 登录、没有模型 API 时，
仍可选择基础记忆，验收显式保存与召回，但不能据此期待自动 Topic Memory。

## 开始前先选择基础记忆或完整记忆

**基础记忆**不依赖模型 API：可以通过 Codex 显式保存和搜索持久 Memory，但普通对话不会自动变成 Topic Memory、
Profile、Experience 或 Skill。**完整记忆能力**需要 Generation 和 Embedding 模型 API，并启用相应后台处理。
它会在满足各自证据和策略条件时启用这些流程；并不表示每条输入都会产生每一种 Artifact。

PowerContext 目前有七个 Artifact family。Source 是它们的证据，Tag 是元数据，不是 Artifact。可以用下列示例告诉
Codex 想做什么；它们说明的是预期工作流，并不是能跳过正常检查的“魔法口令”。

| 值 | 用途 | 在 Codex 中如何开始 | 是否需要完整记忆？ |
| --- | --- | --- | --- |
| Memory | 持久保存事实、决定、约束和下一步 | “记住这个项目使用 uv。”之后再要求搜索它。 | 不需要；基础记忆可显式保存和召回。 |
| Topic Memory | 带有 Source 证据、由模型维护的主题摘要 | 在普通输入中讨论相关决定，等待配置好的 Topic 处理运行。 | 需要。 |
| Profile | 在上下文准备中使用的、已审核的 Scope 背景 | 为 Scope 启用 Profile 策略，再按 Profile 工作流操作。 | 需要。 |
| Experience | 从已观察任务结果提炼的可复用经验 | 记录 task outcome，再要求生成或提交 Experience candidate 并完成审核。 | 生成需要；人工提交 proposal 不需要。 |
| Managed Skill | 含指令和校验项、需要审核的流程 | 要求生成或提交 Skill candidate，审核后导出已批准的 Revision。 | 生成需要；人工提交 proposal 不需要。 |
| Handoff | 用于继续当前工作的持久里程碑 | “交接当前工作”，或先预览再提交。 | 不需要。 |
| Prompt | Scope 级别的运行配置 | 按文档中的 Prompt 工作流审慎配置，不通过普通对话设置。 | 不需要。 |
| Source | 从输入或集成中采集的证据 | 信任 Hook 后发送普通输入，或使用显式 Source 工作流。 | 不需要。 |
| Tag | 组织受支持 Artifact 的元数据 | 要求为一个 Memory、Experience、Skill 或 Handoff 添加明确标签。 | 不需要。 |

已批准的 Managed Skill 仍保存在 Server 中。批准既不代表它已安装到 Codex，也不代表它可以执行：必须将确切的已批准
Revision 导出到 Agent Skill target，宿主才能发现它。审核和导出步骤见
[Experience 和 Skill 生命周期](../workflows/experience-and-skill-lifecycle.md)。

## 1. 安装并进入配置向导

```bash
uv tool install --force "powercontext[cli,server]==1.3.0"
mkdir -p ~/powercontext-demo
cd ~/powercontext-demo
powercontext config init --language zh --output .env
```

首次本机体验可以这样选择：

1. **存储**：SQLite 可直接开始；需要体验嵌入式 seekdb 时选择 seekdb，并同意后台安装缺失依赖。
2. **使用场景**：Agent、浏览器和 Server 都在这台机器时选“只在当前机器”。Server 在另一台机器时，先看[连接远程 Server](../operate/connect-remote-server.md)。
3. **记忆能力**：选择“完整记忆能力”，填写 Generation 和 Embedding API；不确定协议与维度时看[模型配置](configure-models.md)。
4. **Dashboard**：开启，便于观察 Source 和记忆。本地认证默认关闭，需要访问认证时再开启，见[可选认证](configure-server-environment.md#本地-dashboard-与可选认证)。
5. **后台处理**：可先使用各制品的推荐周期。检查间隔不是完成时限，模型处理还需要时间。
6. **Agent**：选择 Codex，规划新的独立 Scope；如需 Claude Code，再选择它，最后选择“结束 Agent 配置”。
7. 核对并保存。

保存后会得到以下文件：

| 文件 | 用途 |
| --- | --- |
| `.env` | 本次安装使用的 Server、客户端、Agent、数据库和模型配置，包括已配置的凭据 |
| `.env.next-steps.md` | 与本次选择对应的启动、Scope 创建、插件连接和验收说明 |

向导会打印 Dashboard 地址和所选 SSH 转发命令。启用认证时，还会显示一次新生成的 Server Token，
以后可在 `.env` 中查看 `POWERCONTEXT_SERVER_AUTH_TOKEN`。文件可能包含凭据，不要提交到 Git。
若 seekdb 仍在安装，向导会在当前界面显示活动进度并等待；安装失败时先按提示完成依赖安装。
保存配置或装好依赖，都不代表 Server 已经启动。

## 2. 启动 Server

在当前终端执行：

```bash
powercontext config validate --env-file .env
powercontext server run --env-file .env
```

保持终端运行。在浏览器打开向导输出的 Dashboard 地址，端口以 `.env` 中保存的
`POWERCONTEXT_SERVER_HTTP_PORT` 为准。未启用认证时可直接进入页面；启用后使用 **Server Token** 登录，不是模型 API key。
首次没有数据是正常现象。需要关闭终端后继续运行时，先停止前台 Server，再通过
`powercontext service install --env-file .env` 安装[个人后台服务](../operate/deploy-server.md#运行持久个人-server)，复用同一份配置。

另开终端，加载客户端连接配置并检查服务：

```bash
cd ~/powercontext-demo
set -a
. ./.env
set +a
powercontext ready
powercontext capabilities
```

确认服务就绪且所选处理能力可用；`degraded` 表示部分依赖仍有问题，不能算完整记忆验收通过。
`config validate` 只检查配置，在线结果以启动后的检查为准。

## 3. 创建 Scope 并安装 Codex 插件

打开 `.env.next-steps.md`，执行其中“创建计划中的独立 Scope”的请求。Server 响应会返回真正的 `scope_id`。
将它写入 `.env`：

```dotenv
POWERCONTEXT_CODEX_SCOPE_ID=替换为返回的scope_id
```

`codex-xxxxxxxx` 是向导规划的标题，不是 ID。配置 Claude Code 时，将其创建请求返回的 ID 写入
`POWERCONTEXT_CLAUDE_SCOPE_ID`。不同 Agent 可以各自隔离，也可以显式绑定同一已有 Scope。
切换目录本身不会创建隔离。验收期间，Dashboard 和 Agent 必须使用相同的 Scope。

重新加载客户端文件并安装匹配的插件：

```bash
set -a
. ./.env
set +a
powercontext setup codex --ref powercontext-v1.3.0
powercontext doctor codex
codex
```

首次新建 Codex 会话时，Codex 会询问是否信任 PowerContext 的 `SessionStart` 和 `UserPromptSubmit` Hook。若这是你要连接的
Scope，请信任二者：`UserPromptSubmit` 会在输入前准备有界召回，并将该输入采集为 Source 证据。若拒绝信任，Codex 仍可打开，
MCP 也可能仍可用，但自动召回和 Source 采集不会运行；用 `/hooks` 查看或修改该选择后，重新开启一个会话再验收。
然后确认 PowerContext Hook 和 MCP 均已加载。非默认地址还需按 `.env.next-steps.md` 的 Codex 连接说明，
检查已安装插件的 `.mcp.json`：MCP 的 URL 必须和 Hook 使用同一 Server，启用认证时还需配置凭据。
仅安装插件不会启动 Server。

这些命令以 Codex CLI 为例。桌面 App 不一定继承终端的环境变量；在桌面中验收前，要确认 App 的 Hook 和 MCP
都拿到了相同的地址和 Scope，启用认证时还需取得对应凭据。更多宿主行为见[Codex](../integrations/codex.md)和[Claude Code](../integrations/claude-code.md)。

完整能力中的 Profile 还需要为这个真实 Scope 启用生成策略。按[Profile 策略步骤](configure-models.md#为-scope-启用-profile-策略)
读取当前版本并更新；只生成配置文件不会自动完成它。

## 4. 用普通对话验收 Topic Memory

在刚启动的 Codex 会话中发送一条包含具体决策的普通输入，例如：

```text
我们正在设计 Aurora 验收项目。决定用 uv 管理 Python 依赖，配置文件使用 .env。
第一阶段只在一台 Linux 服务器运行，Mac 通过 SSH 隧道访问。请复述这些约束，暂时不要修改代码。
```

在 Dashboard 选择刚绑定的 Scope，依次检查：

1. **Source**：能找到这条用户输入。Codex 的提示词采集不意味着自动采集所有 Agent 回复。
2. **Topic Memory**：经过所配置的检查周期和模型处理后，出现相关主题，内容与 Source 相符。
3. 继续发送相关决策：“Aurora 的部署方案更新：服务需要由用户级服务管理器运行；SSH 隧道方案保持不变。”
4. 确认第二条 Source 入库，主题内容或修订记录反映这次更新。不要以“等了一分钟”代替检查。
5. 开启新的 Codex 会话，保留同一 Scope，询问：“从 PowerContext 找回 Aurora 的依赖管理和部署决定，带上引用。”

找到正确内容及其 citation，才完成从采集到演进再到跨会话召回的验收。`doctor codex` 成功仅说明安装与连接检查通过，
不能替代上述业务检查。模型可能决定合并或不更新主题，不保证每条消息都创建一条记忆。

基础记忆模式可改为显式要求 Agent“把 Aurora 使用 uv 的决定保存到 PowerContext”，再在新会话搜索并核对 citation；
该测试不覆盖自动提取。完整能力中的 Profile、Experience 和 Skill 还有各自的触发或审核条件，
见[模型与能力配置](configure-models.md#完整能力不等于每条对话都会生成所有制品)。

## 继续使用

- [安装和运行](install-and-run.md)：版本、seekdb 依赖、已有存储与更新。
- [部署 Server](../operate/deploy-server.md)：后台服务、SSH、HTTPS 和数据备份。
- [配置模型](configure-models.md)：API 协议、向量维度与提取检查。
- [故障排查](../operate/troubleshoot.md)：服务、模型、采集或召回失败。
