"""E3 用：用实战 S1 触发记录拟合"等待成功率" p̂（按财神数、副露数、墙剩余分格，层级收缩），写 models/s1_phat.json。

    python3 tools/s1_fit.py            # 秒级；依赖 tools/s1_split.py 缓存的 tools/.cache/s1_split_rows.jsonl（939 次触发）

分格：g（全部）> j（财神 1 / 2+）> jm（再按副露 0 / 1 / 2+）> jmw（再按墙剩余轮数分 6 档，``mj.s1_variants.wall_bin``）。
预测时从全局率往下逐层收缩：p = (w + k*p_parent) / (n + k)，k=10——小格子向父格靠拢，没有数据的格子（比如现行 S1 没触发过的
"1 财神/0 副露"）用父格的率外推；**这是外推，不是实测**，E3 的 arena 结果要带着这个保留看。
同时写出 p* 需要的参数：L（庄/闲）= 触发后败给别家自摸时我们的付出（实测均值），ratio = 成功时最终番数/当时番数（实测均值）。
按房聚类的留一房交叉验证（每个房留出，用其余房拟合再预测）报告 Brier 分数 vs 恒预测全局率，看分格是否真有预测力。
"""
import json
import os
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from s1_split import breakeven  # noqa: E402

CACHE = os.path.join(ROOT, "tools", ".cache", "s1_split_rows.jsonl")
OUT = os.path.join(ROOT, "models", "s1_phat.json")
K = 10.0


def wall_bin(wall):
    return max(0, min(5, ((wall or 0) - 20) // 8))


def keys(r):
    j, m = min(r["jokers"], 2), min(r["melds"], 2)
    w = wall_bin(r["wall"])
    return ("g", "j%d" % j, "j%dm%d" % (j, m), "j%dm%dw%d" % (j, m, w))


def count(rows):
    lv = defaultdict(lambda: [0, 0])
    for r in rows:
        for k in keys(r):
            lv[k][0] += int(r["won"])
            lv[k][1] += 1
    return lv


def predict(lv, r):
    w0, n0 = lv["g"]
    p = (w0 + 0.5) / (n0 + 1.0)
    for k in keys(r)[1:]:
        w, n = lv.get(k, (0, 0))
        p = (w + K * p) / (n + K)
    return p


def main():
    if not os.path.exists(CACHE):
        print("没有 %s（先跑 tools/s1_split.py）" % CACHE)
        sys.exit(2)
    with open(CACHE, encoding="utf-8") as f:
        rows = [json.loads(l) for l in f]
    lv = count(rows)
    bd, bn = breakeven(rows, True), breakeven(rows, False)
    obj = {"version": 1, "shrink_k": K, "levels": {k: v for k, v in lv.items()},
           "L": {"dealer": bd["L"], "non": bn["L"]}, "ratio": (bd["ratio"] * bd["n_won"] + bn["ratio"] * bn["n_won"]) / (bd["n_won"] + bn["n_won"]),
           "meta": {"n": len(rows), "rooms": len({r["room"] for r in rows}), "p_obs": lv["g"][0] / lv["g"][1],
                    "p_star_dealer_f1": (24 + bd["L"]) / (24 * 2 + bd["L"]), "p_star_non_f1": (10 + bn["L"]) / (10 * 2 + bn["L"])}}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    # 留一房交叉验证
    by = defaultdict(list)
    for r in rows:
        by[r["room"]].append(r)
    brier_model = brier_base = 0.0
    for room, test in by.items():
        train = [r for r in rows if r["room"] != room]
        lv_t = count(train)
        p0 = (lv_t["g"][0] + 0.5) / (lv_t["g"][1] + 1.0)
        for r in test:
            brier_model += (predict(lv_t, r) - r["won"]) ** 2
            brier_base += (p0 - r["won"]) ** 2
    n = len(rows)
    print("=== s1_fit（%d 次触发，%d 个房）→ %s ===" % (n, len(by), OUT))
    print("全局成功率 %.1f%%；L 庄 %.1f / 闲 %.1f；成功时番数倍率 %.2f" % (100 * obj["meta"]["p_obs"], obj["L"]["dealer"], obj["L"]["non"], obj["ratio"]))
    print("留一房 Brier：分格模型 %.4f vs 恒预测全局率 %.4f（越小越好；差距小说明分格几乎没有预测力）" % (brier_model / n, brier_base / n))
    print("p̂ 分格（财神,副露 → n, 成功率；墙剩余档见 w0..w5，每档 8 张）：")
    for j in (1, 2):
        for m in (0, 1, 2):
            w, nn_ = lv.get("j%dm%d" % (j, m), (0, 0))
            if nn_:
                bins = " ".join("w%d:%d/%d" % (b, *(lv.get("j%dm%dw%d" % (j, m, b), (0, 0)))) for b in range(6) if lv.get("j%dm%dw%d" % (j, m, b)))
                print("  %s白/%s露 n=%-4d %5.1f%%  [%s]" % ("2+" if j == 2 else j, "2+" if m == 2 else m, nn_, 100.0 * w / nn_, bins))
    print("p* 参照（f=1,F=2f）：庄 %.1f%% 闲 %.1f%%" % (100 * obj["meta"]["p_star_dealer_f1"], 100 * obj["meta"]["p_star_non_f1"]))


if __name__ == "__main__":
    main()
