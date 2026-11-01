# 运行手册（Windows 11 · Docker Desktop + WSL2 · 全程本地模型）

> 目标：从一台干净机器跑到「老陈记得你」。
> 全程 **不依赖境外网络**（容器镜像、模型权重、Python 包全部走国内可达源），
> 除安装 WSL2 需要管理员权限外，其余都不需要。

## 0. 组件与端口

> 下文的 `./` 都指**本目录**（仓库里的 `dify/`）。

| 组件 | 作用 | 端口 | 备注 |
|---|---|---|---|
| **Dify**（Docker） | 编排：召回 → 生成 → 写回 | 8090 | `EXPOSE_NGINX_PORT=8090` |
| **PowerContext** | 长期记忆（Scope/Memory + RRF 混合检索） | 8000 | SQLite 后端 |
| **llama.cpp** | 本地对话模型 Qwen2.5-3B-Instruct Q4_K_M | 8088 | OpenAI 兼容 |
| **embed_server** | 本地向量：multilingual-e5-small (ONNX) | 8091 | 供 PowerContext 的向量通道使用 |

**本仓库包含什么**：编排所需的全部脚本与配置（`app/`、`deploy/`、`pc-config/`）。

**本仓库不包含什么**（体量太大，用时自己取）：

| 缺失物 | 怎么拿 |
|---|---|
| Dify 源码约 1.6 万文件 | 见 §5，一条 `git clone` |
| 模型权重约 2.5GB | 见 §2，从 `modelscope.cn` 直连下载 |
| PowerContext 本体 | Python 包，`uv pip install` 即可，见 §3 |

```
玩家 → Dify Service API ──① /v1/memory/search──→ PowerContext ──→ embed_server
                        ──② 生成台词（借用 GPUless 本地 LLM）──→ llama.cpp
                        ──③ /v1/memory/remember─→ PowerContext ──→ embed_server
```

## 1. 前置检查

| 依赖 | 要求 | 怎么确认 |
|---|---|---|
| WSL2 | 已安装 | `wsl --status` 有输出（**需管理员**：`wsl --install --no-distribution`，装完重启） |
| Docker Desktop | ≥ 4.40 | `docker version` 能看到 Server 段 |
| Python | 3.13（uv 管理） | `uv --version` |
| Node.js | ≥ 20 | `node -v` |

> **本机实测坑 1**：Docker Desktop 装完但 `docker` 命令不在 PATH 里，需新开终端，
> 或直接用 `C:\Program Files\Docker\Docker\resources\bin\docker.exe`。

> **本机实测坑 2**：shell 里设了 `http_proxy`，curl 会把 `127.0.0.1` 也发给代理，
> 拿回一个 502，看起来像「服务起不来」，其实只是被代理挡了。
> **验证本机服务一律加 `--noproxy '*'`**。（Node 的 `fetch` 不读这两个变量，不受影响。）

## 2. 起本地模型服务

对话模型和向量模型分两个进程跑——llama.cpp 要 `--embeddings` 才是纯向量模式，
同一个实例没法两用，而再起一份 3B 进程内存不划算（本机 32GB，Docker 已吃掉大半）。

### ① 对话模型（llama.cpp）

```bash
# 二进制：从 llama.cpp 的 Releases 取 Windows CPU 版，解压到任意目录（下例为 C:/llama）
# 权重：modelscope.cn 下载 Qwen2.5-3B-Instruct 的 Q4_K_M GGUF（约 2.0GB），放 C:/gguf/

cd C:/llama
./llama-server.exe -m C:/gguf/qwen2.5-3b-instruct-q4_k_m.gguf \
  --host 127.0.0.1 --port 8088 -c 4096 -t 8 --no-warmup
```

> **端口别用 8080**。开发机上 8080 被联想 `ApplicationWebServer.exe` 占用，
> llama-server 会直接 exit 1，日志里只有一行 `couldn't bind HTTP server socket`，
> 很容易误判成 DLL 缺失或 VC 运行库问题。实测改用 8088 即可。

### ② 向量模型（本仓库自带服务）

