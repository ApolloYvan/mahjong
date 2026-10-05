"""S2'：逐候选"相对生产的优势"模型训练（torch，只在训练环境用；不能被 mj/ import）。

    python3 tools/train/train_resid.py --smoke
    python3 tools/train/train_resid.py --search tools/.cache/search/results.jsonl

    模型 g(x_i) 直接预测候选 i（i>=1，生产排名第 i）相对生产选择 P 的优势（分/局），P 自己恒为 0；决策：max g > tau 才改动，否则沿用 P。
    **不再在生产评分上加修正**。g 的最后一层零初始化——起点 g≡0，严格等于生产策略（训练前断言：未训练时 0 个样本被改动）。

数据：``search_s3.py`` 的结果（``adv``/``adv_se``：检验批上每个候选相对生产的配对差和标准误，独立于选择批，无选择偏差）。
损失：逆方差加权的均方误差（权重 1/(se^2+sigma0^2)，sigma0 防止个别 se 很小的点主导）+ 对 g 的小 L2。
按房切分（id 里文件名 -> 房间哈希，``room%10<2`` 验证）；验证集再按 id 哈希对半分：**一半调 tau，另一半报告**，避免"在同一批验证样本上又调阈值
又报增益"的乐观偏差。报告：
- 策略增益估计 = 在报告半集上，模型选了某个非 P 候选的状态里，该候选的 adv（检验批实测）之和 / 报告半集状态数，单位 分/局（含不改动的状态记 0），
  并给自助法 95% 区间、改动率、改动的状态里平均实测优势；
- 对照：理想上界（每个状态都选实测 adv 最大的候选，有选择偏差，仅作参照）。
导出 ``mj/nn/resid.py`` 格式 JSON（默认 ``models/nn_resid.json``；冒烟写 tools/.cache/train/nn_resid_smoke.json）。
"""
import argparse
import hashlib
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

import torch  # noqa: E402
import torch.nn as nn  # noqa: E402
import torch.nn.functional as F  # noqa: E402

from build_dataset import room_of  # noqa: E402
from mj.nn import cands as C  # noqa: E402
from mj.nn import resid  # noqa: E402

CACHE = os.path.join(ROOT, "tools", ".cache", "train")


class ResidModel(nn.Module):
    def __init__(self, dim, h1, h2):
        super().__init__()
        self.l1, self.l2, self.l3 = nn.Linear(dim, h1), nn.Linear(h1, h2), nn.Linear(h2, 1)
        nn.init.zeros_(self.l3.weight)
        nn.init.zeros_(self.l3.bias)
        self.register_buffer("mean", torch.zeros(dim))
        self.register_buffer("std", torch.ones(dim))

    def g(self, x):
        h = F.relu(self.l1((x - self.mean) / self.std))
        return self.l3(F.relu(self.l2(h))).squeeze(-1)

    def adv(self, x):
        """x: [n,K,D] -> [n,K] 预测优势，第 0 个（生产选择）恒为 0。"""
        a = self.g(x)
        return torch.cat([torch.zeros_like(a[:, :1]), a[:, 1:]], dim=1)


def room_split(rid):
    return "val" if room_of(rid.split(":")[0])[1] % 10 < 2 else "train"


def half_of(rid):
    return int(hashlib.md5(rid.encode()).hexdigest()[:6], 16) % 2


def load_records(path, k):
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            if "adv" not in r:
                continue          # 旧格式（只有 alt 的差）没有逐候选优势，不用
            rows.append({"id": r["id"], "split": room_split(r["id"]), "x": r["x"][:k], "adv": r["adv"][:k],
                         "se": r["adv_se"][:k]})
    return rows


def to_tensors(rows, k, dim, sigma0):
    n = len(rows)
    x = torch.zeros(n, k, dim)
    adv = torch.zeros(n, k)
    w = torch.zeros(n, k)
    valid = torch.zeros(n, k, dtype=torch.bool)
    for i, r in enumerate(rows):
        m = len(r["x"])
        x[i, :m] = torch.tensor(r["x"])
        adv[i, :m] = torch.tensor(r["adv"])
        valid[i, 1:m] = True
        w[i, :m] = 1.0 / (torch.tensor(r["se"]) ** 2 + sigma0 ** 2)
    return x, adv, w * valid, valid


