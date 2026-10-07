"""一批房里我们和同桌强手的差距，逐项拆开（只读统计，不跑策略代码）。

    python3 tools/batch_gap_report.py --since 2026-10-06T17:10

「强手」用这批房**以外**的历史挑（≥400 局、扣牌运后每局 ≥ +0.4），避免"这批打得好所以被挑中"的选择偏差。
牌运基准 = 全语料所有玩家「同样起手（庄/闲 × 起手财神 0/1/2+ × 起手向听）」的平均分（同 luck_vs_masters）。
误差 ± 按房间聚类（同一房的局不独立）。分差拆解都是代数恒等式（中点法），各项相加 = 总差。
"""
import argparse
import json
import math
import os
import sys
from collections import Counter, defaultdict
from multiprocessing import Pool

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from baotou_funnel import recent_rooms  # noqa: E402
from mining_common import OUR_NAME, discover_files, merge_rounds, seat_names  # noqa: E402
from mj.shanten import combined_route, route_shanten, shanten  # noqa: E402
from mj.rules import baotou  # noqa: E402
from mj.tiles import to_counts  # noqa: E402

JOKER = "白"
_RECENT = set()


def _init(recent):
    _RECENT.update(recent)


def _tags(detail):
    out = set()
    for x in detail:
        for t in ("爆头", "杠开", "七对", "4个白板"):
            if t in x:
                out.add(t)
        if "飘" in x:
            out.add("财飘")
    return out


def scan(path):
    try:
        with open(path, encoding="utf-8") as source:
            game = json.load(source)
    except (OSError, ValueError):
        return None, [], []
    names = seat_names(game)
    if len(names) != 4:
        return None, [], []
    room = game.get("room_id") or ""
    deep = room in _RECENT
    info = {r.get("round_no"): r for r in game.get("rounds") or []}
    light, detail = [], []
    for rnd in merge_rounds(game):
        meta = info.get(rnd["round_no"])
        hands = rnd.get("start_hands")
        if not meta or not hands or len(hands) != 4 or rnd.get("truncated") or not all(hands):
            continue
        scores = meta.get("scores") or [0] * 4
        dealer = meta.get("dealer", rnd.get("dealer"))
        winner = None if meta.get("is_draw") else meta.get("winner")
        keys = [(s == dealer, min(hands[s].count(JOKER), 2), min(shanten(tuple(to_counts(hands[s])), 0), 5))
                for s in range(4)]
        for s in range(4):
            light.append((names[s], keys[s], scores[s]))
        if not deep:
            continue
        hand = [list(h) for h in hands]
        melds = [0] * 4
        chi, peng, gang, draws = [0] * 4, [0] * 4, [0] * 4, [0] * 4
        first_tn, wait_tn, fan, tags, win_melds = {}, {}, None, set(), None
        try:
            for ev in rnd["events"]:
                kind, seat, tile = ev["type"], ev.get("seat"), ev.get("tile")
                data = ev.get("data") or {}
                if kind == "tile_drawn":
                    hand[seat].append(tile)
                    draws[seat] += 1
                elif kind == "tile_discarded":
                    hand[seat].remove(tile)
                    if seat not in first_tn and len(hand[seat]) + 3 * melds[seat] == 13:
                        counts = tuple(to_counts(hand[seat]))
                        if route_shanten(counts, melds[seat]) == 0:
                            first_tn[seat] = draws[seat]
                            wait_tn[seat] = 34 if baotou(counts, melds[seat]) else len(combined_route(counts, melds[seat])[1])
                elif kind == "chi":
                    used = list(data.get("tiles") or [])
                    used.remove(tile)
                    for t in used:
                        hand[seat].remove(t)
                    melds[seat] += 1
                    chi[seat] += 1
                elif kind == "peng":
                    for _ in range(2):
                        hand[seat].remove(tile)
                    melds[seat] += 1
                    peng[seat] += 1
                elif kind == "gang":
                    take = {"an": 4, "ming": 3, "bu": 1}[data.get("kind")]
                    for _ in range(take):
                        hand[seat].remove(tile)
                    if data.get("kind") != "bu":
                        melds[seat] += 1
                    gang[seat] += 1
                elif kind == "round_ended":
                    if not data.get("draw"):
                        fan = data.get("fan")
                        tags = _tags(data.get("detail") or [])
                        if winner is not None:
                            win_melds = melds[winner]
        except (ValueError, KeyError, IndexError, TypeError):
            continue
        for s in range(4):
            won = winner == s
            detail.append({
                "name": names[s], "room": room, "dealer": s == dealer, "key": keys[s], "j": keys[s][1], "sh": keys[s][2],
                "score": scores[s], "won": won, "fan": fan if won else None, "tags": tags if won else set(),
                "win_draw": draws[s] if won else None, "first_tn": first_tn.get(s), "wait_tn": wait_tn.get(s),
                "chi": chi[s], "peng": peng[s], "gang": gang[s], "win_melds": win_melds if won else None,
                "opp_win": winner is not None and not won, "w_dealer": winner == dealer if winner is not None else None,
                "w_fan": fan if winner is not None and not won else None,
            })
    return room, light, detail


