"""杠开·爆头从哪来：玄武-2346 1671 局 13 次、我们 v1.5 2949 局 3 次。逐局重放，分三步拆：

  1. 进爆头听时，手里有没有能杠的东西（碰过的刻子 / 手里暗刻）——「有料率」
  2. 在爆头听里，能杠的机会出现了多少次（摸到第 4 张 = 补杠/暗杠；别人打出第 4 张 = 明杠）
  3. 机会出现时，杠了还是直接胡/放过——「执行率」
  另外：杠之前是否已在爆头听（还是靠杠「进」爆头听）、杠的种类。

    python3 tools/gangkai_study.py                       # 玄武同桌全部房 + 我们 10/07 16:13 之后
    python3 tools/gangkai_study.py --since 2026-10-07T16:13
    python3 tools/gangkai_study.py --since 2026-10-08T08:00 --arm B     # A/B 只看 B 组（留第 4 张）
"""
import argparse
import json
import os
import sys
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tools")]

from baotou_funnel import room_first_seen  # noqa: E402
from mining_common import OUR_UID, discover_files, merge_rounds  # noqa: E402
from mj.rules import baotou  # noqa: E402
from mj.tiles import to_counts  # noqa: E402

XW = "u_380da525337c"
J = "白"
RESP = {"pass", "peng", "chi", "gang", "timeout"}


def _bt(hand, m):
    return len(hand) + 3 * m == 13 and baotou(tuple(to_counts(hand)), m)


