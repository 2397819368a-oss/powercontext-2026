---
title: 图片 Source 阶段二使用与验收
description: 默认关闭的单图上传、SQLite 保存、来源与原图回读、预算和恢复边界。
---

# 图片 Source 阶段二使用与验收

阶段二实现显式 PNG／JPEG 上传、原图保存、原图 Source 与 pending 任务的耐久接收，以及 HTTP、Client 和 CLI 回读。pending 不代表识图成功或已经生成 Memory。后台解析、重试与消费已接入，完整使用说明见 [阶段三、四说明](phase34-usage.md)。

功能默认关闭。两个图片原生 Definition 始终保留，供历史数据解码；原图始终排除，Readable／partial 解析可提供文字投影，不可读结果只保留审计。可选 Codex CLI 捕获、配置引导及原图查看见 [捕获与查看说明](phase56-usage.md)。

## 独立部署与配置

```powershell
cd D:/code/git-project/powermem/powermem-image-source-experiment
uv sync --locked --extra images --extra server --extra cli
```

images 是可选解码依赖，只使用 Pillow，没有 Torch、CUDA 或模型权重。关闭功能不会导入 Pillow、创建图片目录或实验图片表。格式识别后还会 verify 并 load 完整单帧，检查尺寸和预算。[Pillow 官方说明](https://pillow.readthedocs.io/en/stable/reference/Image.html)

在忽略的实验目录保存独立环境文件，配置独立 SQLite、资产目录和端口。以下占位符必须替换为本地实际路径和用户模型配置，不能直接运行：

```dotenv
POWERCONTEXT_SERVER_DATABASE_URL=sqlite+aiosqlite:///D:/code/git-project/powermem/powermem-image-source-experiment/.powercontext/image-source-experiment/runtime.db
POWERCONTEXT_SERVER_HTTP_HOST=127.0.0.1
POWERCONTEXT_SERVER_HTTP_PORT=18171
POWERCONTEXT_SERVER_IMAGES_ENABLED=true
POWERCONTEXT_SERVER_IMAGES_ASSET_DIRECTORY=D:/code/git-project/powermem/powermem-image-source-experiment/.powercontext/image-source-experiment/assets
POWERCONTEXT_SERVER_INFERENCE_VISION_MODEL=<精确服务模型 ID>
POWERCONTEXT_SERVER_INFERENCE_VISION_BASE_URL=<实际 HTTP(S) endpoint>
POWERCONTEXT_SERVER_INFERENCE_VISION_DEPLOYMENT_ID=<部署版本标识>
```

模型 ID、endpoint 和部署标识在接收时形成非秘密快照。接收不探测 endpoint；后台 worker 从受保护配置解析匹配的 Vision 凭据。非 loopback 地址只允许 HTTPS，无 userinfo、query 或 fragment；缺少或无效配置时拒绝新摄取，普通文字仍可用。密钥留在受保护配置中。

服务沿用现有启动命令和环境文件选项，可查看 `uv run powercontext server --help`。不得复用主 checkout 的服务、配置和数据库。图片接收只使用已存在且允许使用的 Scope，不自动创建 Scope。

图片能力要求文件 SQLite、WAL 或 DELETE journal，以及 FULL 及以上 synchronous。内存数据库、MEMORY journal 或较弱同步配置会返回 image_database_not_durable，普通文字继续沿用既有行为。这一限制确保图片接受的持久化含义成立。

## CLI 与 Client

```powershell
uv run powercontext --server-url http://127.0.0.1:18171 --json image upload .powercontext/image-source-experiment/example.png --scope-id <Scope ID> --capture-key screenshot-1 --context-text "本次报错截图"
uv run powercontext --server-url http://127.0.0.1:18171 --json image task <task_id> --scope-id <Scope ID>
uv run powercontext --server-url http://127.0.0.1:18171 image download <asset_id> --scope-id <Scope ID> --output .powercontext/image-source-experiment/restored.png
```

upload 根据 PNG／JPEG 扩展名选择 MIME，必要时可传 --media-type；Server 检查真实格式，改扩展名不能绕过验证。capture_key 由调用方稳定提供，相同上传用同键重放，新场景使用新键。context-text 可以为空。

download 验证摘要与字节数，再保存新文件，不覆盖已有目标。临时文件与 hard link 用于原子发布；输出文件系统不支持 hard link 时明确失败，已有文件保持不变。

阶段二的三个 Python Client 异步方法使用生成的严格 DTO；重试、重新解析及解析来源查询见阶段三、四说明：

- capture_image(ExperimentalCaptureImageRequest)：单图字节与来源的耐久接受。
- get_image_task(ExperimentalGetImageTaskRequest)：状态及 exact original_payload。
- get_image_asset(ExperimentalGetImageAssetRequest)：原始 bytes_base64 与资产信息。

Base64 仅用于有界传输，不进入 Source 或文字证据。Server 不读取客户端路径、不下载 URL。任务查询中的原图 payload 包含 capture_key、origin、context 和资产字段，再用 asset_id 回读原字节；这是图片来源的正式 HTTP 读路径。通用 Source HTTP 接口保留已有文字契约。

## 接收、预算和恢复

接收先校验图片并耐久预留容量，再在 SQLite 写事务内保存文件、retained 状态、原图 Source、pending 任务、Source 通知和幂等响应。202 只在该提交成功后返回。全局预算行的写锁覆盖接受与维护，防止并发超分配。

文件使用 Scope 摘要目录和内容摘要文件名。每个资产预留对应固定临时文件名，避免崩溃后失去清理入口。临时文件 flush/fsync 后原子发布；Windows 使用带 write-through 的 MoveFileExW，POSIX 同步新建目录及发布目录。取消时等待线程文件操作完成后释放事务，避免清理与晚到写入交错。

文件与 SQLite 不共享事务。接受失败时资产预留保持 reserved，继续占用容量；asset/get 不返回它，也没有成功 Source、任务或接收响应。相同字节可以复用预留和文件。其他新图可能因容量不足被拒绝，不能用反复失败绕过容量上限。

启用服务启动时清理一批超过 24 小时的 reserved 资产，最多 100；按索引查询，不扫描文件树或全库 Source。完整文件和对应临时文件均删除成功后才释放预算；复用旧预留会刷新宽限期。retained 原图不自动删除，关闭时不运行清理。pending 可以跨重启保存，running claim 的租约恢复见阶段三、四说明。清理失败时阻止新的上传、重试和重新解析；已保存的任务、解析和原图仍可按权限读取，既有任务继续按原预算处理，普通文字仍可运行。格式不兼容或资产损坏仍拒绝对应读取。

同 Scope、摄取键、附件位置和完整内容的重放返回原 sequence、asset 和 task_id，当前模型改变也不改历史。不同字节、来源或上下文同键返回 409。同字节不同来源共享 Scope 内资产，保留各自 Source 与任务；不同 Scope 独立记账和授权。

上传需要 scope.contribute，回读和查询需要 scope.read。context.source 还检查同 Scope、真实文字来源和读取权限。Scope 管理角色不隐含内容权限；授权服务不可用时拒绝访问。

未知实验格式、缺表或字段、根目录改变、历史 manifest 占用保留名称会明确拒绝图片能力，普通文字仍可运行。损坏资产返回错误，原材料缺失返回 410，不返回别的图片代替。

默认单图 8 MiB、请求体 12 MiB、20,000,000 像素、单边 16,384；每 Scope 256 个待处理任务、全局 1024；容量默认 1 GiB。reserved 与 retained 都计费。这些是保护默认值，没有性能最优结论。

## 备份与证据级别

备份包含一致 SQLite 快照和固定资产目录，保留原图、pending 任务和容量预留。先停止图片写入及清理，或停止独立实验服务，再备份 DB、复制资产并核对摘要。运行中分别复制目录与 DB，或只复制带 WAL 的数据库主文件，不能视为一致备份。

恢复保持相同资产根目录；变更路径需要停服制定并验证显式迁移，本阶段不提供自动重定位或删除已接受证据的操作。

测试使用实际 SQLite、HTTP adapter，另有实际 loopback Uvicorn、CLI 和 Client 链路。MiniCPM-V-4.7-35B-A3B 的实际 worker 已验证；Desktop 运行内核与正式采集器已联调，实际界面仍需按 [桌面验收指南](desktop-testing.md) 测试。完整命令、结果、代码审查与修复见 [实施记录](implementation-log.md)。
