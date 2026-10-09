"""研究某个对手（按 user_id，不按名字——改名也认得）的整体打法：采集所有和他同桌的对局，逐局重放，
把他和同一批局里的「我们」「其他对手」并排比较：起手运气、速度、吃碰取舍、财神怎么用、杠与杠开、
能胡不胡（转爆头）、飘、做庄、胡牌构成、前几张打什么。另把他的逐局明细写到 reports/player_<uid>_rounds.tsv。

只能看到我们同桌的局（平台不给别人的对局），所以样本 = 我们和他同桌过的全部房间。

    python3 tools/player_study.py --uid u_380da525337c
    python3 tools/player_study.py --uid u_380da525337c --since 2026-10-06T05:00   # 只看 v1.2 之后
    python3 tools/player_study.py --uid u_fd06550b5fb3 --since 2026-10-06T05:00   # 看我们自己（「目标」列=我们）
    python3 tools/player_study.py --uid u_a,u_b,u_c        # 一组玩家合在一起当「目标」
"""
import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from multiprocessing import Pool

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tools")]

from baotou_funnel import room_first_seen  # noqa: E402
from batch_dashboard import _accepted_in_window, _after_claim_shanten, _chi_takes, _sh  # noqa: E402
from mining_common import OUR_UID, discover_files, merge_rounds  # noqa: E402
from mj.rules import _wins_any, baotou  # noqa: E402
from mj.shanten import combined_route, route_shanten, shanten  # noqa: E402
from mj.tiles import TILE_INDEX, to_counts  # noqa: E402

JOKER = "白"
_CFG = {}


def _init(uid, since, first):
    _CFG.update(uid=uid, since=since, first=first)


def _kind(tile):
    i = TILE_INDEX.get(tile)
    if i is None:
        return "?"
    if i >= 27:
        return "字"
    r = i % 9 + 1
    return "幺九" if r in (1, 9) else ("边" if r in (2, 8) else "中")


