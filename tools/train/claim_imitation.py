"""吃碰模仿：用顶级高手的真实吃碰选择拟合 mj/claim_features.py 的线性打分（fc_* 权重）。

    nice -n 19 python3 tools/train/claim_imitation.py extract --uids u_a,u_b --jobs 1   # 抽样本 → reports/claim_imit_data.jsonl
    python3 tools/train/claim_imitation.py fit                                         # 拟合 + 留出房间验证 → tools/overlays/claim_imit.json

模型 = 条件 logit：一个响应窗口里「过」得分恒为 0，每种合法碰/吃法得分 = Σ fc_k·f_k，按 softmax 选。
线上用的正是同一个打分（claim_features.score，取最高分、≤0 就过），所以拟合结果直接写成 fc_* 就能上线，
不需要任何新的运行时代码。验证：按房间 1/5 留出，比较「模型 vs 我们生产版」预测高手选择的准确率。
"""
import argparse
import json
import os
import sys
import zlib
from multiprocessing import Pool

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tools")]
os.environ["MJ_WEIGHTS_NO_FILE"] = "0"

from mining_common import OUR_UID, discover_files, merge_rounds  # noqa: E402
from mj.claim_features import CL_FEATURES, option_features  # noqa: E402
from mj.responses import choose_chi, choose_peng  # noqa: E402
from mj.tiles import INDEX_TILE, TILE_INDEX  # noqa: E402

J = "白"
DATA = os.path.join(ROOT, "reports", "claim_imit_data.jsonl")
_T = set()


def _init(t):
    _T.update(t)