```bash
# 权重：modelscope.cn 的 AI-ModelScope/multilingual-e5-small
#       取 onnx/model.onnx（470MB）+ onnx/tokenizer.json + onnx/tokenizer_config.json
#       放进某个目录，下例记为 $EMBED_MODEL_DIR

uv venv .venv-embed --python 3.13
uv pip install --python .venv-embed/Scripts/python.exe onnxruntime tokenizers numpy \
  -i https://pypi.tuna.tsinghua.edu.cn/simple

.venv-embed/Scripts/python.exe deploy/embed_server.py --model-dir "$EMBED_MODEL_DIR" --port 8091
```

> ONNX 导出吐的是原始 `last_hidden_state`，**没有**做池化和归一化。
> `deploy/embed_server.py` 里实现了 mean pooling + L2（`--model-dir` 默认指向 `model/`），
> 直接用 HF 那一套会得到全是接近 1 的假相似度——这是最容易踩的一个坑。

验证：

```bash
curl --noproxy '*' http://127.0.0.1:8088/v1/models
curl --noproxy '*' http://127.0.0.1:8091/health
```

## 3. 起 PowerContext

在本目录（`dify/`）操作：

```bash
# 首次
uv venv .venv --python 3.13
uv pip install --python .venv/Scripts/python.exe "powercontext[cli,server]" \
  -i https://pypi.tuna.tsinghua.edu.cn/simple
```

配置模板见 `pc-config/powercontext.env.example`。先复制一份再填真值：

```bash
cp pc-config/powercontext.env.example pc-config/powercontext.env
```

其中关键项：

```ini
POWERCONTEXT_SERVER_HTTP_HOST=0.0.0.0    # 必须 0.0.0.0，容器才能经 host.docker.internal 访问
POWERCONTEXT_SERVER_HTTP_PORT=8000
POWERCONTEXT_SERVER_ACCESS_MODE=enforced
POWERCONTEXT_SERVER_AUTH_TOKEN=<随机串>   # 换成你自己的，别用仓库里的占位符
POWERCONTEXT_SERVER_DASHBOARD_ENABLED=true

# 向量通道（没有这段就只有全文检索，详见「已知限制」第 1 条）
POWERCONTEXT_SERVER_INFERENCE_EMBEDDING_BASE_URL=http://127.0.0.1:8091/v1
POWERCONTEXT_SERVER_INFERENCE_EMBEDDING_MODEL=openai:multilingual-e5-small
POWERCONTEXT_SERVER_INFERENCE_EMBEDDING_PROFILE_ID=e5-small-local
POWERCONTEXT_SERVER_INFERENCE_EMBEDDING_DIMENSION=384
POWERCONTEXT_SERVER_INFERENCE_EMBEDDING_NORMALIZATION=unit
POWERCONTEXT_SERVER_INFERENCE_EMBEDDING_SEND_DIMENSIONS=false
```

注意环境变量前缀是 `POWERCONTEXT_SERVER_`，嵌套分隔符是 `_`（见
`server/settings.py:204`），所以嵌套字段 `inference.embedding_model` 要写成
`POWERCONTEXT_SERVER_INFERENCE_EMBEDDING_MODEL`。

后续命令都要用到这个 token，先导出：

```bash
export POWERCONTEXT_SERVER_AUTH_TOKEN=<你刚才填的那个随机串>
```

启动：

```bash
POWERCONTEXT_HOME="C:/pc-data-vec" \
  .venv/Scripts/powercontext.exe server run --env-file pc-config/powercontext.env
```

确认两条：

```bash
curl --noproxy '*' http://127.0.0.1:8000/health/live     # {"status":"ok"}
curl --noproxy '*' -H "Authorization: Bearer <token>" http://127.0.0.1:8000/health/ready
# ready 里应当出现 "inference.embedding":"ready"
```

> **⚠️ 踩过的坑：不要给已经跑起来的旧库追加 embedding 配置。**
> 启用向量通道会新增一批 `sqlite-vec` 影子表（`pc_atomic_memory_current_vec*`），
> 而 `powercontext` 在启动时会比对 schema 指纹，指纹对不上会直接拒绝启动：
> `MigrationError: incompatible_schema: Actual schema does not match the recorded revision.`
> 解决办法是换一个新的 `POWERCONTEXT_HOME` 从头初始化（老目录我们已经备份为 `C:/pc-data.bak.*`），
> 而不是去改老库。config validate 只会校验语法，发现不了这个问题，必须实际启动才知道。

> Windows 上 PowerContext 用 **SQLite** 后端——嵌入式 seekdb 只在 Linux/macOS 提供，这是平台限制不是配置问题。

