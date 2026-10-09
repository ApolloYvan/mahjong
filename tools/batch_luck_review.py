"""三批对局对照：把「运气」和「打法」拆开。

    python3 tools/batch_luck_review.py reports/overnight_1009_rooms.txt

批次：
  基线 = v1.5 发布前后 10/07 16:13～10/08 07:00 的房（room_first_seen）
  A/B  = 10/08 17:00～10/09 00:30（三组 A/B 那 28 房）
  本批 = 参数里的房间清单

运气调整：每个（座位, 局）按起手条件分格 ——（是否做庄, 起手白 0/1/2+, 起手向听 ≤2/3/4/5+, 局中摸到白 0/1+）——
参照值 = 三批全部座位在同一格里的平均得分。「实际 − 参照」= 去掉起手/摸牌运气后的打法差。
"""
import glob
import json
import os
import sys
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tools")]

from baotou_funnel import room_first_seen  # noqa: E402
from mining_common import OUR_UID, merge_rounds  # noqa: E402
from room_rounds import _seat_round  # noqa: E402


def _rooms_between(first, lo, hi):
    return sorted(r for r, t in first.items() if lo <= t < hi)


def _load(rooms, batch):
    rows = []
    for r in rooms:
        for p in sorted(glob.glob(os.path.join(ROOT, "models", "events", r + "_*.json"))):
            with open(p, encoding="utf-8") as f:
                g = json.load(f)
            seats = g.get("seats") or []
            uids = [s.get("user_id") for s in seats]
            if OUR_UID not in uids or len(uids) != 4:
                continue
            names = [s.get("name") or s.get("user_id") for s in seats]
            info = {x.get("round_no"): x for x in g.get("rounds") or []}
            for rnd in merge_rounds(g):
                meta = info.get(rnd["round_no"]) or {}
                if not rnd.get("start_hands") or len(rnd["start_hands"]) != 4 or not meta.get("scores"):
                    continue
                end = next((e for e in rnd["events"] if e.get("type") == "round_ended"), None)
                d = (end or {}).get("data") or {}
                winner = None if d.get("draw") else (end or {}).get("seat")
                dealer = meta.get("dealer", d.get("dealer"))
                try:
                    xs = [_seat_round(rnd, s) for s in range(4)]
                except (ValueError, KeyError):
                    continue
                rto = Counter()
                for e in rnd["events"]:
                    if e.get("type") == "timeout" and (e.get("data") or {}).get("kind") == "response":
                        rto[e.get("seat")] += 1
                for s in range(4):
                    x = xs[s]
                    rows.append({
                        "batch": batch, "room": r, "uid": uids[s], "name": names[s], "us": uids[s] == OUR_UID,
                        "d": dealer == s, "j": min(x["j"], 2), "jd": min(x["jd"], 1),
                        "sh": min(max(x["sh"], 2), 5), "score": meta["scores"][s], "won": winner == s,
                        "fan": (d.get("fan") or 0) if winner == s else 0, "tn": x["ftn"] is not None,
                        "ftn": x["ftn"], "bt": bool(x["bt"]), "dec": x["dec"], "to": x["to"], "rto": rto[s],
                        "claims": 0 if x["claims"] == "-" else len(x["claims"]),
                    })
    return rows


