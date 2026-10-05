"""S2：模仿基线训练（torch，只在训练环境用；不能被 mj/ import）。

    python3 tools/train/train_s2.py --smoke                                   # 冒烟，<1 分钟
    caffeinate -i nice -n 15 python3 tools/train/train_s2.py --max-minutes 60 # 全量

数据：``build_s2.py`` 的分片。网络（结构必须与 ``mj/nn/infer.py`` 一致）::

    稀疏输入 --EmbeddingBag(F,H1)+b1--> ReLU --Linear(H1,H2)--> ReLU --> 头 D(35) / R(6) / H(3) / V(1)

第一层用 EmbeddingBag（对非零特征累加整行），跟纯 Python 推理的稀疏第一层是同一个运算。
损失：三个头各自的"合法集合内掩码 + 样本加权交叉熵"（样本权重 = 玩家权重 / keep 概率）+ 0.2 x 价值头 Huber（目标 /50）。
每个 epoch 在验证集（按房间切分）上报告每个头的 top-1（掩码后）、按有/无财神分层；保存检查点（可续跑）和验证损失最低的权重，
并导出 ``mj/nn/infer.py`` 格式的 JSON（默认 ``models/nn_policy.json``，体积 <5MB；冒烟写到 tools/.cache/train/）。
"""
import argparse
import glob
import json
import math
import os
import random
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.environ.setdefault("MJ_WEIGHTS_NO_FILE", "1")

import torch  # noqa: E402
import torch.nn as nn  # noqa: E402
import torch.nn.functional as F  # noqa: E402

from mj.nn import encode as E  # noqa: E402
from mj.nn import infer  # noqa: E402

CACHE = os.path.join(ROOT, "tools", ".cache", "train")
HEAD_CODE = {"D": 0, "H": 1, "R": 2}
NEG = -1e4


class PolicyNet(nn.Module):
    def __init__(self, feature_dim, h1, h2):
        super().__init__()
        self.emb = nn.EmbeddingBag(feature_dim, h1, mode="sum")
        self.b1 = nn.Parameter(torch.zeros(h1))
        self.l2 = nn.Linear(h1, h2)
        self.hd = nn.Linear(h2, 35)
        self.hr = nn.Linear(h2, 6)
        self.hh = nn.Linear(h2, 3)
        self.hv = nn.Linear(h2, 1)
        nn.init.normal_(self.emb.weight, std=0.05)

    def forward(self, idx, off, val):
        h = F.relu(self.emb(idx, off, per_sample_weights=val) + self.b1)
        h = F.relu(self.l2(h))
        return self.hd(h), self.hr(h), self.hh(h), self.hv(h).squeeze(-1)


class Data:
    """整份数据集展平成张量：稀疏特征 CSR + 标签 + 合法掩码。"""

    def __init__(self, files):
        idx, val, lens = [], [], []
        head, yd, yh, yr, w, value, joker, legal = [], [], [], [], [], [], [], []
        for path in files:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    r = json.loads(line)
                    idx.extend(r["i"])
                    val.extend(r["v"])
                    lens.append(len(r["i"]))
                    head.append(HEAD_CODE[r["head"]])
                    yd.append(r.get("yd", -1))
                    yh.append(r.get("yh", -1))
                    yr.append(r.get("yr", -1))
                    w.append(r["w"])
                    value.append(float("nan") if r["val"] is None else r["val"])
                    joker.append(1 if r["joker"] else 0)
                    m = [0] * E.N_ACTIONS
                    for a in r["legal"]:
                        m[a] = 1
                    legal.append(m)
        self.n = len(lens)
        self.idx = torch.tensor(idx, dtype=torch.int32)
        self.val = torch.tensor(val, dtype=torch.float32)
        self.len = torch.tensor(lens, dtype=torch.long)
        self.off = torch.cumsum(self.len, 0) - self.len
        self.head = torch.tensor(head)
        self.yd, self.yh, self.yr = torch.tensor(yd), torch.tensor(yh), torch.tensor(yr)
        self.w = torch.tensor(w, dtype=torch.float32)
        self.value = torch.tensor(value, dtype=torch.float32)
        self.joker = torch.tensor(joker)
        self.legal = torch.tensor(legal, dtype=torch.bool)

    def batch(self, ids):
        lens = self.len[ids]
        starts = self.off[ids]
        total = int(lens.sum())
        ends = torch.cumsum(lens, 0)
        boff = ends - lens
        pos = torch.arange(total) - torch.repeat_interleave(boff, lens)
        flat = torch.repeat_interleave(starts, lens) + pos
        return self.idx[flat].long(), boff, self.val[flat]


def masks(legal):
    """合法掩码：D 头 35 个（34 出牌 + 自己杠），R 头 6 个。"""
    md = torch.cat([legal[:, :34], legal[:, E.A_GANG_SELF:E.A_GANG_SELF + 1]], dim=1)
    mr = legal[:, [E.A_PASS, E.A_PENG, E.A_GANG_MING, E.A_CHI_LOW, E.A_CHI_MID, E.A_CHI_HIGH]]
    return md, mr