## 4. 灌入种子记忆

```bash
cd app
node seed/seed-demo.mjs            # 演示用账本（幂等，可反复跑）
node seed/seed-demo.mjs --fresh    # 换一个全新干净 scope（旧数据还在，便于对比）
```

跑完会在 `app/.scope` 写入 scope id。

> `seed/seed.mjs` 是开发时用的账本，里面混着调试产生的测试条目；
> `seed-demo.mjs` 专门给截图/录像用，内容一样但独立。两者共用 `seed/world.mjs` 里的世界设定。

## 5. 起 Dify

### ① 取 Dify 源码

```bash
cd ..                       # 回到仓库根之外的一个工作目录
git clone --depth 1 -b 1.18.0 https://github.com/langgenius/dify.git dify-source
# 国内拉不动就用镜像：
# git clone --depth 1 -b 1.18.0 https://gitee.com/dify_ai/dify.git dify-source
cd dify-source/docker
```

### ② 生成 `.env`（**这步漏了会直接起不来**）

```bash
cp .env.example .env
```

`.env` 里必须确认三项：

```ini
EXPOSE_NGINX_PORT=8090                    # 避开 80/443，本机这两个端口已被占
SECRET_KEY=<openssl rand -base64 42>      # 不填的话 api 容器会拒绝启动
```

### ③ 让容器能访问宿主机服务（**不加会 SSRF 拦截**）

在 `.env` 末尾追加：

```ini
SSRF_PROXY_ALLOW_PRIVATE_IPS=192.168.65.254/32
SSRF_PROXY_ALLOW_PRIVATE_DOMAINS=host.docker.internal
DIFY_AGENT_LOCAL_SANDBOX_AUTH_TOKEN=<任意随机串>
```

> 前两行不加，工作流里的 HTTP 节点会报：
> `Access to 'http://host.docker.internal:8000/v1/memory/search' was blocked by SSRF protection`。
> Dify 的出网请求统一走 squid 代理（`ssrf_proxy` 容器），默认 deny RFC1918，
> `192.168.65.254` 是本机 `host.docker.internal` 解析出来的地址。
> 改完要 `docker compose up -d --force-recreate ssrf_proxy agent_ssrf_proxy` 才生效。
>
> 第三行不填的话 `local_sandbox` / `agent_backend` 会一直 Restarting（`shellctl: auth token is required`）。
> 这两个容器本作品用不到，但留着它们反复重启不好看。

### ④ 换成国内镜像站并启动

```bash
cp <本仓库>/dify/deploy/docker-compose.override.yaml ./docker-compose.override.yaml
bash <本仓库>/dify/deploy/pull-images.sh     # 串行拉取，更稳
docker compose up -d
```

> 首次约 8GB。**务必串行**：并行拉会互相抢带宽，实测吞吐只有串行的 1/10
> （并发 0.1MB/s，串行能到 1–3MB/s）。

`docker-compose.override.yaml` 干的事：把官方镜像全部换成国内镜像站
（`docker.1ms.run` / `docker.m.daocloud.io`），并把 `cr.weaviate.io` 也换掉
（宿主能 curl 通，但 WSL2 里的 daemon 解析不了，会回落成 `registry-1.docker.io` 然后失败）。

## 6. 初始化 Dify 并导入工作流（脚本化）

```bash
cd app
node tools/dify-bootstrap.mjs          # 建管理员 → 删同名旧应用 → 同步 scope/token → 导入 → 发布 → 取 API Key
node tools/dify-setup-model.mjs        # 装 OpenAI-API-compatible 插件 → 注册本地模型 → 设为默认
```

> `dify-bootstrap.mjs` 会做一件重要的事：**把本机的 scope 与 token 写进 DSL 再导入**。
> 仓库里的 `grudge-tavern.chatflow.yml` 这两个值是 `REPLACE_WITH_YOUR_PC_*` 占位符
> （工作流导出会把 HTTP 节点的 Bearer 值明文写进 YAML，直接提交等于泄漏密钥），
> 脚本从 `app/.scope` 和环境变量 `POWERCONTEXT_SERVER_AUTH_TOKEN` 取真值注入。
> 所以导入前请确保 §3 里那个 token 已经 export。