def scan(path):
    out = []
    if _CFG["since"] and _CFG["first"].get(os.path.basename(path).split("_r")[0], "") < _CFG["since"]:
        return out           # 先按房间时间过滤，不读文件（--since 时快很多）
    try:
        with open(path, encoding="utf-8") as f:
            game = json.load(f)
    except (OSError, ValueError):
        return out
    seats = game.get("seats") or []
    uids = [s.get("user_id") for s in seats]
    targets = set(_CFG["uid"].split(","))
    if len(uids) != 4 or not targets & set(uids) or OUR_UID not in uids:
        return out
    room = game.get("room_id") or ""
    if _CFG["since"] and _CFG["first"].get(room, "") < _CFG["since"]:
        return out
    me = uids.index(OUR_UID)
    group = {s: ("目标" if uids[s] in targets else "我们" if s == me else "其他") for s in range(4)}
    info = {r.get("round_no"): r for r in game.get("rounds") or []}
    for rnd in merge_rounds(game):
        meta = info.get(rnd["round_no"])
        hands = rnd.get("start_hands")
        if not meta or not hands or len(hands) != 4 or rnd.get("truncated") or not all(hands):
            continue
        dealer = meta.get("dealer", rnd.get("dealer"))
        winner = None if meta.get("is_draw") else meta.get("winner")
        scores = meta.get("scores") or [0] * 4
        hand = [list(h) for h in hands]
        melds, draws = [0] * 4, [0] * 4
        st = {s: defaultdict(float) for s in range(4)}
        for s in range(4):
            st[s]["early"] = []
        pending, was_bt = {}, {}
        fan, detail = None, []
        events = rnd["events"]
        try:
            for i, ev in enumerate(events):
                k, s, t = ev["type"], ev.get("seat"), ev.get("tile")
                d = ev.get("data") or {}
                if k == "tile_drawn":
                    pre = tuple(to_counts(hand[s]))
                    was_bt[s] = len(hand[s]) + 3 * melds[s] == 13 and baotou(pre, melds[s])
                    hand[s].append(t)
                    draws[s] += 1
                    st[s]["jd"] += t == JOKER
                    if _wins_any(tuple(to_counts(hand[s])), melds[s]) and JOKER in hand[s]:
                        pending[s] = "piao" if was_bt[s] else "s1"
                elif k == "tile_discarded":
                    x = st[s]
                    if s in pending:
                        x[pending[s] + "_n"] += 1
                        x[pending[s] + "_dec"] += 1
                        del pending[s]
                    if len(x["early"]) < 3:
                        x["early"].append(_kind(t))
                    x["dj"] += t == JOKER
                    for o in range(4):
                        if o == s:
                            continue
                        oh, y = hand[o], st[o]
                        if t != JOKER and oh.count(t) >= 2:
                            imp = _after_claim_shanten(oh, (t, t), melds[o]) < _sh(oh, melds[o])
                            key = "peng_imp" if imp else "peng_flat"
                            y[key + "_n"] += 1
                            y[key + "_acc"] += _accepted_in_window(events, i, o, "peng")
                        if t != JOKER and o == (s + 1) % 4:
                            takes = _chi_takes(oh, t)
                            if takes:
                                imp = _after_claim_shanten(oh, tuple(takes[0]), melds[o]) < _sh(oh, melds[o])
                                key = "chi_imp" if imp else "chi_flat"
                                y[key + "_n"] += 1
                                y[key + "_acc"] += _accepted_in_window(events, i, o, "chi")
                    hand[s].remove(t)
                    if len(hand[s]) + 3 * melds[s] == 13:
                        c = tuple(to_counts(hand[s]))
                        if route_shanten(c, melds[s]) == 0:
                            bt = baotou(c, melds[s])
                            if not x["tn"]:
                                x["tn"], x["tn_draw"] = 1, draws[s]
                                x["tn_waits"] = 34 if bt else len(combined_route(c, melds[s])[1])
                            if bt and not x["bt"]:
                                x["bt"], x["bt_draw"] = 1, draws[s]
                        if JOKER in hand[s]:
                            x["held"] = 1
                elif k == "chi":
                    used = list(d.get("tiles") or [])
                    used.remove(t)
                    for u in used:
                        hand[s].remove(u)
                    melds[s] += 1
                    st[s]["chi"] += 1
                elif k == "peng":
                    hand[s].remove(t)
                    hand[s].remove(t)
                    melds[s] += 1
                    st[s]["peng"] += 1
                elif k == "gang":
                    kind = d.get("kind")
                    # 杠之前（按 13 张口径）是不是爆头听：杠开爆头的前提
                    pre = list(hand[s])
                    for _ in range({"an": 4, "ming": 3, "bu": 1}[kind]):
                        hand[s].remove(t)
                    if kind != "bu":
                        melds[s] += 1
                    st[s]["gang"] += 1
                    rest = list(hand[s])
                    if len(rest) + 3 * melds[s] == 13 or len(rest) + 3 * melds[s] == 14:
                        c = tuple(to_counts(rest[:]))
                        if len(rest) + 3 * melds[s] == 13 and baotou(c, melds[s]):
                            st[s]["gang_bt"] += 1
                    del pre
                elif k == "round_ended":
                    if not d.get("draw"):
                        fan, detail = d.get("fan"), d.get("detail") or []
                    break
        except (ValueError, KeyError, IndexError, TypeError):
            continue
        for s, kind in pending.items():
            if s == winner:
                st[s][kind + "_n"] += 1
        for s in range(4):
            x = st[s]
            won = winner == s
            tags = set()
            if won:
                for t in detail:
                    for tag in ("爆头", "杠开", "七对", "4个白板"):
                        if tag in t:
                            tags.add(tag)
                    if "飘" in t:
                        tags.add("财飘")
            early = x.pop("early")
            out.append({"g": group[s], "room": room, "file": os.path.basename(path)[:22], "round": rnd["round_no"],
                        "d": s == dealer, "j": min(hands[s].count(JOKER), 2), "j_raw": hands[s].count(JOKER),
                        "sh": shanten(tuple(to_counts(hands[s])), 0), "score": scores[s], "won": won,
                        "fan": fan if won else None, "tags": tags, "win_draw": draws[s] if won else None,
                        "win_melds": melds[s] if won else None, "melds_end": melds[s],
                        "early_honor": early.count("字") + early.count("幺九"), "early_n": len(early), **x})
    return out


def _avg(rows, f, den=None):
    if isinstance(den, str):
        n = sum(r.get(den, 0) for r in rows)
        return (sum(f(r) for r in rows) / n if n else None), int(n)
    sel = [r for r in rows if (den(r) if den else True)]
    vals = [f(r) for r in sel if f(r) is not None]
    return (sum(vals) / len(vals) if vals else None), len(vals)


