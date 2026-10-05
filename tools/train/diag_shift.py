"""S2 诊断：训练/实战分布偏差。网络与生产的一致率，在"数据集快照"（高手决策的回放快照）和"arena 快照"
（生产自己打 arena 对局时各座位的真实决策快照）上各算一遍，差距 >5pp 就说明编码/状态分布有偏差。

    python3 tools/train/diag_shift.py --max-minutes 0.8        # 默认采样小，<1 分钟
    caffeinate -i nice -n 15 python3 tools/train/diag_shift.py --n 1500 --max-minutes 20

两边都拿"生产在同一快照上的动作"当参照（不用高手标签），口径完全一致：
- 数据集快照：验证房间里高手的决策点（``iter_decisions``，与 S2 训练/验证同一来源）；
- arena 快照：``Arena2Match`` 四个座位都用生产，逐决策记录快照 + 生产动作 + bot 按局记录的 ctx（ChainTracker）。
另外做敏感性检查：把 ctx（飘数/链含杠）置零后一致率变多少；按头、按有/无财神分层；列出差距最大的动作类别。
"""
import argparse
import json
import os
import random
import sys
import time
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "tools", "train"))
os.environ.setdefault("MJ_WEIGHTS_NO_FILE", "1")

import arena2  # noqa: E402
from build_dataset import iter_decisions, room_of, split_of  # noqa: E402
from eval_heads import action_class, head_class  # noqa: E402
from mining_common import discover_files  # noqa: E402
from mj.bot import choose_action  # noqa: E402
from mj.fit import frozen_file_weights  # noqa: E402
from mj.mc.chain_tracker import ChainTracker  # noqa: E402
from mj.nn import encode as E  # noqa: E402
from mj.nn import policy  # noqa: E402
from mj.tiles import JOKER  # noqa: E402

CACHE = os.path.join(ROOT, "tools", ".cache", "train")


class RecordingMatch(arena2.Arena2Match):
    """四个座位都走生产；记录 (快照, ctx, 生产动作)。"""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.records = []

    def _choose_fn(self, seat, snapshot):
        tracker = self._trackers.setdefault(seat, ChainTracker())
        ctx = tracker.ctx(snapshot)
        action = choose_action(snapshot, self.rules)
        tracker.observe(snapshot, action)
        self.records.append((snapshot, ctx, action))
        return action


HEADS = "DRH"


def collect_dataset(n_per_head, deadline, seed, masters):
    out = defaultdict(list)
    files = [p for p in discover_files() if split_of(room_of(os.path.basename(p))[1]) == "val"]
    random.Random(seed).shuffle(files)
    for path in files:
        if time.time() > deadline or all(len(out[h]) >= n_per_head for h in HEADS):
            break
        sel = set(masters.get(str(1 - room_of(os.path.basename(path))[1] % 2), []))
        for rec in iter_decisions(path, keep=lambda nm, ph, ac: (0.2 if ac.get("action") == "pass" else 1.0) if nm in sel else 0.0,
                                  seed=seed):
            y = E.action_to_index(rec["snapshot"], rec["action"])
            if y is None:
                continue
            head, _ = head_class(rec["snapshot"], y)   # 头由局面决定（与标签是谁的无关）
            if head in HEADS and len(out[head]) < n_per_head:
                # 参照动作 = 生产在这个快照上的动作（不是高手标签），与 arena 一侧口径一致
                out[head].append((rec["snapshot"], rec["ctx"], choose_action(rec["snapshot"], None)))
    return out


def collect_arena(n_per_head, deadline, seed):
    out = defaultdict(list)
    s = seed
    while time.time() < deadline and not all(len(out[h]) >= n_per_head for h in HEADS):
        m = RecordingMatch(s, rounds=2)
        m.play()
        for snap, ctx, action in m.records:
            y = E.action_to_index(snap, action)
            if y is None:
                continue
            head, _ = head_class(snap, y)
            if head in HEADS and len(out[head]) < n_per_head:
                out[head].append((snap, ctx, action))
        s += 1
    return out