def losses(model, data, ids):
    idx, off, val = data.batch(ids)
    ld, lr_, lh, v = model(idx, off, val)
    md, mr = masks(data.legal[ids])
    ld = ld.masked_fill(~md, NEG)
    lr_ = lr_.masked_fill(~mr, NEG)
    w = data.w[ids]
    out = {}
    sel = data.yd[ids] >= 0
    if sel.any():
        out["D"] = (F.cross_entropy(ld[sel], data.yd[ids][sel], reduction="none") * w[sel]).sum() / w[sel].sum()
    sel = data.yr[ids] >= 0
    if sel.any():
        out["R"] = (F.cross_entropy(lr_[sel], data.yr[ids][sel], reduction="none") * w[sel]).sum() / w[sel].sum()
    sel = data.yh[ids] >= 0
    if sel.any():
        out["H"] = (F.cross_entropy(lh[sel], data.yh[ids][sel], reduction="none") * w[sel]).sum() / w[sel].sum()
    tv = data.value[ids]
    sel = ~torch.isnan(tv)
    if sel.any():
        out["V"] = (F.smooth_l1_loss(v[sel], tv[sel] / 50.0, reduction="none") * w[sel]).sum() / w[sel].sum()
    return out, (ld, lr_, lh)


def total_loss(out):
    return out.get("D", 0) + out.get("R", 0) + out.get("H", 0) + 0.2 * out.get("V", 0)


@torch.no_grad()
def evaluate(model, data, bs=4096):
    model.eval()
    acc = {k: [0.0, 0.0, 0.0, 0.0] for k in ("D", "R", "H", "D_joker", "D_nojoker", "R_nonpass")}   # [加权对, 加权总, 对, 总]
    lsum, wsum = 0.0, 0.0
    for s in range(0, data.n, bs):
        ids = torch.arange(s, min(s + bs, data.n))
        out, (ld, lr_, lh) = losses(model, data, ids)
        w = data.w[ids]
        wsum += float(w.sum())
        lsum += float(total_loss(out)) * float(w.sum())
        for key, logits, y in (("D", ld, data.yd[ids]), ("R", lr_, data.yr[ids]), ("H", lh, data.yh[ids])):
            sel = y >= 0
            if not sel.any():
                continue
            ok = (logits[sel].argmax(1) == y[sel]).float()
            ws = w[sel]
            acc[key][0] += float((ok * ws).sum())
            acc[key][1] += float(ws.sum())
            acc[key][2] += float(ok.sum())
            acc[key][3] += float(sel.sum())
            if key == "D":
                jk = data.joker[ids][sel] == 1
                for name, m in (("D_joker", jk), ("D_nojoker", ~jk)):
                    acc[name][0] += float((ok[m] * ws[m]).sum())
                    acc[name][1] += float(ws[m].sum())
                    acc[name][2] += float(ok[m].sum())
                    acc[name][3] += float(m.sum())
            if key == "R":
                m = y[sel] != 0
                acc["R_nonpass"][2] += float(ok[m].sum())
                acc["R_nonpass"][3] += float(m.sum())
    model.train()
    res = {k: (v[0] / v[1] if v[1] else float("nan"), v[2] / v[3] if v[3] else float("nan"), int(v[3]))
           for k, v in acc.items()}
    return lsum / max(wsum, 1e-9), res


def export(model, path, feature_dim, meta):
    sd = model.state_dict()

    def flat(t):
        return t.detach().contiguous().view(-1).tolist()

    def mat_in_major(linear_weight):   # torch Linear.weight [out,in] -> 输入维在前 [in,out]
        t = linear_weight.detach().t().contiguous()
        return list(t.shape), t.view(-1).tolist()

    h1 = sd["emb.weight"].shape[1]
    h2 = sd["l2.weight"].shape[0]
    tensors = {"w1": (list(sd["emb.weight"].shape), flat(sd["emb.weight"])), "b1": ([h1], flat(sd["b1"])),
               "w2": mat_in_major(sd["l2.weight"]), "b2": ([h2], flat(sd["l2.bias"]))}
    for key, name in (("d", "hd"), ("r", "hr"), ("h", "hh"), ("v", "hv")):
        tensors["w" + key] = mat_in_major(sd[name + ".weight"])
        tensors["b" + key] = ([sd[name + ".bias"].numel()], flat(sd[name + ".bias"]))
    obj = infer.pack(feature_dim, [h1, h2], tensors, meta)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, separators=(",", ":"))
    return os.path.getsize(path)