def choose(pred, valid, tau):
    """每个状态：预测优势最大的非 P 候选，超过 tau 才选，否则 0（沿用生产）。"""
    best, idx = pred.masked_fill(~valid, -1e9).max(1)
    return torch.where(best > tau, idx, torch.zeros_like(idx))


def gain(pick, adv, mask):
    """选了非 P 候选的状态记其实测 adv，没改动记 0，除以 mask 内状态数。返回 (增益, 逐状态值)。"""
    sel = adv.gather(1, pick[:, None]).squeeze(1)
    return float(sel[mask].sum()) / max(int(mask.sum()), 1), sel


def boot(vals, n=2000, seed=0):
    rng = random.Random(seed)
    v = vals.tolist()
    m = len(v)
    stats = sorted(sum(v[rng.randrange(m)] for _ in range(m)) / m for _ in range(n))
    return stats[int(n * 0.025)], stats[int(n * 0.975)]


def export(model, path, tau, k, meta):
    def flat(t):
        return t.detach().contiguous().view(-1).tolist()

    def w_in_major(lin):
        t = lin.weight.detach().t().contiguous()
        return list(t.shape), t.view(-1).tolist()
    tensors = {"w1": w_in_major(model.l1), "b1": ([model.l1.out_features], flat(model.l1.bias)),
               "w2": w_in_major(model.l2), "b2": ([model.l2.out_features], flat(model.l2.bias)),
               "w3": w_in_major(model.l3), "b3": ([1], flat(model.l3.bias))}
    obj = resid.pack_resid([model.l1.out_features, model.l2.out_features], tensors, model.mean.tolist(),
                           model.std.tolist(), tau=tau, k=k, extra=meta)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, separators=(",", ":"))
    return os.path.getsize(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--search", default=os.path.join(os.environ.get("MJ_SEARCH_DIR") or os.path.join(ROOT, "tools", ".cache", "search"),
                                                     "results.jsonl"))
    ap.add_argument("--hidden", type=int, nargs=2, default=[32, 16])
    ap.add_argument("--k", type=int, default=C.K_DEFAULT)
    ap.add_argument("--epochs", type=int, default=60)
    ap.add_argument("--batch", type=int, default=256)
    ap.add_argument("--lr", type=float, default=3e-3)
    ap.add_argument("--wd", type=float, default=1e-3)
    ap.add_argument("--g-l2", type=float, default=1e-3)
    ap.add_argument("--sigma0", type=float, default=1.0, help="逆方差权重里的噪声下限（分/局）")
    ap.add_argument("--patience", type=int, default=10)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--max-minutes", type=float, default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--smoke", action="store_true")
    args = ap.parse_args()
    if args.smoke:
        args.epochs = 8
    out_path = args.out or (os.path.join(CACHE, "nn_resid_smoke.json") if args.smoke else os.path.join(ROOT, "models", "nn_resid.json"))
    torch.manual_seed(args.seed)
    random.seed(args.seed)
    torch.set_num_threads(int(os.environ.get("MJ_JOBS", "6")))
    t0 = time.time()
    deadline = t0 + args.max_minutes * 60 if args.max_minutes else None
    if not os.path.exists(args.search):
        print("没有搜索结果 %s（先跑 search_s3.py run）" % args.search)
        sys.exit(2)
    rows = load_records(args.search, args.k)
    if args.smoke:     # 冒烟状态太少，按 id 哈希对半分（正式训练按房间切分）
        for r in rows:
            r["split"] = "val" if half_of(r["id"] + "s") else "train"
    tr = [r for r in rows if r["split"] == "train"]
    va = [r for r in rows if r["split"] == "val"]
    if len(tr) < 4 or len(va) < 4:
        print("训练 %d / 验证 %d 条状态——数据太少（先多跑一些 search_s3.py run；搜索结果需要是含 adv 的新格式）" % (len(tr), len(va)))
        sys.exit(2)
    dtr, dva = to_tensors(tr, args.k, C.FEATURE_DIM, args.sigma0), to_tensors(va, args.k, C.FEATURE_DIM, args.sigma0)
    model = ResidModel(C.FEATURE_DIM, *args.hidden)
    flat = dtr[0].reshape(-1, C.FEATURE_DIM)
    model.mean.copy_(flat.mean(0))
    model.std.copy_(flat.std(0).clamp_min(1e-3))
    with torch.no_grad():
        n_change = int((choose(model.adv(dva[0]), dva[3], 0.0) != 0).sum())
    assert n_change == 0, "零初始化失败：未训练的模型改动了生产选择"
    print("=== train_resid（目标 = 相对生产的优势，分/局） ===")
    print("搜索状态：训练 %d / 验证 %d；起点检查通过（未训练 == 生产，改动 0 个）" % (len(tr), len(va)))
    adv_nonp = dva[1][dva[3]]
    masked = dva[1].masked_fill(~dva[3], -1e9)
    print("验证集非 P 候选的实测优势：均值 %+.3f 标准差 %.3f；每状态最优候选的实测优势均值 %+.3f（有选择偏差，仅参照）" % (
        float(adv_nonp.mean()), float(adv_nonp.std()), float(masked.max(1)[0].clamp_min(0).mean())))
    opt = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=args.wd)
    best, bad = (float("inf"), None), 0
    x, adv, w, valid = dtr
    for epoch in range(args.epochs):
        perm = torch.randperm(len(tr))
        for s_ in range(0, len(tr), args.batch):
            ids = perm[s_:s_ + args.batch]
            pred = model.adv(x[ids])
            mse = ((pred - adv[ids]) ** 2 * w[ids]).sum() / w[ids].sum().clamp_min(1e-9)
            reg = args.g_l2 * (model.g(x[ids]) ** 2).mean()
            opt.zero_grad()
            (mse + reg).backward()
            opt.step()
        with torch.no_grad():
            pv = model.adv(dva[0])
            vloss = float(((pv - dva[1]) ** 2 * dva[2]).sum() / dva[2].sum().clamp_min(1e-9))
        if vloss < best[0] - 1e-6:
            best, bad = (vloss, {k_: v.clone() for k_, v in model.state_dict().items()}), 0
        else:
            bad += 1
        if epoch % 10 == 0 or bad == 0:
            print("epoch %d 验证加权MSE %.4f" % (epoch + 1, vloss))
        if bad >= args.patience or (deadline and time.time() > deadline):
            break
    if best[1] is not None:
        model.load_state_dict(best[1])
    # tau：验证集一半调、另一半报告
    halves = torch.tensor([half_of(r["id"]) for r in va])
    with torch.no_grad():
        pv = model.adv(dva[0])
    tune, rep = halves == 0, halves == 1
    best_tau, best_gain = 4.0, -1e9
    for t in (0.0, 0.25, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0):
        g_, _ = gain(choose(pv, dva[3], t), dva[1], tune)
        if g_ > best_gain:
            best_tau, best_gain = t, g_
    pick = choose(pv, dva[3], best_tau)
    g_rep, sel = gain(pick, dva[1], rep)
    lo, hi = boot(sel[rep]) if int(rep.sum()) > 1 else (float("nan"), float("nan"))
    changed = (pick != 0) & rep
    ideal_pick = torch.where(masked.max(1)[0] > 0, masked.argmax(1), torch.zeros(len(va), dtype=torch.long))
    ideal, _ = gain(ideal_pick, dva[1], rep)
    size = export(model, out_path, best_tau, args.k, {"val_mse": best[0], "train_n": len(tr), "gain_report": g_rep})
    print("tau=%.2f（在验证集一半上调，调时增益 %+.3f 分/局/状态）" % (best_tau, best_gain))
    print("报告半集（%d 个状态，未参与调 tau）：策略增益估计 %+.3f 分/局/状态，95%%CI [%+.3f, %+.3f]；改动率 %.1f%%；改动的状态里平均实测优势 %+.2f" % (
        int(rep.sum()), g_rep, lo, hi, 100.0 * float(changed.sum()) / max(1, int(rep.sum())),
        float(sel[changed].mean()) if int(changed.sum()) else 0.0))
    print("参照：每个状态都选实测最优候选的上界 %+.3f（有选择偏差）；不改动 0.000" % ideal)
    print("已导出 %s（%.2f MB）" % (out_path, size / 1e6))


if __name__ == "__main__":
    main()