def agreement(items, net, label):
    """网络 vs 生产的一致率；ctx 置零后的一致率；按有/无财神分层；返回 dict。"""
    res = defaultdict(Counter)
    for head, rows in items.items():
        for snap, ctx, action in rows:
            y = E.action_to_index(snap, action)
            if y is None:
                continue
            _, cls = head_class(snap, y)
            prod_cls = cls   # 参照 = 生产动作本身
            for tag, c in (("ctx", ctx), ("noctx", {})):
                act, log = policy.nn_action(snap, c, net=net, budget_s=10.0)
                ok = action_class(snap, head, act) == prod_cls
                res[(head, tag)]["n"] += 1
                res[(head, tag)]["ok"] += ok
                if tag == "ctx":
                    if head == "R" and prod_cls != E.A_PASS:
                        res[(head, "非过")]["n"] += 1
                        res[(head, "非过")]["ok"] += ok
                    strat = "有财神" if JOKER in snap["my_hand"] else "无财神"
                    res[(head, strat)]["n"] += 1
                    res[(head, strat)]["ok"] += ok
                    if not ok:
                        res[(head, "miss_" + str(prod_cls))]["n"] += 1
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=250, help="每个头每边的样本数")
    ap.add_argument("--max-minutes", type=float, default=0.8)
    ap.add_argument("--model", default=None)
    ap.add_argument("--seed", type=int, default=3)
    ap.add_argument("--heads", default="DRH")
    args = ap.parse_args()
    global HEADS
    HEADS = args.heads
    model = args.model or os.path.join(ROOT, "models", "nn_policy.json")
    os.environ["MJ_NN_POLICY_PATH"] = model
    net, err = policy.get_net()
    if net is None:
        print("模型加载失败：%s" % err)
        sys.exit(2)
    with open(os.path.join(CACHE, "masters.json"), encoding="utf-8") as f:
        masters = json.load(f)["masters_from"]
    t0 = time.time()
    half = t0 + args.max_minutes * 30
    with frozen_file_weights():
        ds = collect_dataset(args.n, half, args.seed, masters)
        ar = collect_arena(args.n, t0 + args.max_minutes * 60, args.seed)
    r_ds, r_ar = agreement(ds, net, "ds"), agreement(ar, net, "arena")
    names = {"D": "出牌", "R": "响应", "H": "胡飘弃胡"}
    print("=== diag_shift（网络 vs 生产 一致率；%s；%.0fs） ===" % (os.path.basename(model), time.time() - t0))
    print("%-8s %-8s | %-18s | %-18s | 差(arena-数据集)" % ("头", "分层", "数据集快照", "arena 快照"))
    worst = 0.0
    for head in HEADS:
        for tag in ("ctx", "noctx", "有财神", "无财神", "非过"):
            a, b = r_ds.get((head, tag)), r_ar.get((head, tag))
            if not a or not b or not a["n"] or not b["n"]:
                continue
            pa, pb = a["ok"] / a["n"], b["ok"] / b["n"]
            if tag == "ctx":
                worst = max(worst, abs(pb - pa))
            print("%-8s %-8s | %.3f (n=%-4d)     | %.3f (n=%-4d)     | %+.1fpp" % (
                names[head], {"ctx": "全部", "noctx": "ctx置零"}.get(tag, tag), pa, a["n"], pb, b["n"], 100 * (pb - pa)))
    for head in "DRH":
        for tag, r in (("数据集", r_ds), ("arena", r_ar)):
            miss = {k[1][5:]: v["n"] for k, v in r.items() if k[0] == head and k[1].startswith("miss_")}
            if miss:
                print("  %s %s 不一致的生产动作类别(下标:次数)：%s" % (names[head], tag, dict(sorted(miss.items(), key=lambda kv: -kv[1])[:6])))
    print("最大差距 %.1fpp：%s" % (100 * worst, "**>5pp，编码/分布有偏差，需要查**" if worst > 0.05 else "<=5pp，未发现编码偏差"))


if __name__ == "__main__":
    main()