def fmt(res):
    def one(k, name):
        w, u, n = res[k]
        return "%s %.3f(加权)/%.3f(n=%d)" % (name, w, u, n) if n else "%s -" % name
    return "; ".join([one("D", "出牌"), one("D_joker", "有财神"), one("D_nojoker", "无财神"), one("R", "响应"),
                      "非过响应召回 %.3f(n=%d)" % (res["R_nonpass"][1], res["R_nonpass"][2]) if res["R_nonpass"][2] else "非过 -",
                      one("H", "胡飘")])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--parts", default=None, help="分片目录（含 train/ val/），缺省 parts_s2（冒烟 parts_s2_smoke）")
    ap.add_argument("--hidden", type=int, nargs=2, default=[256, 128])
    ap.add_argument("--epochs", type=int, default=20)
    ap.add_argument("--batch", type=int, default=1024)
    ap.add_argument("--lr", type=float, default=2e-3)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--max-minutes", type=float, default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--fresh", action="store_true", help="忽略已有检查点从头训练")
    ap.add_argument("--smoke", action="store_true")
    args = ap.parse_args()
    parts = args.parts or os.path.join(CACHE, "parts_s2_smoke" if args.smoke else "parts_s2")
    if args.smoke:
        args.epochs = 3
    out_path = args.out or (os.path.join(CACHE, "nn_policy_smoke.json") if args.smoke
                            else os.path.join(ROOT, "models", "nn_policy.json"))
    tag = "smoke" if args.smoke else "full"
    ckpt_path = os.path.join(CACHE, "s2_ckpt_%s.pt" % tag)
    torch.manual_seed(args.seed)
    random.seed(args.seed)
    torch.set_num_threads(int(os.environ.get("MJ_JOBS", "6")))
    t0 = time.time()
    deadline = t0 + args.max_minutes * 60 if args.max_minutes else None
    tr_files = sorted(glob.glob(os.path.join(parts, "train", "*.jsonl")))
    va_files = sorted(glob.glob(os.path.join(parts, "val", "*.jsonl")))
    if not tr_files or not va_files:
        print("没有分片 %s（先跑 build_s2.py）" % parts)
        sys.exit(2)
    train, val = Data(tr_files), Data(va_files)
    print("=== train_s2（%s） ===" % tag)
    print("数据：训练 %d / 验证 %d 条（%.0fs 载入）；头 D/H/R = %s" % (
        train.n, val.n, time.time() - t0, [int((train.head == c).sum()) for c in (0, 1, 2)]))
    model = PolicyNet(E.FEATURE_DIM, *args.hidden)
    opt = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
    start_epoch, best = 0, (float("inf"), None)
    if os.path.exists(ckpt_path) and not args.fresh:
        ck = torch.load(ckpt_path)
        if ck["hidden"] == args.hidden and ck["feature_dim"] == E.FEATURE_DIM:
            model.load_state_dict(ck["model"])
            opt.load_state_dict(ck["opt"])
            start_epoch, best = ck["epoch"], (ck["best_loss"], ck["best_state"])
            print("从检查点续跑：已完成 %d 个 epoch" % start_epoch)
    steps_per_epoch = max(1, math.ceil(train.n / args.batch))
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=args.epochs * steps_per_epoch)
    for _ in range(start_epoch * steps_per_epoch):
        sched.step()
    stopped = False
    for epoch in range(start_epoch, args.epochs):
        perm = torch.randperm(train.n)
        run, nb = 0.0, 0
        for s in range(0, train.n, args.batch):
            ids = perm[s:s + args.batch]
            out, _ = losses(model, train, ids)
            loss = total_loss(out)
            opt.zero_grad()
            loss.backward()
            opt.step()
            sched.step()
            run += float(loss)
            nb += 1
            if deadline and time.time() > deadline:
                stopped = True
                break
        vloss, res = evaluate(model, val)
        print("epoch %d/%d 训练损失 %.4f 验证损失 %.4f（%.0fs）" % (epoch + 1, args.epochs, run / max(nb, 1), vloss, time.time() - t0))
        print("  验证 top-1：%s" % fmt(res))
        if vloss < best[0]:
            best = (vloss, {k: v.clone() for k, v in model.state_dict().items()})
        torch.save({"model": model.state_dict(), "opt": opt.state_dict(), "epoch": epoch + 1, "hidden": args.hidden,
                    "feature_dim": E.FEATURE_DIM, "best_loss": best[0], "best_state": best[1]}, ckpt_path)
        if stopped:
            print("到 --max-minutes 上限，停止（检查点已存，重跑同一命令续跑）")
            break
    if best[1] is not None:
        model.load_state_dict(best[1])
    vloss, res = evaluate(model, val)
    size = export(model, out_path, E.FEATURE_DIM,
                  {"val_loss": vloss, "epochs": args.epochs, "hidden": args.hidden, "train_n": train.n, "parts": parts,
                   "val_top1": {k: v[1] for k, v in res.items()}})
    print("最优权重（验证损失 %.4f）已导出 %s（%.2f MB）" % (vloss, out_path, size / 1e6))
    print("最优验证 top-1：%s" % fmt(res))


if __name__ == "__main__":
    main()