def _takes(hand, t):
    i = TILE_INDEX.get(t)
    out = []
    if i is None or i >= 27:
        return out
    for offs in ((-2, -1), (-1, 1), (1, 2)):
        idx = [i + x for x in offs]
        if min(idx) < 0 or max(idx) >= 27 or any(x // 9 != i // 9 for x in idx):
            continue
        names = [INDEX_TILE[x] for x in idx]
        if all(hand.count(n) >= 1 for n in names):
            out.append(tuple(names))
    return out


def scan(path):
    out = []
    try:
        g = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return out
    uids = [s.get("user_id") for s in g.get("seats") or []]
    if len(uids) != 4 or not (_T & set(uids)):
        return out
    room = os.path.basename(path).split("_r1")[0]
    info = {r.get("round_no"): r for r in g.get("rounds") or []}
    for rnd in merge_rounds(g):
        hs = rnd.get("start_hands")
        meta = info.get(rnd["round_no"]) or {}
        if not hs or len(hs) != 4 or not all(hs) or rnd.get("truncated"):
            continue
        dealer = meta.get("dealer", rnd.get("dealer"))
        hand = [list(h) for h in hs]
        melds = [[] for _ in range(4)]
        rivers = [[] for _ in range(4)]
        wall, catch = 83, None
        ev = rnd["events"]
        try:
            for i, e in enumerate(ev):
                k, s, t = e["type"], e.get("seat"), e.get("tile")
                d = e.get("data") or {}
                if k == "round_ended":
                    break
                if k == "tile_drawn":
                    wall -= 1
                    hand[s].append(t)
                    if catch == s:
                        catch = None
                elif k == "tile_discarded":
                    hand[s].remove(t)
                    rivers[s].append(t)
                    if t == J:
                        catch = s
                    if catch is not None or t == J or wall <= 4:
                        continue
                    win = [y for y in ev[i + 1:i + 13] if y["type"] in ("pass", "peng", "chi", "gang", "timeout")]
                    for o in range(4):
                        if o == s or uids[o] not in _T:
                            continue
                        opts, labels = [], []
                        mg = len(melds[o])
                        chi_n = sum(1 for m in melds[o] if m["kind"] == "chi")
                        other_claim = any(y["type"] in ("peng", "gang") and y.get("seat") != o for y in win)
                        if hand[o].count(t) >= 2:
                            opts.append((t, t))
                        if o == (s + 1) % 4 and chi_n < 2 and not other_claim:
                            opts += _takes(hand[o], t)
                        if not opts:
                            continue
                        mine = [y for y in win if y.get("seat") == o and y["type"] in ("peng", "chi", "gang")]
                        if mine and mine[0]["type"] == "gang":
                            continue
                        chosen = -1
                        if mine and mine[0]["type"] == "peng":
                            chosen = 0
                        elif mine and mine[0]["type"] == "chi":
                            used = list((mine[0].get("data") or {}).get("tiles") or [])
                            used.remove(t)
                            key = tuple(sorted(used, key=lambda x: TILE_INDEX[x]))
                            for q, op in enumerate(opts):
                                if len(set(op)) == 2 and tuple(sorted(op, key=lambda x: TILE_INDEX[x])) == key:
                                    chosen = q
                            if chosen == -1:
                                continue
                        feats = []
                        for op in opts:
                            f = option_features(hand[o], op, mg, chi_n, wall, o == dealer)
                            feats.append([f[c] for c in CL_FEATURES] if f else None)
                        if all(f is None for f in feats):
                            continue
                        snap = {"my_hand": list(hand[o]), "seat": o, "dealer": dealer,
                                "melds": [list(m) for m in melds], "discards": [list(r) for r in rivers],
                                "wall_remaining": wall, "window_tile": t, "last_discard": t,
                                "responding_seats": [o], "turn": s, "god": {}}
                        prod_peng = bool(choose_peng(dict(snap, phase="response_peng"))) if hand[o].count(t) >= 2 else False
                        prod_chi = choose_chi(dict(snap, phase="response_chi")) if o == (s + 1) % 4 else None
                        if prod_peng:
                            prod = 0
                        elif prod_chi:
                            key = tuple(sorted([x for x in prod_chi.get("tiles") or []], key=lambda x: TILE_INDEX[x]))
                            prod = next((q for q, op in enumerate(opts) if len(set(op)) == 2 and
                                         tuple(sorted(op, key=lambda x: TILE_INDEX[x])) == key), -1)
                        else:
                            prod = -1
                        out.append({"room": room, "uid": uids[o], "feats": feats, "chosen": chosen, "prod": prod,
                                    "kinds": ["peng" if len(set(op)) == 1 else "chi" for op in opts]})
                elif k == "peng":
                    hand[s].remove(t)
                    hand[s].remove(t)
                    melds[s].append({"kind": "peng", "tiles": [t] * 3})
                elif k == "chi":
                    used = list(d.get("tiles") or [])
                    melds[s].append({"kind": "chi", "tiles": list(used)})
                    used.remove(t)
                    for u in used:
                        hand[s].remove(u)
                elif k == "gang":
                    kind = d.get("kind")
                    for _ in range({"an": 4, "ming": 3, "bu": 1}[kind]):
                        hand[s].remove(t)
                    if kind != "bu":
                        melds[s].append({"kind": "gang", "tiles": [t] * 4})
        except (ValueError, KeyError, IndexError, TypeError):
            continue
    return out


def extract(args):
    targets = args.uids.split(",")
    files = [p for p in discover_files() if any(u in open(p, encoding="utf-8").read() for u in targets)]
    print("文件 %d 个" % len(files), file=sys.stderr)
    n = 0
    with open(DATA, "w", encoding="utf-8") as f, Pool(args.jobs, initializer=_init, initargs=(targets,)) as pool:
        for i, part in enumerate(pool.imap_unordered(scan, files, chunksize=4), 1):
            for r in part:
                f.write(json.dumps(r) + "\n")
                n += 1
            if i % 100 == 0:
                print("  %d/%d 文件，样本 %d" % (i, len(files), n), file=sys.stderr, flush=True)
    print("样本 %d -> %s" % (n, DATA), file=sys.stderr)


def fit(args):
    import numpy as np
    rows = [json.loads(l) for l in open(DATA, encoding="utf-8")]
    test = lambda r: zlib.crc32(r["room"].encode()) % 5 == 0  # noqa: E731
    tr = [r for r in rows if not test(r)]
    te = [r for r in rows if test(r)]
    K = len(CL_FEATURES)

    def batch(rs):
        X, mask, y = [], [], []
        m = max(len(r["feats"]) for r in rs)
        for r in rs:
            x = np.zeros((m, K))
            mk = np.zeros(m)
            for q, f in enumerate(r["feats"]):
                if f is not None:
                    x[q] = f
                    mk[q] = 1
            X.append(x)
            mask.append(mk)
            y.append(r["chosen"])
        return np.array(X), np.array(mask), np.array(y)

    Xtr, Mtr, ytr = batch(tr)
    w = np.zeros(K)
    lr, l2 = 0.5, 1e-3
    for it in range(3000):
        s = Xtr @ w                                       # (N, m)
        s = np.where(Mtr > 0, s, -1e9)
        z = np.concatenate([np.zeros((len(s), 1)), s], axis=1)   # 第 0 列 = 过
        z -= z.max(axis=1, keepdims=True)
        p = np.exp(z)
        p /= p.sum(axis=1, keepdims=True)
        tgt = np.zeros_like(p)
        tgt[np.arange(len(ytr)), ytr + 1] = 1
        g = ((p - tgt)[:, 1:, None] * Xtr).sum(axis=(0, 1)) / len(ytr) + l2 * w
        w -= lr * g
    nll = -np.log(p[np.arange(len(ytr)), ytr + 1] + 1e-12).mean()

    def predict(r):
        best, bs = -1, 0.0
        for q, f in enumerate(r["feats"]):
            if f is None:
                continue
            v = float(np.dot(w, f))
            if v > bs:
                best, bs = q, v
        return best

    def acc(rs, fn):
        exact = sum(fn(r) == r["chosen"] for r in rs) / len(rs)
        binary = sum((fn(r) >= 0) == (r["chosen"] >= 0) for r in rs) / len(rs)
        return exact, binary

    print("训练 %d 窗口 / 留出 %d 窗口；训练 NLL %.3f" % (len(tr), len(te), nll))
    for name, rs in (("留出", te), ("训练", tr)):
        me, mb = acc(rs, predict)
        pe, pb = acc(rs, lambda r: r["prod"])
        acc_rate = lambda rs2, fn: sum(fn(r) >= 0 for r in rs2) / len(rs2)  # noqa: E731
        print("  %s：模型 准确率 %.1f%%（接/过 %.1f%%）  我们生产版 %.1f%%（接/过 %.1f%%）  接受率 高手 %.1f%% 模型 %.1f%% 生产版 %.1f%%" % (
            name, 100 * me, 100 * mb, 100 * pe, 100 * pb, 100 * acc_rate(rs, lambda r: r["chosen"]),
            100 * acc_rate(rs, predict), 100 * acc_rate(rs, lambda r: r["prod"])))
    for kind in ("peng", "chi"):
        rs = [r for r in te if kind in r["kinds"]]
        me, mb = acc(rs, predict)
        pe, pb = acc(rs, lambda r: r["prod"])
        print("  留出·含%s的窗口 %d：模型 %.1f%%  生产版 %.1f%%" % ("碰" if kind == "peng" else "吃", len(rs), 100 * me, 100 * pe))
    weights = {"fc_" + c: round(float(v), 4) for c, v in zip(CL_FEATURES, w)}
    print("权重：" + "  ".join("%s=%+.2f" % (k[3:], v) for k, v in weights.items()))
    overlay = dict(weights, rule_fitted_claim_enabled=1)
    out = os.path.join(ROOT, "tools", "overlays", "claim_imit.json")
    json.dump(overlay, open(out, "w"), ensure_ascii=False, indent=1)
    print("-> %s" % out)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("extract")
    e.add_argument("--uids", required=True)
    e.add_argument("--jobs", type=int, default=1)
    sub.add_parser("fit")
    args = ap.parse_args()
    extract(args) if args.cmd == "extract" else fit(args)


if __name__ == "__main__":
    main()
