"""真实 MiniMax 连通性探针。

用途：拿到 API key 之后、跑正式流水线之前的体检。
回答三件事：

1. key 是否被服务端接受（不被接受就是 key 本身的问题，与模型名无关）；
2. 应该用哪个 base_url（国内站 / 国际站，两家的 key 不通用）；
3. 当前默认模型名是否真的存在（写错模型名会浪费一次重试）。

用法：MINIMAX_API_KEY=xxx python samples/probe_real.py
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

KEY = os.environ.get("MINIMAX_API_KEY", "").strip()
HOSTS = [
    ("国际站 api.minimaxi.com", "https://api.minimaxi.com/v1"),
    ("国内站 api.minimax.chat", "https://api.minimax.chat/v1"),
]
# 2026-10-30 实测：M2 及 M2.x 全部已下线（2069），仅 M3/M3.1-Flash 可用。
CANDIDATE_MODELS = ["MiniMax-M3.1-Flash", "MiniMax-M3", "MiniMax-M2.7"]


def request(url: str, method: str = "GET", payload: dict | None = None, timeout: int = 45):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {KEY}")
    req.add_header("Content-Type", "application/json")
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            return int((time.monotonic() - started) * 1000), resp.status, body
    except urllib.error.HTTPError as exc:  # 4xx/5xx 仍然要把 body 读出来看原因
        body = exc.read().decode("utf-8", "replace")
        return int((time.monotonic() - started) * 1000), exc.code, body
    except Exception as exc:  # noqa: BLE001 - 网络层错误要原样报告
        return int((time.monotonic() - started) * 1000), -1, f"{type(exc).__name__}: {exc}"


def main() -> None:
    if not KEY:
        print("未设置 MINIMAX_API_KEY，无法探测。")
        return

    print(f"key 长度={len(KEY)} 前缀={KEY[:6]} 后缀={KEY[-4:]}")
    print("=" * 60)

    good_host: str | None = None
    for label, base in HOSTS:
        ms, status, body = request(f"{base}/models")
        ok = status == 200
        print(f"[models] {label}: HTTP {status}  {ms} ms")
        if ok:
            good_host = base
            try:
                ids = [m["id"] for m in json.loads(body).get("data", [])]
                print(f"         可用模型 {len(ids)} 个，前 20 个：")
                for mid in ids[:20]:
                    marker = "  <== 默认模型名命中" if mid in CANDIDATE_MODELS else ""
                    print(f"           - {mid}{marker}")
                missing = [m for m in CANDIDATE_MODELS if m not in ids]
                print(f"         候选模型中不在列表里的：{missing or '无'}")
            except Exception as exc:  # noqa: BLE001
                print(f"         响应无法解析: {exc}\n         {body[:300]}")
        else:
            print(f"         {body[:220]}")
        print("-" * 60)

    if good_host is None:
        print("结论：两个域名都没通，key 很可能无效或账号未开通 API 调用。")
        return

    print(f"选定 {good_host}，开始最小 chat 调用验证：")
    # 注意：MiniMax 即使模型不存在也会回 HTTP 200，真正的结果藏在
    # body 里的 base_resp.status_code（非 0 即为失败）。只看 HTTP 状态会误判。
    _, _, body = request(f"{good_host}/models")
    model_ids = [m["id"] for m in json.loads(body).get("data", [])]
    working: list[tuple[str, int, str]] = []
    for model in model_ids:
        ms, status, body = request(
            f"{good_host}/text/chatcompletion_v2",
            "POST",
            {
                "model": model,
                "messages": [{"role": "user", "content": "回复两个字：可用"}],
                "max_tokens": 16,
            },
        )
        code, msg, snippet = None, "", ""
        try:
            parsed = json.loads(body)
            base = parsed.get("base_resp", {})
            code = base.get("status_code")
            msg = base.get("status_msg", "")
            if not msg and "error" in parsed:
                msg = str(parsed["error"])[:120]
            snippet = (parsed.get("choices") or [{}])[0].get("message", {}).get("content", "")[:40]
        except Exception:  # noqa: BLE001
            msg = body[:120]
        ok = status == 200 and code == 0
        flag = "OK  " if ok else "FAIL"
        print(f"  [{flag}] {model:24s} HTTP{status} code={code} {ms:5d}ms {snippet or msg}")
        if ok:
            working.append((model, ms, snippet))

    print("=" * 60)
    if working:
        best = working[0]
        print(f"结论：可用模型 {len(working)}/{len(model_ids)} 个，推荐用 `{best[0]}`")
        print(f"      实测响应 {best[1]} ms，回话 {best[2]!r}")
        print(f"      切换到它：设置环境变量 MINIMAX_MODEL={best[0]}")
        print("      全部可用模型：" + ", ".join(m for m, _, _ in working))
    else:
        print("结论：所有模型都调用失败，需要核对平台上的计费/开通状态。")


if __name__ == "__main__":
    main()