M = [
    # 标签, 取值, 分母, 是否百分比
    ("分/局", lambda r: r["score"], None, False),
    ("胜率", lambda r: float(r["won"]), None, True),
    ("番/胡", lambda r: r["fan"], lambda r: r["won"] and r["fan"], False),
    ("做庄局占比", lambda r: float(r["d"]), None, True),
    ("起手财神", lambda r: float(r["j_raw"]), None, False),
    ("起手向听", lambda r: float(r["sh"]), None, False),
    ("摸到财神/局", lambda r: r.get("jd", 0), None, False),
    ("听过牌", lambda r: float(r.get("tn", 0)), None, True),
    ("首次听牌第几摸", lambda r: r.get("tn_draw"), lambda r: r.get("tn"), False),
    ("首听听口数(非爆头)", lambda r: r.get("tn_waits"), lambda r: r.get("tn") and r.get("tn_waits") != 34, False),
    ("持财神局到过爆头听", lambda r: float(r.get("bt", 0)), lambda r: r.get("held"), True),
    ("到爆头听第几摸", lambda r: r.get("bt_draw"), lambda r: r.get("bt"), False),
    ("胡在第几摸", lambda r: r["win_draw"], lambda r: r["won"], False),
    ("吃/局", lambda r: r.get("chi", 0), None, False),
    ("碰/局", lambda r: r.get("peng", 0), None, False),
    ("杠/局", lambda r: r.get("gang", 0), None, False),
    ("爆头听时开杠/局", lambda r: r.get("gang_bt", 0), None, False),
    ("碰·改善 接受率", lambda r: r.get("peng_imp_acc", 0), "peng_imp_n", True),
    ("碰·不改善 接受率", lambda r: r.get("peng_flat_acc", 0), "peng_flat_n", True),
    ("吃·改善 接受率", lambda r: r.get("chi_imp_acc", 0), "chi_imp_n", True),
    ("吃·不改善 接受率", lambda r: r.get("chi_flat_acc", 0), "chi_flat_n", True),
    ("能普通胡却不胡(转爆头)", lambda r: r.get("s1_dec", 0), "s1_n", True),
    ("爆头听能胡却飘", lambda r: r.get("piao_dec", 0), "piao_n", True),
    ("打出财神/局", lambda r: r.get("dj", 0), None, False),
    ("前3张打字牌/幺九", lambda r: r["early_honor"], "early_n", True),
    ("胡时副露数", lambda r: r["win_melds"], lambda r: r["won"], False),
    ("含爆头/胡", lambda r: float("爆头" in r["tags"]), lambda r: r["won"], True),
    ("含杠开/胡", lambda r: float("杠开" in r["tags"]), lambda r: r["won"], True),
    ("含财飘/胡", lambda r: float("财飘" in r["tags"]), lambda r: r["won"], True),
    ("含七对/胡", lambda r: float("七对" in r["tags"]), lambda r: r["won"], True),
    ("4番+/胡", lambda r: float((r["fan"] or 0) >= 4), lambda r: r["won"] and r["fan"], True),
]


def table(rows, title):
    G = ("目标", "我们", "其他")
    print("\n=== %s ===" % title)
    print("%-22s | %16s | %16s | %16s" % ("指标", *G))
    for label, f, den, pct in M:
        cells = []
        for g in G:
            v, n = _avg([r for r in rows if r["g"] == g], f, den)
            if v is None or n < 10:
                cells.append("%16s" % ("- (n=%d)" % n))
            else:
                cells.append("%16s" % (("%.1f%%" % (100 * v)) if pct else ("%+.2f" % v if label == "分/局" else "%.2f" % v))
                             + "")
        print("%-22s | %s" % (label, " | ".join(cells)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--uid", required=True)
    ap.add_argument("--since", default="")
    ap.add_argument("--jobs", type=int, default=6)
    args = ap.parse_args()
    first = room_first_seen()
    rows = []
    with Pool(args.jobs, initializer=_init, initargs=(args.uid, args.since, first)) as pool:
        for done, part in enumerate(pool.imap_unordered(scan, discover_files(), chunksize=8), 1):
            rows += part
            if done % 1000 == 0:
                print("  已扫 %d" % done, file=sys.stderr, flush=True)
    tgt = [r for r in rows if r["g"] == "目标"]
    print("目标 %s：同桌 %d 房、%d 局%s" % (args.uid, len({r["room"] for r in tgt}), len(tgt),
                                       "（%s 之后）" % args.since if args.since else ""))
    if not tgt:
        return
    table(rows, "全部局")
    table([r for r in rows if r["d"]], "做庄局")
    table([r for r in rows if not r["d"]], "不做庄局")
    for j, lab in ((0, "起手 0 财神"), (1, "起手 1 财神"), (2, "起手 2+ 财神")):
        table([r for r in rows if r["j"] == j], lab)
    fans = Counter()
    for r in tgt:
        if r["won"] and r["fan"] and r["fan"] >= 4:
            fans["·".join(sorted(r["tags"])) + " %d番" % r["fan"]] += 1
    print("\n=== 目标的 4 番以上牌型 ===")
    for k, v in fans.most_common():
        print("  %3d  %s" % (v, k))
    os.makedirs("reports", exist_ok=True)
    out = "reports/player_%s_rounds.tsv" % (args.uid if "," not in args.uid else "group%d" % len(args.uid.split(",")))
    cols = ["file", "round", "d", "j_raw", "sh", "tn_draw", "tn_waits", "bt_draw", "chi", "peng", "gang", "gang_bt",
            "s1_n", "s1_dec", "piao_n", "piao_dec", "dj", "won", "fan", "win_draw", "win_melds", "score"]
    with open(out, "w", encoding="utf-8") as f:
        f.write("\t".join(cols + ["tags"]) + "\n")
        for r in sorted(tgt, key=lambda r: (r["file"], r["round"])):
            f.write("\t".join(str(r.get(c, "")) for c in cols) + "\t" + "/".join(sorted(r["tags"])) + "\n")
    print("\n逐局明细：%s（%d 行）" % (out, len(tgt)))
    print("读法：「目标」和「我们」「其他」是同一批局、同样的牌墙环境，差值就是打法差异；n<10 的格子不显示。")


if __name__ == "__main__":
    main()