def _cluster(rows, f):
    """均值和房间聚类 95% 半宽。"""
    rows = [r for r in rows if f(r) is not None]
    n = len(rows)
    if n < 2:
        return None, None, n
    m = sum(f(r) for r in rows) / n
    by = defaultdict(float)
    for r in rows:
        by[r["room"]] += f(r) - m
    k = len(by)
    se = math.sqrt(sum(v * v for v in by.values()) * k / max(1, k - 1)) / n
    return m, 1.96 * se, n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True)
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--min-rounds", type=int, default=400)
    ap.add_argument("--min-skill", type=float, default=0.4)
    args = ap.parse_args()
    recent = recent_rooms(args.since)
    files = discover_files()
    light_hist, rows = [], []
    with Pool(args.jobs, initializer=_init, initargs=(recent,)) as pool:
        for done, (room, light, detail) in enumerate(pool.imap_unordered(scan, files, chunksize=8), 1):
            if room not in recent:
                light_hist += light
            rows += detail
            if done % 1000 == 0 or done == len(files):
                print("  已扫 %d / %d" % (done, len(files)), file=sys.stderr, flush=True)

    # 牌运基准与强手名单：只用这批房以外的数据
    tot, cnt = defaultdict(float), Counter()
    for _n, key, sc in light_hist:
        tot[key] += sc
        cnt[key] += 1
    base = {k: tot[k] / cnt[k] for k in tot}
    hist_n, hist_d, hist_s = Counter(), defaultdict(float), defaultdict(float)
    for name, key, sc in light_hist:
        hist_n[name] += 1
        hist_d[name] += sc - base[key]
        hist_s[name] += sc
    here = Counter(r["name"] for r in rows)
    strong = {n for n in here if n != OUR_NAME and hist_n[n] >= args.min_rounds and hist_d[n] / hist_n[n] >= args.min_skill}
    for r in rows:
        r["exp"] = base.get(r["key"], 0.0)
        r["g"] = "我们" if r["name"] == OUR_NAME else "强手" if r["name"] in strong else "其他对手"
    G = ("我们", "强手", "其他对手")
    grp = {g: [r for r in rows if r["g"] == g] for g in G}
    rooms = sorted({r["room"] for r in rows})

    print("=" * 100)
    print("样本：%d 房、%d 局（%s 之后）。强手 = 这批房以外 ≥%d 局、扣牌运后 ≥%+.1f/局的同桌对手：" % (
        len(rooms), len(grp["我们"]), args.since, args.min_rounds, args.min_skill))
    for n in sorted(strong, key=lambda x: -here[x]):
        print("    %-24s 本批 %4d 局 | 历史 %5d 局 分/局 %+.2f 扣牌运 %+.2f" % (
            n, here[n], hist_n[n], hist_s[n] / hist_n[n], hist_d[n] / hist_n[n]))
    print("  我们的历史（这批以外，含旧版本）：%d 局 分/局 %+.2f 扣牌运 %+.2f" % (
        hist_n[OUR_NAME], hist_s[OUR_NAME] / max(1, hist_n[OUR_NAME]), hist_d[OUR_NAME] / max(1, hist_n[OUR_NAME])))

    def row(label, f, fmt="%+7.2f", pct=False):
        cells = []
        for g in G:
            m, h, n = _cluster(grp[g], f)
            if m is None:
                cells.append("%18s" % "-")
            elif pct:
                cells.append("%7.1f%% ±%5.1f   " % (100 * m, 100 * h))
            else:
                cells.append((fmt + " ±%5.2f   ") % (m, h))
        print("  %-26s %s" % (label, " ".join(cells)))

    def head(title):
        print("\n" + "=" * 100 + "\n" + title + "\n" + "-" * 100)
        print("  %-26s %s" % ("", " ".join("%-18s" % g for g in G)))

    head("1. 结果与牌运（每局）")
    row("分/局", lambda r: r["score"])
    row("起手期望分（牌运）", lambda r: r["exp"])
    row("实际−期望（打法）", lambda r: r["score"] - r["exp"])
    row("胜率", lambda r: float(r["won"]), pct=True)
    row("收入/局（胡牌局得分）", lambda r: r["score"] if r["won"] else 0.0)
    row("支出/局（非胡牌局得分）", lambda r: r["score"] if not r["won"] else 0.0)
    row("做庄局占比", lambda r: float(r["dealer"]), pct=True)
    row("起手财神数", lambda r: float(r["j"]))
    row("起手向听", lambda r: float(r["sh"]))

    # 同桌配对：有强手的房里，同一局 我们 − 该强手（scan 每局按座位顺序连续产出 4 行）
    pairs = []
    seq = defaultdict(list)
    for r in rows:
        seq[r["room"]].append(r)
    for room, rs in seq.items():
        for i in range(0, len(rs), 4):
            four = rs[i:i + 4]
            us = [r for r in four if r["g"] == "我们"]
            if len(us) != 1:
                continue
            for o in four:
                if o["g"] == "强手":
                    pairs.append({"room": room, "d": us[0]["score"] - o["score"],
                                  "dl": (us[0]["score"] - us[0]["exp"]) - (o["score"] - o["exp"])})
    m, h, n = _cluster(pairs, lambda r: r["d"])
    m2, h2, _ = _cluster(pairs, lambda r: r["dl"])
    if m is not None:
        print("\n  同桌配对（同一局 我们 − 强手）：%d 对、%d 房：分差 %+.2f ±%.2f；扣牌运后 %+.2f ±%.2f" % (
            n, len({p["room"] for p in pairs}), m, h, m2, h2))

    head("2. 速度（每局）")
    row("8 摸内听牌", lambda r: float(r["first_tn"] is not None and r["first_tn"] <= 8), pct=True)
    row("本局听过牌", lambda r: float(r["first_tn"] is not None), pct=True)
    row("首次听牌在第几摸", lambda r: r["first_tn"], fmt="%7.2f")
    row("首次听牌听口数(34=爆头)", lambda r: r["wait_tn"], fmt="%7.2f")
    row("听牌后胡牌率", lambda r: float(r["won"]) if r["first_tn"] is not None else None, pct=True)
    row("胡牌在第几摸", lambda r: r["win_draw"], fmt="%7.2f")

    head("3. 胡的大小（只算胡牌局）")
    row("平均番", lambda r: r["fan"] if r["won"] and r["fan"] else None, fmt="%7.2f")
    row("每胡得分", lambda r: r["score"] if r["won"] else None)
    for t in ("爆头", "财飘", "杠开", "七对"):
        row("含%s" % t, lambda r, t=t: float(t in r["tags"]) if r["won"] else None, pct=True)
    for lo, hi, lab in ((1, 1, "1番"), (2, 3, "2番"), (4, 7, "4番"), (8, 999, "8番+")):
        row("%s 占胡" % lab, lambda r, lo=lo, hi=hi: float(lo <= (r["fan"] or 0) <= hi) if r["won"] and r["fan"] else None,
            pct=True)
    row("庄家胡占胡", lambda r: float(r["dealer"]) if r["won"] else None, pct=True)

    head("4. 支出（只算别人胡的局）")
    row("每次付分", lambda r: r["score"] if r["opp_win"] else None)
    row("付分时我是庄", lambda r: float(r["dealer"]) if r["opp_win"] else None, pct=True)
    row("赢家番数", lambda r: r["w_fan"] if r["opp_win"] and r["w_fan"] else None, fmt="%7.2f")
    row("付分局占比", lambda r: float(r["opp_win"]), pct=True)

    head("5. 吃碰杠（每局）")
    row("吃", lambda r: float(r["chi"]), fmt="%7.2f")
    row("碰", lambda r: float(r["peng"]), fmt="%7.2f")
    row("杠", lambda r: float(r["gang"]), fmt="%7.2f")
    row("胡时副露数", lambda r: r["win_melds"], fmt="%7.2f")
    for k in (0, 1, 2):
        row("胡时 %s 露 占胡" % ("2+" if k == 2 else k),
            lambda r, k=k: float(min(r["win_melds"], 2) == k) if r["won"] and r["win_melds"] is not None else None, pct=True)

    head("6. 按 庄/闲 × 起手财神 分格（分/局 | 胜率 | 平均番）")
    for d in (True, False):
        for j in (0, 1, 2):
            cells = []
            for g in G:
                sub = [r for r in grp[g] if r["dealer"] == d and r["j"] == j]
                if len(sub) < 20:
                    cells.append("%-30s" % ("n=%d" % len(sub)))
                    continue
                wins = [r for r in sub if r["won"] and r["fan"]]
                cells.append("%-30s" % ("n=%4d %+6.2f %5.1f%% %4.2f" % (
                    len(sub), sum(r["score"] for r in sub) / len(sub), 100.0 * len(wins) / len(sub),
                    sum(r["fan"] for r in wins) / max(1, len(wins)))))
            print("  %-10s %s" % ("%s %s白" % ("庄" if d else "闲", "2+" if j == 2 else j), " ".join(cells)))

    # 7. 分差拆解：我们 vs 强手，中点法恒等式
    def agg(rs):
        n = len(rs)
        w = [r for r in rs if r["won"]]
        p = len(w) / n
        inc = sum(r["score"] for r in w) / max(1, len(w))
        out = sum(r["score"] for r in rs if not r["won"]) / n
        return n, p, inc, out
    print("\n" + "=" * 100 + "\n7. 分差拆解（我们 − 强手，各项相加 = 总差；中点法）\n" + "-" * 100)
    nu, pu, iu, ou = agg(grp["我们"])
    ns, ps, is_, os_ = agg(grp["强手"]) if grp["强手"] else (0, 0, 0, 0)
    if ns:
        d_rate = (pu - ps) * (iu + is_) / 2
        d_size = (iu - is_) * (pu + ps) / 2
        d_out = ou - os_
        print("  总差 %+.2f/局 = 胡得多少 %+.2f + 每胡大小 %+.2f + 支出 %+.2f" % (
            d_rate + d_size + d_out, d_rate, d_size, d_out))
        print("    胜率 %.1f%% vs %.1f%%；每胡得分 %+.1f vs %+.1f；支出/局 %+.2f vs %+.2f" % (
            100 * pu, 100 * ps, iu, is_, ou, os_))

        # 每胡大小再拆：番数分布 vs 同番得分（庄闲构成）
        def fan_bucket(f):
            return 1 if f <= 1 else 2 if f <= 3 else 4 if f <= 7 else 8

        def per_fan(rs):
            c, s = Counter(), defaultdict(float)
            for r in rs:
                if r["won"] and r["fan"]:
                    b = fan_bucket(r["fan"])
                    c[b] += 1
                    s[b] += r["score"]
            tot_ = sum(c.values())
            return {b: c[b] / tot_ for b in c}, {b: s[b] / c[b] for b in c}
        su, vu = per_fan(grp["我们"])
        ss, vs = per_fan(grp["强手"])
        bs = sorted(set(su) | set(ss))
        mix = sum((su.get(b, 0) - ss.get(b, 0)) * (vu.get(b, vs.get(b, 0)) + vs.get(b, vu.get(b, 0))) / 2 for b in bs)
        pay = sum((vu.get(b, 0) - vs.get(b, 0)) * (su.get(b, 0) + ss.get(b, 0)) / 2 for b in bs if b in vu and b in vs)
        print("  每胡大小差 %+.1f 分 = 番数构成 %+.1f + 同番数得分（庄闲/付分人构成）%+.1f" % (iu - is_, mix, pay))
        for b in bs:
            print("      %d番档：占胡 %5.1f%% vs %5.1f%%   每胡得分 %+6.1f vs %+6.1f" % (
                b, 100 * su.get(b, 0), 100 * ss.get(b, 0), vu.get(b, float("nan")), vs.get(b, float("nan"))))
        print("  支出差 %+.2f/局：付分局占比 %.1f%% vs %.1f%%，每次付分 %+.2f vs %+.2f" % (
            ou - os_, 100 * sum(r["opp_win"] for r in grp["我们"]) / nu, 100 * sum(r["opp_win"] for r in grp["强手"]) / ns,
            sum(r["score"] for r in grp["我们"] if r["opp_win"]) / max(1, sum(r["opp_win"] for r in grp["我们"])),
            sum(r["score"] for r in grp["强手"] if r["opp_win"]) / max(1, sum(r["opp_win"] for r in grp["强手"]))))
    print("\n读法：± 是房间聚类 95% 半宽；「强手」组是很多人的合计，每人局数见开头。牌运列只扣起手，不扣摸牌运。")


if __name__ == "__main__":
    main()