def scan(path, who, c, trace):
    with open(path, encoding="utf-8") as f:
        g = json.load(f)
    uids = [s.get("user_id") for s in g.get("seats") or []]
    if len(uids) != 4:
        return
    group = {}
    for s, u in enumerate(uids):
        if who == "xw":
            group[s] = "玄武" if u == XW else ("我们旧版" if u == OUR_UID else "玄武房其他")
        else:
            group[s] = "我们v1.5" if u == OUR_UID else "我们房其他"
    for rnd in merge_rounds(g):
        hs = rnd.get("start_hands")
        if not hs or len(hs) != 4 or not all(hs) or rnd.get("truncated"):
            continue
        hand = [list(h) for h in hs]
        m = [0] * 4
        pengs = [set() for _ in range(4)]
        inwait = [False] * 4
        held = [False] * 4        # 本局把碰牌的第 4 张留在手里过
        bu_now = [False] * 4      # 本局摸到第 4 张当场补杠过
        entered = [False] * 4
        wall = 83
        ev = rnd["events"]
        try:
            for i, e in enumerate(ev):
                k, s, t = e["type"], e.get("seat"), e.get("tile")
                d = e.get("data") or {}
                if k == "round_ended":
                    break
                if k == "tile_drawn":
                    wall -= 1
                    x = c[group[s]]
                    if inwait[s]:
                        x["wait_draws"] += 1
                        if t != J and wall > 20 and (t in pengs[s] or hand[s].count(t) == 3):
                            x["oppA"] += 1
                            nxt = next((y for y in ev[i + 1:] if y.get("seat") == s or y["type"] == "round_ended"), None)
                            if nxt and nxt["type"] == "gang":
                                x["oppA_gang"] += 1
                            elif nxt and nxt["type"] == "round_ended" and nxt.get("seat") == s:
                                x["oppA_hu"] += 1
                    if t in pengs[s] and wall > 20 and not inwait[s]:
                        nxt = next((y for y in ev[i + 1:] if y.get("seat") == s or y["type"] == "round_ended"), None)
                        typ = nxt["type"] if nxt else None
                        x["d4"] += 1
                        if typ == "gang":
                            x["d4_bu"] += 1
                            bu_now[s] = True
                        elif typ == "tile_discarded" and nxt.get("tile") == t:
                            x["d4_disc"] += 1
                        elif typ == "tile_discarded":
                            x["d4_hold"] += 1
                            held[s] = True
                        elif typ == "round_ended":
                            x["d4_hu"] += 1
                    hand[s].append(t)
                    # ④ 摸牌后（14 张形）有没有一种杠能让剩下的牌直接成爆头听：杠完岭上必胡 = 杠开·爆头
                    if wall > 20 and not inwait[s]:
                        opts = [q for q in pengs[s] if q in hand[s]] + \
                               [q for q in set(hand[s]) if q != J and hand[s].count(q) == 4]
                        for q in opts:
                            rest = list(hand[s])
                            n_rm = 1 if q in pengs[s] else 4
                            for _ in range(n_rm):
                                rest.remove(q)
                            if _bt(rest, m[s] + (0 if n_rm == 1 else 1)):
                                x["oppC"] += 1
                                x["oppC_drawn" if q == t else "oppC_held"] += 1
                                nxt = next((y for y in ev[i + 1:] if y.get("seat") == s or y["type"] == "round_ended"), None)
                                typ = nxt["type"] if nxt else None
                                if typ == "round_ended" and nxt.get("seat") == s:
                                    x["oppC_hu"] += 1
                                elif typ == "gang":
                                    x["oppC_gang"] += 1
                                elif typ == "tile_discarded":
                                    x["oppC_disc"] += 1
                                    x["oppC_disc_q" if nxt.get("tile") == q else "oppC_disc_other"] += 1
                                if group[s] == "我们v1.5":
                                    trace.append(("④我们", os.path.basename(path)[:20], rnd["round_no"], "杠" + q,
                                                  "摸" + t, "".join(sorted(hand[s])), "m=%d" % m[s], "实际:" + str(typ),
                                                  nxt.get("tile") if nxt else ""))
                                break
                elif k == "tile_discarded":
                    hand[s].remove(t)
                    now = _bt(hand[s], m[s])
                    x = c[group[s]]
                    if now and not entered[s]:
                        entered[s] = True
                        x["enter"] += 1
                        have_p = any(p for p in pengs[s] if p != J)
                        have_t = any(hand[s].count(q) == 3 for q in set(hand[s]) if q != J)
                        x["enter_peng"] += have_p
                        x["enter_trip"] += have_t
                        x["enter_any"] += have_p or have_t
                    inwait[s] = now
                    for o in range(4):
                        if o != s and inwait[o] and t != J and hand[o].count(t) == 3 and wall > 20:
                            y = c[group[o]]
                            y["oppB"] += 1
                            took = any(z["type"] == "gang" and z.get("seat") == o
                                       for z in ev[i + 1:i + 13] if z["type"] in RESP)
                            y["oppB_gang"] += took
                elif k == "peng":
                    hand[s].remove(t)
                    hand[s].remove(t)
                    m[s] += 1
                    pengs[s].add(t)
                elif k == "chi":
                    used = list(d.get("tiles") or [])
                    used.remove(t)
                    for u in used:
                        hand[s].remove(u)
                    m[s] += 1
                elif k == "gang":
                    kind = d.get("kind")
                    pre_wait = inwait[s]
                    for _ in range({"an": 4, "ming": 3, "bu": 1}[kind]):
                        hand[s].remove(t)
                    if kind != "bu":
                        m[s] += 1
                    pengs[s].discard(t)
                    x = c[group[s]]
                    x["gang"] += 1
                    post = _bt(hand[s], m[s])
                    if post:
                        x["gang_bt"] += 1
                        x["gang_bt_pre_wait"] += pre_wait
                        x["gang_bt_" + kind] += 1
                        nxt = next((y for y in ev[i + 1:] if y["type"] in ("tile_drawn", "round_ended")), None)
                        j = ev.index(nxt) if nxt else None
                        won = j is not None and j + 1 < len(ev) and ev[j + 1]["type"] == "round_ended" \
                            and ev[j + 1].get("seat") == s
                        x["gang_bt_won"] += won
                        if group[s] in ("玄武", "我们v1.5"):
                            trace.append((group[s], os.path.basename(path)[:20], rnd["round_no"], kind, t,
                                          "已在爆头听" if pre_wait else "杠后才进爆头听",
                                          "".join(sorted(hand[s])), "m=%d" % m[s], "胡" if won else "-"))
                    inwait[s] = post
            end = next((y for y in ev if y["type"] == "round_ended"), None)
            sc = ((end or {}).get("data") or {}).get("scores") or [0] * 4
            ww = end.get("seat") if end and not (end.get("data") or {}).get("draw") else None
            fan = ((end or {}).get("data") or {}).get("fan") or 0
            for s in range(4):
                c[group[s]]["rounds"] += 1
                for tag, flag in (("H", held[s]), ("B", bu_now[s])):
                    if flag:
                        c[group[s]][tag + "n"] += 1
                        c[group[s]][tag + "won"] += ww == s
                        c[group[s]][tag + "fan"] += fan if ww == s else 0
                        c[group[s]][tag + "score"] += sc[s]
            if end and not (end.get("data") or {}).get("draw"):
                w = end.get("seat")
                det = (end.get("data") or {}).get("detail") or []
                if w is not None and 0 <= w < 4:
                    c[group[w]]["wins"] += 1
                    c[group[w]]["gk_bt"] += any("杠开" in q for q in det) and any("爆头" in q for q in det)
                    c[group[w]]["gk"] += any("杠开" in q for q in det)
        except (ValueError, KeyError, IndexError, TypeError):
            continue


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-10-07T16:13")
    ap.add_argument("--arm", default=None, help="只看我们 A/B 某一组的房（A/B/C…，按 ab_assign 日志）")
    args = ap.parse_args()
    first = room_first_seen()
    arms = None
    if args.arm:
        from latency_check import ab_labels
        arms = ab_labels(args.since[:10], None)
    c = defaultdict(Counter)
    trace = []
    for p in discover_files():
        room = os.path.basename(p).split("_r")[0]
        try:
            with open(p, encoding="utf-8") as f:
                raw = f.read()
        except OSError:
            continue
        if XW in raw and OUR_UID in raw:
            scan(p, "xw", c, trace)
        elif OUR_UID in raw and first.get(room, "") >= args.since and (arms is None or arms.get(room) == args.arm):
            scan(p, "us", c, trace)
    G = ["玄武", "玄武房其他", "我们v1.5", "我们房其他"]
    rows = [
        ("局数", lambda x: x["rounds"], "%d"),
        ("杠开/局 ‰", lambda x: 1000 * x["gk"] / max(1, x["rounds"]), "%.1f"),
        ("杠开·爆头/局 ‰", lambda x: 1000 * x["gk_bt"] / max(1, x["rounds"]), "%.1f"),
        ("杠/局", lambda x: x["gang"] / max(1, x["rounds"]), "%.3f"),
        ("进爆头听/局", lambda x: x["enter"] / max(1, x["rounds"]), "%.3f"),
        ("① 进听时有料(碰或暗刻)", lambda x: 100 * x["enter_any"] / max(1, x["enter"]), "%.1f%%"),
        ("    其中有碰过的刻子", lambda x: 100 * x["enter_peng"] / max(1, x["enter"]), "%.1f%%"),
        ("    其中手里有暗刻", lambda x: 100 * x["enter_trip"] / max(1, x["enter"]), "%.1f%%"),
        ("爆头听里摸牌次数/次进听", lambda x: x["wait_draws"] / max(1, x["enter"]), "%.2f"),
        ("② 摸到第4张(补/暗杠机会)", lambda x: x["oppA"], "%d"),
        ("    ③ 杠了", lambda x: x["oppA_gang"], "%d"),
        ("    ③ 直接胡了", lambda x: x["oppA_hu"], "%d"),
        ("② 别人打第4张(明杠机会)", lambda x: x["oppB"], "%d"),
        ("    ③ 明杠了", lambda x: x["oppB_gang"], "%d"),
        ("⑤ 摸到自己碰牌的第4张", lambda x: x["d4"], "%d"),
        ("    当场补杠/留手里/打掉/胡", lambda x: "%d/%d/%d/%d" % (x["d4_bu"], x["d4_hold"], x["d4_disc"], x["d4_hu"]), "%s"),
        ("    留手里的局: 胜率", lambda x: "%d局 %.0f%%" % (x["Hn"], 100 * x["Hwon"] / max(1, x["Hn"])), "%s"),
        ("    留手里的局: 番/胡 分/局", lambda x: "%.2f %+.1f" % (x["Hfan"] / max(1, x["Hwon"]), x["Hscore"] / max(1, x["Hn"])), "%s"),
        ("    当场补杠的局: 胜率", lambda x: "%d局 %.0f%%" % (x["Bn"], 100 * x["Bwon"] / max(1, x["Bn"])), "%s"),
        ("    当场补杠的局: 番/胡 分/局", lambda x: "%.2f %+.1f" % (x["Bfan"] / max(1, x["Bwon"]), x["Bscore"] / max(1, x["Bn"])), "%s"),
        ("④ 杠了就成爆头听的机会", lambda x: x["oppC"], "%d"),
        ("    第4张是刚摸的/早在手里", lambda x: "%d/%d" % (x["oppC_drawn"], x["oppC_held"]), "%s"),
        ("    实际:杠", lambda x: x["oppC_gang"], "%d"),
        ("    实际:直接胡", lambda x: x["oppC_hu"], "%d"),
        ("    实际:打出那张/打别的", lambda x: "%d/%d" % (x["oppC_disc_q"], x["oppC_disc_other"]), "%s"),
        ("杠后成爆头听 次数", lambda x: x["gang_bt"], "%d"),
        ("    杠前已在爆头听", lambda x: x["gang_bt_pre_wait"], "%d"),
        ("    暗/明/补", lambda x: "%d/%d/%d" % (x["gang_bt_an"], x["gang_bt_ming"], x["gang_bt_bu"]), "%s"),
        ("    岭上胡了", lambda x: x["gang_bt_won"], "%d"),
    ]
    print("%-26s" % "" + "".join("%14s" % g for g in G))
    for lab, f, fmt in rows:
        print("%-26s" % lab + "".join("%14s" % (fmt % f(c[g])) for g in G))
    print("\n=== 玄武 / 我们v1.5 每一次「杠后成爆头听」===")
    for r in trace:
        print("  ", *r)


if __name__ == "__main__":
    main()