`dify-bootstrap.mjs` 里踩过的坑都写成了代码注释：

1. `/console/api/login` 的密码要 **base64 编码**（`FieldEncryption` 实际只做 base64），传明文报 `Invalid encrypted data`。
2. 登录后必须同时带 `Cookie: access_token=...; csrf_token=...` 和 `X-CSRF-Token` 头，缺一个 401。
3. `POST /console/api/apps/imports` 每次是**新建**应用，所以要先把同名旧的删掉。
4. Service API 只跑**已发布**的工作流，不发布会报 `Workflow not published`。
5. 它会用 `app/.scope` 的内容和环境变量里的 token 改写 DSL 的 `PC_SCOPE` / `PC_TOKEN` 再导入，
   避免两边配置漂移（详见上面的说明）。

## 7. 验证

**A. 不依赖 Dify 的链路验证**（证明记忆部分本身是通的）：

```bash
export POWERCONTEXT_SERVER_AUTH_TOKEN=<token>
node tools/simulate.mjs "老板，来碗酒，先记着，回头一起结"
```

应当看到：召回若干条 → 调用模型 → `解析 成功（format=json）` → 写回一行。

**B. 端到端验证**（走 Dify 的 Service API，推荐，证据最全）：

```bash
export POWERCONTEXT_SERVER_AUTH_TOKEN=<token>
node tools/dify-smoke.mjs
#    会自动读 app/.difykey，无需手动传 DIFY_KEY
```

脚本三轮 + 三层证据：

| # | 提问 | 考察点 |
|---|---|---|
| 1 | 「我把那把**蓝色油纸伞**落在你柜台上了」 | 写入 +1 条 |
| 2 | 「我是不是有东西落这儿了？」（**不提伞字**） | 同会话能否召回并说出伞名 |
| 3 | 「我昨天落的东西你收好了吗？」（**新会话**） | 跨会话能否召回 |

每层还打印节点级证据：检索 `mode=hybrid`、召回条数、模型原始 JSON 输出、解析方式、写回结果。
当前实测稳定 **9 passed, 0 failed**。

> 标记物特意选了种子记忆里没有的东西，避免「本来就是世界设定里写死的」这种假命中。

## 8. 重置存档

```bash
node seed/seed-demo.mjs --fresh
node tools/dify-bootstrap.mjs     # 会把新 scope 同步进 DSL 并重新发布
```

## 已知限制

1. **检索方式依赖 embedding 是否配置。** 不配 embedding 时 PowerContext 只有全文通道，
   它的准入规则是先 Jieba 分词、再要求候选至少命中 2 个查询词（`builtin/artifacts/search.py`
   的 `_FTS_MIN_MATCHED_TERMS=2`）。于是「老板，我是不是有东西落这儿了」这种说法
   **连一条候选都进不来**，NPC 就想不起伞——不是 PowerContext 不行，是这一步压根没走向量。
   配上 local embedding 后 `mode` 从 `text` 变成 `hybrid`（全文 + 向量 + RRF），问题消失。
   这条也是本项目里最值得说的工程结论。
2. **写回格式会影响召回质量。** 早期版本把「玩家原话 → 老陈台词」整句回灌，
   同一个问题问两次时，检索结果会被上一轮的回声占满，真正要找的旧账被挤到后面。
   现在只存第三人称摘要 + 信任度。
3. **3B 模型的台词质量有限，靠温度 + 少提示词来回的量出来的。**
   温度 0.7 时 8/9，失败项在 2、3 轮之间漂移；降到 **0.5** 后三轮测试稳定 9/9。
4. **⚠️ 别给 3B 加「跟验收问法相近」的 few-shot 示例。**
   试过加一条「客人问：我是不是有东西落你这儿了 → 老板答：你那个蓝布包袱……」的示例，
   还顺手把温度降到 0.2。结果是三轮输出**一字不差地照抄示例里的"蓝布包袱"**，
   稳态掉到 7/9——比不加还差。小模型会把 few-shot 退化成「复读」，
   示例教的是行为，它学成了答案。现在的做法是：**只留一条不同场景的示例（还酒钱）**，
   「必须用物件本名」这条规则放在文字约束里，而不是靠示例暗示。
   换更强的模型（改 Dify 里的模型配置即可）会明显更好，链路本身不用动。
5. 单 scope = 单存档，多人同时玩会串账。
