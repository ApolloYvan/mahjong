"""S2 关卡：纯 Python 推理 vs torch 前向的最大绝对差 <1e-5；单次推理耗时（性能核/能效核各跑一次）。

    python3 tools/train/verify_infer.py --smoke
    caffeinate -i nice -n 15 python3 tools/train/verify_infer.py --label perf
    caffeinate -i nice -n 15 taskpolicy -c background python3 tools/train/verify_infer.py --label eff

1. 精度：同一份**导出文件**（float32 舍入后的权重）分别用 ``mj/nn/infer.py``（纯 Python，float64 累加）和 torch
   （float64 前向）算验证集上 N 条样本的全部 logit（D/R/H 头 + 价值），报告最大绝对差。torch 用 float64 是为了让对比只反映
   "实现差异"，不混入 float32 累加误差；线上用的就是纯 Python 这一份。
2. 耗时：真实快照（回放对局，验证房间）上 ``mj.nn.policy.nn_action``（编码 + 合法掩码 + 前向 + 选动作的端到端）单次耗时 p50/p99/max，
   以及只算前向的耗时；``--gate-ms``（默认 50）是 p99 验收线（能效核）。任一关卡不过，退出码非零。
"""
import argparse
import glob
import json
import os
import random
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "tools", "train"))
os.environ.setdefault("MJ_WEIGHTS_NO_FILE", "1")

from mj.nn import encode as E  # noqa: E402
from mj.nn import infer  # noqa: E402

CACHE = os.path.join(ROOT, "tools", ".cache", "train")


def pct(v, p):
    v = sorted(v)
    return v[min(len(v) - 1, int(len(v) * p))] if v else 0.0


def torch_logits(obj, samples):
    import torch
    t = {k: infer._unb64(v) for k, v in obj["tensors"].items()}
    sh = obj["shapes"]

    def mat(name):
        return torch.tensor(list(t[name]), dtype=torch.float64).view(*sh[name])

    w1, b1 = mat("w1"), mat("b1")
    w2, b2 = mat("w2"), mat("b2")
    res = []
    with torch.no_grad():
        for idx, val in samples:
            x = torch.zeros(len(w1), dtype=torch.float64)
            x[torch.tensor(idx)] = torch.tensor(val, dtype=torch.float64)
            h = torch.relu(x @ w1 + b1)
            h = torch.relu(h @ w2 + b2)
            out = {k: (h @ mat("w" + k) + mat("b" + k)).tolist() for k in ("d", "r", "h", "v")}
            out["v"] = out["v"][0]
            res.append(out)
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=None)
    ap.add_argument("--parts", default=None)
    ap.add_argument("--n-precision", type=int, default=300)
    ap.add_argument("--n-latency", type=int, default=400)
    ap.add_argument("--gate-ms", type=float, default=50.0)
    ap.add_argument("--label", default="perf")
    ap.add_argument("--max-minutes", type=float, default=None)
    ap.add_argument("--skip-latency", action="store_true", help="只验精度（非 macOS：实战测速只在 macOS 跑）")
    ap.add_argument("--smoke", action="store_true")
    args = ap.parse_args()
    model = args.model or (os.path.join(CACHE, "nn_policy_smoke.json") if args.smoke else os.path.join(ROOT, "models", "nn_policy.json"))
    parts = args.parts or os.path.join(CACHE, "parts_s2_smoke" if args.smoke else "parts_s2")
    if not os.path.exists(model):
        print("没有模型文件 %s（先跑 train_s2.py）" % model)
        sys.exit(2)
    os.environ["MJ_NN_POLICY_PATH"] = model
    from mj.nn import policy
    started = time.time()
    deadline = started + args.max_minutes * 60 if args.max_minutes else None
    with open(model, encoding="utf-8") as f:
        obj = json.load(f)
    net = infer.Net(obj)
    # 1. 精度
    files = sorted(glob.glob(os.path.join(parts, "val", "*.jsonl")))
    random.Random(0).shuffle(files)
    samples = []
    for path in files:
        with open(path, encoding="utf-8") as f:
            for line in f:
                r = json.loads(line)
                samples.append((r["i"], r["v"]))
                if len(samples) >= args.n_precision:
                    break
        if len(samples) >= args.n_precision:
            break
    ref = torch_logits(obj, samples)
    max_diff = 0.0
    for (idx, val), r in zip(samples, ref):
        out = net.forward(idx, val)
        for k in ("d", "r", "h"):
            max_diff = max(max_diff, max(abs(a - b) for a, b in zip(out[k], r[k])))
        max_diff = max(max_diff, abs(out["v"] - r["v"]))
    if args.skip_latency:
        print("=== verify_infer [%s]（仅精度） ===" % args.label)
        print("精度：%d 条样本，纯 Python vs torch(float64) 最大绝对差 %.3e（验收线 1e-5）%s" % (
            len(samples), max_diff, "通过" if max_diff < 1e-5 else "**未通过**"))
        sys.exit(0 if max_diff < 1e-5 else 1)
    # 2. 耗时
    from build_dataset import iter_decisions
    from mining_common import discover_files
    allf = discover_files()
    random.Random(1).shuffle(allf)
    snaps = []
    for path in allf:
        if len(snaps) >= args.n_latency or (deadline and time.time() > deadline):
            break
        for rec in iter_decisions(path, keep=0.05, seed=1):
            snaps.append((rec["snapshot"], rec["ctx"]))
            if len(snaps) >= args.n_latency:
                break
    e2e, fwd, fallbacks = [], [], 0
    for snap, ctx in snaps:
        t = time.perf_counter()
        action, log = policy.nn_action(snap, ctx, net=net, budget_s=10.0)
        e2e.append((time.perf_counter() - t) * 1000)
        fallbacks += action is None
        idx, val = E.encode_sparse(snap, ctx)
        t = time.perf_counter()
        net.forward(idx, val)
        fwd.append((time.perf_counter() - t) * 1000)
    ok_prec = max_diff < 1e-5
    ok_lat = pct(e2e, 0.99) <= args.gate_ms
    print("=== verify_infer [%s]（模型 %s，%.1f MB，%.0fs） ===" % (args.label, os.path.basename(model),
                                                              os.path.getsize(model) / 1e6, time.time() - started))
    print("精度：%d 条样本，纯 Python vs torch(float64) 全部 logit 最大绝对差 %.3e（验收线 1e-5）%s" % (
        len(samples), max_diff, "通过" if ok_prec else "**未通过**"))
    print("端到端 nn_action 耗时（%d 个真实快照）：p50 %.1fms p90 %.1fms p99 %.1fms max %.1fms；回落 %d 次（%s）" % (
        len(e2e), pct(e2e, 0.5), pct(e2e, 0.9), pct(e2e, 0.99), max(e2e) if e2e else 0.0, fallbacks, "无合法动作等"))
    print("只算前向：p50 %.1fms p99 %.1fms；验收线 p99<=%.0fms：%s" % (
        pct(fwd, 0.5), pct(fwd, 0.99), args.gate_ms, "通过" if ok_lat else "**未通过**"))
    sys.exit(0 if ok_prec and ok_lat else 1)


if __name__ == "__main__":
    main()
