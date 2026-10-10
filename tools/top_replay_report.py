"""把 tools/top_replay.py 的决策点汇总成「高手 vs 我们」的差异报告。

    python3 tools/top_replay_report.py reports/top_replay_recs.jsonl > reports/top_replay_1010.txt
"""
import json
import sys
from collections import Counter, defaultdict


def pct(a, b):
    return "%5.1f%%" % (100.0 * a / b) if b else "    -"


def main():
    recs = [json.loads(l) for l in open(sys.argv[1], encoding="utf-8")]
    players = sorted({r["name"] for r in recs})
    rounds = defaultdict(list)
    for r in recs:
        rounds[(r["name"], r["room"], r["round"])].append(r)

    print("=== 1. 总体一致率（高手实际选择 = 我们生产版在同一局面的选择）===")
    print("%-14s %8s %9s %9s %9s %9s %9s" % ("", "出牌点", "出牌一致", "能胡点", "胡/不胡一致", "碰点", "吃点"))
    for p in players + ["合计"]:
        sel = [r for r in recs if p == "合计" or r["name"] == p]
        dr = [r for r in sel if r["kind"] == "draw" and r["theirs"]["action"] == "discard" and r["ours"]["action"] == "discard"]
        hu = [r for r in sel if r["kind"] == "draw" and r["can_hu"]]
        hu_agree = sum((r["theirs"]["action"] == "hu") == (r["ours"]["action"] == "hu") for r in hu)
        pe = [r for r in sel if r["kind"] == "peng"]
        ch = [r for r in sel if r["kind"] == "chi"]
        print("%-14s %8d %9s %9d %9s %9d %9d" % (p[:14], len(dr), pct(sum(r["theirs"]["tile"] == r["ours"]["tile"] for r in dr), len(dr)),
                                            len(hu), pct(hu_agree, len(hu)), len(pe), len(ch)))

    print("\n=== 2. 出牌分歧：高手那张打完 vs 我们那张打完 ===")
    dis = [r for r in recs if r.get("a_theirs")]
    cls = Counter()
    by_cls = defaultdict(list)
    for r in dis:
        a, b = r["a_theirs"], r["a_ours"]
        if a["sh"] > b["sh"]:
            c = "高手更慢（向听更多）"
        elif a["sh"] < b["sh"]:
            c = "高手更快（向听更少）"
        elif a["bt"] and not b["bt"]:
            c = "同向听·高手成爆头听"
        elif b["bt"] and not a["bt"]:
            c = "同向听·我们成爆头听"
        elif a["j"] > b["j"]:
            c = "同向听·高手多留财神"
        elif a["uk"] > b["uk"]:
            c = "同向听·高手进张更多"
        elif a["uk"] < b["uk"]:
            c = "同向听·高手进张更少"
        else:
            c = "同向听·进张一样（风格差）"
        cls[c] += 1
        by_cls[c].append(r)
    tot = len(dis)
    print("分歧 %d 次（占全部出牌点 %s）" % (tot, pct(tot, sum(1 for r in recs if r['kind'] == 'draw' and r['theirs']['action'] == 'discard'))))
    print("%-24s %6s %7s %10s %10s" % ("类型", "次数", "占比", "该局胜率", "该局番/胡"))
    for c, n in cls.most_common():
        rs = by_cls[c]
        keys = {(r["name"], r["room"], r["round"]) for r in rs}
        rr = [rounds[k][0] for k in keys]
        w = sum(x["won"] for x in rr)
        print("%-24s %6d %7s %10s %10.2f" % (c, n, pct(n, tot), pct(w, len(rr)), sum(x["fan"] for x in rr) / max(1, w)))
    agree_rounds = [v[0] for k, v in rounds.items() if not any(r.get("a_theirs") for r in v)]
    print("（对照：整局出牌全部一致的局 %d 个，胜率 %s）" % (len(agree_rounds), pct(sum(x["won"] for x in agree_rounds), len(agree_rounds))))

    print("\n=== 3. 能胡时：高手 vs 我们 ===")
    hu = [r for r in recs if r["kind"] == "draw" and r["can_hu"]]
    tab = Counter((r["theirs"]["action"], r["ours"]["action"]) for r in hu)
    for (t, o), n in tab.most_common():
        rs = [r for r in hu if (r["theirs"]["action"], r["ours"]["action"]) == (t, o)]
        w = sum(r["won"] for r in rs)
        print("  高手 %-7s 我们 %-7s %5d 次   该局最终高手胡 %s  番/胡 %.2f" % (t, o, n, pct(w, n), sum(r["fan"] for r in rs) / max(1, w)))

    print("\n=== 4. 吃碰：高手 vs 我们（按 碰完是否降向听 / 有无财神）===")
    for kind in ("peng", "chi"):
        rs = [r for r in recs if r["kind"] == kind]
        print("  %s：" % ("碰" if kind == "peng" else "吃"))
        groups = [("全部", lambda r: True)]
        if kind == "peng":
            groups += [("降向听", lambda r: r["imp"]), ("不降向听", lambda r: not r["imp"])]
        groups += [("无财神", lambda r: r["j"] == 0), ("有财神", lambda r: r["j"] > 0)]
        for lab, f in groups:
            s = [r for r in rs if f(r)]
            print("    %-8s %5d 次  高手接 %s  我们接 %s  高手接/我们不接 %4d  高手不接/我们接 %4d" % (
                lab, len(s), pct(sum(r["theirs"] for r in s), len(s)), pct(sum(r["ours"] for r in s), len(s)),
                sum(r["theirs"] and not r["ours"] for r in s), sum(r["ours"] and not r["theirs"] for r in s)))

    print("\n=== 5. 逐局回放样例：高手胡 4番+ 且有出牌分歧的局（每人最多 3 局）===")
    shown = Counter()
    for k, v in sorted(rounds.items(), key=lambda kv: -kv[1][0]["fan"]):
        name = k[0]
        if v[0]["fan"] < 4 or shown[name] >= 3 or not any(r.get("a_theirs") for r in v):
            continue
        shown[name] += 1
        print("\n## %s  %s 第%d局  %s  胡 %d番 %+d" % (name, k[1], k[2], "庄" if v[0]["d"] else "闲", v[0]["fan"], v[0]["score"]))
        for r in v:
            if r["kind"] != "draw":
                if r["kind"] in ("peng", "chi") and r["theirs"] != r["ours"]:
                    print("    [%s] 墙%2d 高手%s 我们%s" % ("碰" if r["kind"] == "peng" else "吃", r["wall"],
                                                     "接" if r["theirs"] else "不接", "接" if r["ours"] else "不接"))
                continue
            mark = "  " if r["theirs"] == r["ours"] or (r["theirs"].get("tile") == r["ours"].get("tile")
                                                         and r["theirs"]["action"] == r["ours"]["action"]) else "≠ "
            extra = ""
            if r.get("a_theirs"):
                a, b = r["a_theirs"], r["a_ours"]
                extra = "  [高手打完 向听%d 进张%d%s | 我们打完 向听%d 进张%d%s]" % (
                    a["sh"], a["uk"], " 爆头听" if a["bt"] else "", b["sh"], b["uk"], " 爆头听" if b["bt"] else "")
            print("  %s墙%2d 手 %s 摸%s → 高手 %s%s  我们 %s%s%s" % (
                mark, r["wall"], r["hand"], r["drawn"], r["theirs"]["action"], r["theirs"].get("tile") or "",
                r["ours"]["action"], r["ours"].get("tile") or "", extra))


if __name__ == "__main__":
    main()
