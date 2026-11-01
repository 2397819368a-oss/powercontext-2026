"""
embed_server.py —— 给 PowerContext 用的本地 OpenAI 兼容 embedding 服务

为什么要有这个东西：
    PowerContext 的 Memory 检索有两条通道：全文（FTS）和向量（vector），混合检索时
    用 RRF 融合。但**没配 embedding 供应商时，向量通道不存在**，只剩全文匹配。
    全文匹配是「词条覆盖」判定（Jieba 分词后要求至少命中 2 个查询词），
    所以像「老板，我是不是有东西落这儿了？」这种一个实词都不重合的问法，
    检索结果里根本不会出现「蓝色油纸伞」那条记忆——NPC 也就想不起来。

    这里用 multilingual-e5-small 的 ONNX 导出起一个 /v1/embeddings，
    让 PowerContext 的 vector 和 hybrid 模式真正可用。全程本地、无外网依赖。

实现要点：
    - ONNX 导出只有 last_hidden_state，池化和归一化得自己做：
      按 attention_mask 做 mean pooling（与 sentence-transformers 的
      1_Pooling/config.json 一致），再做 L2 归一化（PowerContext 用 l2 距离 + unit 归一化）。
    - e5 系列建议给查询加 "query: " 前缀、文档加 "passage: " 前缀。
      PowerContext 调用时不区分，因此统一按 max_length 判断：短文本当 query。
      可用 --no-prefix 关掉。

用法：
    C:/pc-embed/.venv/Scripts/python.exe C:/pc-embed/embed_server.py --port 8091
"""

from __future__ import annotations

import argparse
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

# 默认值既支持本机绝对路径，也支持相对这一份脚本所在目录的 model/。
# 优先级：命令行 --model-dir > 环境变量 EMBED_MODEL_DIR > ./model
#
# 目录内的文件名约定（从 ModelScope 下载时把路径分隔符换成了下划线，避免嵌套目录）：
#   onnx/model.onnx            -> onnx_model.onnx
#   onnx/tokenizer.json        -> onnx_tokenizer.json
MODEL_DIR = os.environ.get("EMBED_MODEL_DIR") or str(Path(__file__).resolve().parent / "model")
MAX_LENGTH = 512


class Embedder:
    def __init__(self, model_dir: str = MODEL_DIR, threads: int = 4, use_prefix: bool = True) -> None:
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads
        opts.inter_op_num_threads = 1
        opts.log_severity_level = 3
        self.session = ort.InferenceSession(
            f"{model_dir}/onnx_model.onnx", opts, providers=["CPUExecutionProvider"]
        )
        self.tokenizer = Tokenizer.from_file(f"{model_dir}/onnx_tokenizer.json")
        self.tokenizer.enable_truncation(max_length=MAX_LENGTH)
        self.tokenizer.enable_padding(length=None, pad_id=0, pad_token="[PAD]")
        # token_type_ids 也要一起 pad；用 encodings 手工对齐更安全
        self.tokenizer.no_truncation()
        self.use_prefix = use_prefix

    def _prefix(self, role: str) -> str:
        return {"query": "query: ", "passage": "passage: "}.get(role, "") if self.use_prefix else ""

    def encode(self, texts: list[str], role: str = "passage") -> np.ndarray:
        prefixed = [self._prefix(role) + t for t in texts]
        enc = self.tokenizer.encode_batch(prefixed)
        maxlen = max(len(e.ids) for e in enc)
        maxlen = min(maxlen, MAX_LENGTH)

        input_ids = np.zeros((len(enc), maxlen), dtype=np.int64)
        attention = np.zeros((len(enc), maxlen), dtype=np.int64)
        token_types = np.zeros((len(enc), maxlen), dtype=np.int64)
        for i, e in enumerate(enc):
            ids = e.ids[:maxlen]
            mask = e.attention_mask[:maxlen] if e.attention_mask else [1] * len(ids)
            types = e.type_ids[:maxlen] if e.type_ids else [0] * len(ids)
            input_ids[i, : len(ids)] = ids
            attention[i, : len(mask)] = mask
            token_types[i, : len(types)] = types

        (hidden,) = self.session.run(
            ["last_hidden_state"],
            {"input_ids": input_ids, "attention_mask": attention, "token_type_ids": token_types},
        )
        # mean pooling（只在未被 pad 的位置上求均值）
        mask = attention[:, :, None].astype(np.float32)
        summed = (hidden * mask).sum(axis=1)
        counts = mask.sum(axis=1).clip(min=1e-9)
        out = summed / counts
        # L2 归一化：PowerContext 用 cosine=1-l2^2/2 换算，要求向量是单位向量
        norm = np.linalg.norm(out, axis=1, keepdims=True)
        return out / np.clip(norm, 1e-12, None)


EMBEDDER: Embedder | None = None
LOCK = threading.Lock()
MODEL_NAME = "multilingual-e5-small"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:  # 静音，避免刷屏
        pass

    def do_GET(self) -> None:
        if urlparse(self.path).path in ("/health", "/health/live"):
            self._send(200, {"status": "ok"})
        else:
            self._send(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/v1/embeddings":
            self._send(404, {"error": {"message": "not found"}})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n) or b"{}")
        except Exception as exc:  # noqa: BLE001
            self._send(400, {"error": {"message": f"bad request: {exc}"}})
            return

        raw = payload.get("input")
        if isinstance(raw, str):
            items: list = [raw]
        elif isinstance(raw, list):
            items = [str(x) for x in raw]
        else:
            self._send(400, {"error": {"message": "input must be a string or list of strings"}})
            return
        if not items:
            self._send(400, {"error": {"message": "input must not be empty"}})
            return

        # 输入类型按请求里的 input_type / truncate 之类 hint 走；没有就按长度猜：
        # 短句多半是查询，长文本多半是待索引的文档。
        role = "passage"
        hint = str(payload.get("input_type") or "").lower()
        if hint in ("query", "search_query"):
            role = "query"
        elif hint in ("passage", "document"):
            role = "passage"
        elif len(items) == 1 and len(items[0]) <= 64:
            role = "query"

        try:
            with LOCK:
                vectors = EMBEDDER.encode(items, role)
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"error": {"message": f"embedding failed: {exc}"}})
            return

        self._send(
            200,
            {
                "object": "list",
                "model": MODEL_NAME,
                "data": [
                    {"object": "embedding", "index": i, "embedding": [float(v) for v in row]}
                    for i, row in enumerate(vectors)
                ],
                "usage": {"prompt_tokens": sum(len(t) for t in items), "total_tokens": sum(len(t) for t in items)},
            },
        )


def main() -> None:
    global EMBEDDER
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8091)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--model-dir", default=MODEL_DIR)
    ap.add_argument("--no-prefix", action="store_true", help="不添加 e5 的 query:/passage: 前缀")
    args = ap.parse_args()

    EMBEDDER = Embedder(args.model_dir, args.threads, use_prefix=not args.no_prefix)
    warm = EMBEDDER.encode(["warmup"], "query")
    print(f"embedding dim = {warm.shape[1]}", flush=True)
    print(f"listening on http://{args.host}:{args.port}/v1/embeddings", flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
