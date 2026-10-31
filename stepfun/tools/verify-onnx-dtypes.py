"""
verify-onnx-dtypes.py —— 用独立的 onnxruntime（Python）交叉验证三个权重文件

存在的理由：浏览器侧跑出来的质量数据必须能被第三方独立复 dise 证。
如果 FP16 / INT8 文件其实加载的是同一个权重，浏览器里的余弦相似度会恰好是 1.0，
仅凭浏览器自身无法证伪。这里用另一套运行时、同一批输入各跑一遍：

  - 三个文件的磁盘体积与压缩比
  - 同一输入下，FP16 / INT8 输出相对 FP32 的差异（余弦相似度 + 相对误差）

若某个文件与 FP32 输出**完全一致**（误差严格为 0），说明它内部其实没被换成对应精度。
"""
import os
import numpy as np
import onnxruntime as ort

BASE = os.path.join(os.path.dirname(__file__), "..", "web", "public", "models",
                    "Xenova", "all-MiniLM-L6-v2", "onnx")

FILES = [
    ("FP32", "model.onnx"),
    ("FP16", "model_fp16.onnx"),
    ("INT8", "model_quantized.onnx"),
]

print("=== 1. 文件体积 ===")
sizes = {}
for name, fn in FILES:
    p = os.path.join(BASE, fn)
    b = os.path.getsize(p)
    sizes[name] = b
    print(f"{name:5s} {fn:24s} {b:>12,} B  {b/1024/1024:7.2f} MB")
base = sizes["FP32"]
print("\n相对 FP32 压缩比：")
for name, _ in FILES:
    print(f"  {name:5s}: {sizes[name]/base*100:6.2f}%  省 {(1-sizes[name]/base)*100:6.2f}%")

print("\n=== 2. 同一输入下的输出差异 ===")
rng = np.random.default_rng(20261031)
seq = 24
ids = rng.integers(1000, 20000, size=(1, seq)).astype("int64")
am = np.ones((1, seq), dtype="int64")

outs = {}
for name, fn in FILES:
    opts = ort.SessionOptions()
    opts.log_severity_level = 3
    sess = ort.InferenceSession(os.path.join(BASE, fn), opts, providers=["CPUExecutionProvider"])
    names = [i.name for i in sess.get_inputs()]
    feed = {}
    for n in names:
        feed[n] = ids if "input_ids" in n else (am if "token_type" in n or "attention" in n else am)
    res = sess.run(None, feed)
    # last_hidden_state 通常是第一个输出
    hidden = res[0]
    # 复刻前端做法：mean pooling + L2 normalize
    pooled = hidden.mean(axis=1)[0]
    norm = pooled / np.linalg.norm(pooled)
    outs[name] = norm
    print(f"{name:5s} inputs={names} hidden={hidden.shape} dtype={hidden.dtype}")

ref = outs["FP32"]
print("\n=== 3. 与 FP32 的对比 ===")
for name, _ in FILES:
    if name == "FP32":
        continue
    v = outs[name]
    cos = float(np.dot(ref, v) / (np.linalg.norm(ref) * np.linalg.norm(v)))
    diff = np.abs(v - ref)
    print(
        f"{name:5s} cos={cos:.10f}  "
        f"平均绝对偏差={diff.mean():.3e}  最大绝对偏差={diff.max():.3e}  "
        f"完全一致={bool(np.array_equal(v, ref))}"
    )
