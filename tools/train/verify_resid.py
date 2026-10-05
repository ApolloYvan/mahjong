"""S2' 关卡：纯 Python 推理 vs torch(float64) 最大绝对差 <1e-5；耗时（特征提取 + 前向，性能核/能效核各跑一次）；
以及"起点 == 生产"（未训练的零初始化模型在真实快照上不改变生产选择）的独立复核。

    python3 tools/train/verify_resid.py --smoke
    caffeinate -i nice -n 15 python3 tools/train/verify_resid.py --label perf
    caffeinate -i nice -n 15 taskpolicy -c background python3 tools/train/verify_resid.py --label eff

耗时口径（如实说明我的理解）：验收线"能效核单次推理 p99 <= 50ms"我按**模型前向（g）**算；"特征提取"（生产已有的 nn_discard 特征 +
候选打分，K 个候选）单独报告 p50/p99，并给端到端（特征+前向+排序）p99，端到端 p99 超过 ``--gate-e2e-ms``（默认 150ms，
留给 3 秒出牌预算里生产自己的计算之外的余量）也算未通过。
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

from mj import bot  # noqa: E402
from mj.nn import cands as C  # noqa: E402
from mj.nn import infer, resid  # noqa: E402

CACHE = os.path.join(ROOT, "tools", ".cache", "train")


def pct(v, p):
    v = sorted(v)
    return v[min(len(v) - 1, int(len(v) * p))] if v else 0.0


def torch_g(obj, xs):
    import torch
    t = {k: infer._unb64(v) for k, v in obj["tensors"].items()}
    sh = obj["shapes"]
    mean = torch.tensor(obj["meta"]["mean"], dtype=torch.float64)
    std = torch.tensor(obj["meta"]["std"], dtype=torch.float64)

    def mat(n):
        return torch.tensor(list(t[n]), dtype=torch.float64).view(*sh[n])
    w1, b1, w2, b2, w3, b3 = (mat(n) for n in ("w1", "b1", "w2", "b2", "w3", "b3"))
    out = []
    with torch.no_grad():
        for x in xs:
            h = torch.relu(((torch.tensor(x, dtype=torch.float64) - mean) / std) @ w1 + b1)
            out.append(float((torch.relu(h @ w2 + b2) @ w3 + b3)[0]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=None)
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--gate-ms", type=float, default=50.0)
    ap.add_argument("--gate-e2e-ms", type=float, default=150.0)
    ap.add_argument("--label", default="perf")
    ap.add_argument("--skip-latency", action="store_true")
    ap.add_argument("--smoke", action="store_true")
    args = ap.parse_args()
    model = args.model or (os.path.join(CACHE, "nn_resid_smoke.json") if args.smoke else os.path.join(ROOT, "models", "nn_resid.json"))
    if not os.path.exists(model):
        print("没有模型文件 %s（先跑 train_resid.py）" % model)
        sys.exit(2)
    os.environ["MJ_NN_RESID_PATH"] = model
    from build_dataset import iter_decisions
    from mining_common import discover_files
    from mj.fit import frozen_file_weights
    with open(model, encoding="utf-8") as f:
        obj = json.load(f)
    net = resid.ResidNet(obj)
    files = discover_files()
    random.Random(2).shuffle(files)
    snaps = []
    started = time.time()
    for path in files:
        if len(snaps) >= args.n:
            break
        for rec in iter_decisions(path, keep=0.05, seed=2):
            s = rec["snapshot"]
            if s["phase"] == "draw" and rec["action"].get("action") == "discard":
                snaps.append((s, rec["ctx"]))
                if len(snaps) >= args.n:
                    break
    feats, fwd, e2e, vec_all, changed, bad_first = [], [], [], [], 0, 0
    with frozen_file_weights():
        for s, ctx in snaps:
            prod = bot._choose_action_production(s)
            if not prod or prod.get("action") != "discard":
                continue
            t0 = time.perf_counter()
            cs = C.production_candidates(s, prod_tile=prod["tile"], k=net.k)
            if len(cs) < 2:
                continue
            vecs = C.candidate_features(s, cs, ctx, skip_prod=True, deadline=t0 + resid.FEATURE_BUDGET_S)
            t1 = time.perf_counter()
            lg = net.advantages(vecs)
            t2 = time.perf_counter()
            feats.append((t1 - t0) * 1000)
            fwd.append((t2 - t1) * 1000)
            e2e.append((t2 - t0) * 1000)
            vec_all.extend(vecs[1:3])
            bad_first += cs[0]["tile"] != prod["tile"]
    ref = torch_g(obj, vec_all)
    max_diff = max(abs(net.g(x) - r) for x, r in zip(vec_all, ref))
    ok_prec = max_diff < 1e-5
    print("=== verify_resid [%s]（%s，%.0fs） ===" % (args.label, os.path.basename(model), time.time() - started))
    print("精度：%d 个候选向量，纯 Python g(x) vs torch(float64) 最大绝对差 %.3e（验收线 1e-5）%s；候选首位==生产选择：违例 %d" % (
        len(vec_all), max_diff, "通过" if ok_prec else "**未通过**", bad_first))
    if args.skip_latency:
        sys.exit(0 if ok_prec and not bad_first else 1)
    ok_fwd = pct(fwd, 0.99) <= args.gate_ms
    ok_e2e = pct(e2e, 0.99) <= args.gate_e2e_ms
    print("耗时（%d 个真实决策，K=%d）：特征提取 p50 %.1fms p99 %.1fms；前向 p50 %.2fms p99 %.2fms（线 %.0fms：%s）；"
          "端到端 p50 %.1fms p99 %.1fms（线 %.0fms：%s）" % (
              len(e2e), net.k, pct(feats, 0.5), pct(feats, 0.99), pct(fwd, 0.5), pct(fwd, 0.99), args.gate_ms,
              "通过" if ok_fwd else "**未通过**", pct(e2e, 0.5), pct(e2e, 0.99), args.gate_e2e_ms, "通过" if ok_e2e else "**未通过**"))
    sys.exit(0 if ok_prec and ok_fwd and ok_e2e and not bad_first else 1)


if __name__ == "__main__":
    main()