def main():
    rooms_now = [l.strip() for l in open(sys.argv[1], encoding="utf-8") if l.strip()]
    first = room_first_seen()
    base = _rooms_between(first, "2026-10-07T16:13", "2026-10-08T07:00")
    ab = _rooms_between(first, "2026-10-08T09:00", "2026-10-08T16:40")
    ab = [r for r in ab if r not in rooms_now]
    rows = _load(base, "基线") + _load(ab, "A/B") + _load(rooms_now, "本批")
    key = lambda x: (x["d"], x["j"], x["sh"], x["jd"])
    ref = defaultdict(list)
    for x in rows:
        ref[key(x)].append(x["score"])
    refm = {k: sum(v) / len(v) for k, v in ref.items()}
    for x in rows:
        x["exp"] = refm[key(x)]
    print("房数：基线 %d  A/B %d  本批 %d" % (len({x['room'] for x in rows if x['batch'] == '基线'}),
                                      len({x['room'] for x in rows if x['batch'] == 'A/B'}), len(rooms_now)))

    def summary(sel):
        n = len(sel) or 1
        tn = [x for x in sel if x["tn"]]
        return {"n": len(sel), "score": sum(x["score"] for x in sel) / n, "exp": sum(x["exp"] for x in sel) / n,
                "won": sum(x["won"] for x in sel) / n, "fan": sum(x["fan"] for x in sel) / max(1, sum(x["won"] for x in sel)),
                "tn": len(tn) / n, "conv": sum(x["won"] for x in tn) / max(1, len(tn)),
                "ftn": sum(x["ftn"] for x in tn) / max(1, len(tn)), "j": sum(x["j"] for x in sel) / n,
                "d": sum(x["d"] for x in sel) / n, "to": sum(x["to"] for x in sel) / n, "rto": sum(x["rto"] for x in sel) / n,
                "dec": sum(x["dec"] for x in sel) / n, "claims": sum(x["claims"] for x in sel) / n}

    print("\n=== 1. 三批对照（每局平均）：实际分、按起手+摸白运气算的「应得分」、两者之差 = 打法 ===")
    hdr = "%-14s %6s %7s %7s %8s %6s %6s %6s %7s %6s %6s %6s %7s %7s"
    print(hdr % ("", "局数", "实际", "应得", "打法差", "胜率", "番/胡", "听牌", "听后胡率", "首听", "起手白", "做庄", "出牌超时", "吃碰次"))
    for b in ("基线", "A/B", "本批"):
        for who in ("我们", "对手"):
            sel = [x for x in rows if x["batch"] == b and x["us"] == (who == "我们")]
            s = summary(sel)
            print("%-14s %6d %+7.2f %+7.2f %+8.2f %5.1f%% %6.2f %5.1f%% %6.1f%% %6.2f %6.2f %5.1f%% %7.3f %7.2f" % (
                b + "·" + who, s["n"], s["score"], s["exp"], s["score"] - s["exp"], 100 * s["won"], s["fan"],
                100 * s["tn"], 100 * s["conv"], s["ftn"], s["j"], 100 * s["d"], s["to"], s["claims"]))

    print("\n=== 2. 本批按分组：我们 vs 对手 的打法差（实际−应得，每局）===")
    print("%-16s %8s %10s %10s %10s %10s" % ("分组", "我们局数", "我们打法差", "对手打法差", "我们听后胡率", "对手听后胡率"))
    groups = [("做庄", lambda x: x["d"]), ("不做庄", lambda x: not x["d"]),
              ("起手0白", lambda x: x["j"] == 0), ("起手1白", lambda x: x["j"] == 1), ("起手2+白", lambda x: x["j"] == 2)]
    for lab, f in groups:
        u = [x for x in rows if x["batch"] == "本批" and x["us"] and f(x)]
        o = [x for x in rows if x["batch"] == "本批" and not x["us"] and f(x)]
        su, so = summary(u), summary(o)
        print("%-16s %8d %+10.2f %+10.2f %9.1f%% %9.1f%%" % (lab, su["n"], su["score"] - su["exp"], so["score"] - so["exp"],
                                                        100 * su["conv"], 100 * so["conv"]))

    print("\n=== 3. 本批每房：我们的实际分 / 应得分 / 名次 / 网络 ===")
    tot = Counter()
    for r in rooms_now:
        sel = [x for x in rows if x["room"] == r]
        by = defaultdict(float)
        for x in sel:
            by[x["name"]] += x["score"]
        order = sorted(by, key=lambda k: -by[k])
        us = [x for x in sel if x["us"]]
        if not us:
            continue
        a, e = sum(x["score"] for x in us), sum(x["exp"] for x in us)
        tot["a"] += a
        tot["e"] += e
        myname = us[0]["name"]
        print("%s 实际 %+5d 应得 %+6.0f 打法差 %+6.0f  第%d名  出牌超时 %2d  对手：%s" % (
            r, a, e, a - e, order.index(myname) + 1, sum(x["to"] for x in us),
            " / ".join("%s%+d" % (k[:8], by[k]) for k in order if k != myname)))
    print("本批合计：实际 %+d，应得 %+.0f，打法差 %+.0f" % (tot["a"], tot["e"], tot["a"] - tot["e"]))

    print("\n=== 4. 本批对手：打法差最高的（同桌 ≥160 局）===")
    opp = defaultdict(list)
    for x in rows:
        if x["batch"] == "本批" and not x["us"]:
            opp[x["name"]].append(x)
    for name, sel in sorted(opp.items(), key=lambda kv: -sum(y["score"] - y["exp"] for y in kv[1]) / len(kv[1])):
        if len(sel) < 160:
            continue
        s = summary(sel)
        print("  %-20s %5d 局  实际 %+5.2f  打法差 %+5.2f  胜率 %4.1f%%  听后胡率 %4.1f%%" % (
            name[:20], s["n"], s["score"], s["score"] - s["exp"], 100 * s["won"], 100 * s["conv"]))


if __name__ == "__main__":
    main()
