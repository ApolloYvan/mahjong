"""差中差：强手「做庄 − 不做庄」的打法变化 vs 我们「做庄 − 不做庄」的打法变化。

2026-10-07：我们做庄时每局比强手少 1.19 分，不做庄只少 0.28；而我们的庄家/闲家出牌代码几乎一样
（tools/dealer_replay.py：持财神时 0.0~0.2% 不同）。所以要回答的是：强手做庄时打法变了什么，我们没变。
只读事件流（不跑策略代码），按起手财神 0/1/2+ 分格对齐后比较；每个指标列：
  强手 庄 / 闲 / 差      我们 庄 / 闲 / 差      差中差 = 强手差 − 我们差（≠0 说明强手做庄时有我们没有的调整）

    python3 tools/role_diff.py --since 2026-10-06T05:00
"""
import argparse
import json
import os
import sys
from collections import defaultdict
from multiprocessing import Pool

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from baotou_funnel import recent_rooms  # noqa: E402
from batch_dashboard import _accepted_in_window, _after_claim_shanten, _chi_takes, _sh  # noqa: E402
from dealer_gap import STRONG  # noqa: E402
from mining_common import OUR_NAME, discover_files, merge_rounds, seat_names  # noqa: E402
from mj.rules import _wins_any, baotou  # noqa: E402
from mj.shanten import combined_route, route_shanten  # noqa: E402
from mj.tiles import to_counts  # noqa: E402

JOKER = "白"
_RECENT = set()


def _init(recent):
    _RECENT.update(recent)


def _probe(path):
    try:
        with open(path, encoding="utf-8") as source:
            game = json.load(source)
    except (OSError, ValueError):
        return path, False, False
    return path, any(n in STRONG for n in seat_names(game)), (game.get("room_id") or "") in _RECENT


def scan(path):
    """每个被跟踪座位×局产出一条 dict；指标缺省为 None（不计入分母）。"""
    out = []
    try:
        with open(path, encoding="utf-8") as source:
            game = json.load(source)
    except (OSError, ValueError):
        return out
    names = seat_names(game)
    if len(names) != 4:
        return out
    room = game.get("room_id") or ""
    groups = {}
    for s, n in enumerate(names):
        if n == OUR_NAME:
            if room in _RECENT:
                groups[s] = "我们(当前)"
        elif n in STRONG:
            groups[s] = "强手"
    if not groups:
        return out
    info = {r.get("round_no"): r for r in game.get("rounds") or []}
    for rnd in merge_rounds(game):
        meta = info.get(rnd["round_no"])
        hands = rnd.get("start_hands")
        if not meta or not hands or len(hands) != 4 or rnd.get("truncated") or not all(hands):
            continue
        dealer = meta.get("dealer", rnd.get("dealer"))
        winner = None if meta.get("is_draw") else meta.get("winner")
        hand = [list(h) for h in hands]
        melds = [0] * 4
        draws = [0] * 4
        st = {s: defaultdict(float) for s in groups}
        pending = {}           # seat -> ("s1"/"piao", jokers) 摸到能胡的牌、等看下一步是胡还是打
        fan, detail = None, []
        events = rnd["events"]
        try:
            for i, ev in enumerate(events):
                kind, seat, tile = ev["type"], ev.get("seat"), ev.get("tile")
                data = ev.get("data") or {}
                if kind == "tile_drawn":
                    if seat in groups:
                        before = tuple(to_counts(hand[seat]))
                        was_bt = len(hand[seat]) + 3 * melds[seat] == 13 and baotou(before, melds[seat])
                    hand[seat].append(tile)
                    draws[seat] += 1
                    if seat in groups and _wins_any(tuple(to_counts(hand[seat])), melds[seat]) \
                            and JOKER in hand[seat]:
                        pending[seat] = "piao" if was_bt else "s1"
                elif kind == "tile_discarded":
                    if seat in pending:
                        x = st[seat]
                        x[pending[seat] + "_n"] += 1
                        x[pending[seat] + "_dec"] += 1
                        del pending[seat]
                    # 吃碰机会（被跟踪的其他座位）
                    for other in groups:
                        if other == seat:
                            continue
                        oh = hand[other]
                        x = st[other]
                        if oh.count(tile) >= 2 and tile != JOKER:
                            improves = _after_claim_shanten(oh, (tile, tile), melds[other]) < _sh(oh, melds[other])
                            k = "peng_imp" if improves else "peng_flat"
                            x[k + "_n"] += 1
                            x[k + "_acc"] += _accepted_in_window(events, i, other, "peng")
                        if other == (seat + 1) % 4 and tile != JOKER:
                            takes = _chi_takes(oh, tile)
                            if takes:
                                improves = _after_claim_shanten(oh, tuple(takes[0]), melds[other]) < _sh(oh, melds[other])
                                k = "chi_imp" if improves else "chi_flat"
                                x[k + "_n"] += 1
                                x[k + "_acc"] += _accepted_in_window(events, i, other, "chi")
                    hand[seat].remove(tile)
                    if seat in groups and len(hand[seat]) + 3 * melds[seat] == 13:
                        x = st[seat]
                        counts = tuple(to_counts(hand[seat]))
                        if route_shanten(counts, melds[seat]) == 0:
                            is_bt = baotou(counts, melds[seat])
                            if not x["tn"]:
                                x["tn"] = 1
                                x["tn_draw"] = draws[seat]
                                x["tn_joker"] = float(JOKER in hand[seat])
                                x["tn_bt"] = float(is_bt)
                                x["tn_waits"] = 34 if is_bt else len(combined_route(counts, melds[seat])[1])
                            if is_bt:
                                x["bt_ever"] = 1
                        if JOKER in hand[seat]:
                            x["held"] = 1
                elif kind == "chi":
                    used = list(data.get("tiles") or [])
                    used.remove(tile)
                    for t in used:
                        hand[seat].remove(t)
                    melds[seat] += 1
                elif kind == "peng":
                    for _ in range(2):
                        hand[seat].remove(tile)
                    melds[seat] += 1
                elif kind == "gang":
                    take = {"an": 4, "ming": 3, "bu": 1}[data.get("kind")]
                    for _ in range(take):
                        hand[seat].remove(tile)
                    if data.get("kind") != "bu":
                        melds[seat] += 1
                elif kind == "round_ended":
                    if not data.get("draw"):
                        fan, detail = data.get("fan"), data.get("detail") or []
                    break
        except (ValueError, KeyError, IndexError, TypeError):
            continue
        for s, kind_ in pending.items():          # 能胡且最后真的胡了
            if s == winner:
                st[s][kind_ + "_n"] += 1
        for s, g in groups.items():
            x = st[s]
            won = winner == s
            out.append({"g": g, "d": s == dealer, "j": min(hands[s].count(JOKER), 2), "score": (meta.get("scores") or [0] * 4)[s],
                        "won": won, "fan": fan if won else None, "bt_win": won and any("爆头" in t for t in detail),
                        "melds_end": melds[s], "win_draw": draws[s] if won else None, **x})
    return out


METRICS = (
    # (标签, 分子函数, 分母过滤) ——都按局算比例或均值
    ("分/局", lambda r: r["score"], lambda r: True),
    ("胜率", lambda r: float(r["won"]), lambda r: True),
    ("番/胡", lambda r: r["fan"], lambda r: r["won"] and r["fan"]),
    ("爆头胡/胡", lambda r: float(r["bt_win"]), lambda r: r["won"]),
    ("胡在第几摸", lambda r: r["win_draw"], lambda r: r["won"]),
    ("听过牌", lambda r: float(r.get("tn", 0)), lambda r: True),
    ("首次听牌第几摸", lambda r: r.get("tn_draw"), lambda r: r.get("tn")),
    ("首听留着财神", lambda r: r.get("tn_joker"), lambda r: r.get("tn")),
    ("首听即爆头听", lambda r: r.get("tn_bt"), lambda r: r.get("tn")),
    ("首听听口数", lambda r: r.get("tn_waits"), lambda r: r.get("tn") and not r.get("tn_bt")),
    ("本局到过爆头听", lambda r: float(r.get("bt_ever", 0)), lambda r: r.get("held")),
    ("副露数(局末)", lambda r: float(r["melds_end"]), lambda r: True),
    ("碰·改善 接受率", lambda r: r.get("peng_imp_acc", 0), "peng_imp_n"),
    ("碰·不改善 接受率", lambda r: r.get("peng_flat_acc", 0), "peng_flat_n"),
    ("吃·改善 接受率", lambda r: r.get("chi_imp_acc", 0), "chi_imp_n"),
    ("吃·不改善 接受率", lambda r: r.get("chi_flat_acc", 0), "chi_flat_n"),
    ("能普通胡却不胡(S1)", lambda r: r.get("s1_dec", 0), "s1_n"),
    ("爆头听能胡却飘", lambda r: r.get("piao_dec", 0), "piao_n"),
)


def _stat(rows, num, den):
    if isinstance(den, str):           # 机会类：Σ接受 / Σ机会
        n = sum(r.get(den, 0) for r in rows)
        return (sum(num(r) for r in rows) / n if n else None), int(n)
    sel = [r for r in rows if den(r)]
    vals = [num(r) for r in sel if num(r) is not None]
    return (sum(vals) / len(vals) if vals else None), len(vals)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-10-06T05:00")
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--strong-files", type=int, default=600, help="强手样本：从含强手的文件里随机抽这么多个（固定种子）")
    args = ap.parse_args()
    args.ours = "我们(当前)"
    recent = recent_rooms(args.since)
    rows = []
    import random
    with Pool(args.jobs, initializer=_init, initargs=(recent,)) as pool:
        probe = list(pool.imap_unordered(_probe, discover_files(), chunksize=16))
        ours = sorted(p for p, _st, rc in probe if rc)
        strong = sorted(p for p, st, rc in probe if st and not rc)
        random.Random(7).shuffle(strong)
        files = ours + strong[:args.strong_files]
        print("  我们(当前)文件 %d + 强手抽样文件 %d（共 %d 个含强手）" % (len(ours), min(len(strong), args.strong_files),
                                                              len(strong)), file=sys.stderr, flush=True)
        for done, part in enumerate(pool.imap_unordered(scan, files, chunksize=4), 1):
            rows += part
            if done % 50 == 0 or done == len(files):
                print("  已扫 %d / %d" % (done, len(files)), file=sys.stderr, flush=True)
    for jsel, jlabel in ((None, "全部"), (0, "起手 0 财神"), (1, "起手 1 财神"), (2, "起手 2+ 财神")):
        print("\n=== %s（我们 = %s）===" % (jlabel, args.ours))
        print("%-18s | %-26s | %-26s | %-8s | %s" % ("指标", "强手 庄 / 闲 / 差", "我们 庄 / 闲 / 差", "差中差", "样本(强庄/强闲/我庄/我闲)"))
        for label, num, den in METRICS:
            cells, ns = [], []
            diffs = []
            for g in ("强手", args.ours):
                vals = []
                for d in (True, False):
                    sub = [r for r in rows if r["g"] == g and r["d"] == d and (jsel is None or r["j"] == jsel)]
                    v, n = _stat(sub, num, den)
                    vals.append(v)
                    ns.append(n)
                if None in vals:
                    cells.append("-")
                    diffs.append(None)
                    continue
                pct = label.endswith("率") or label.startswith(("胜率", "爆头胡", "听过", "首听留", "首听即", "本局到过", "能普通", "爆头听能"))
                f = (lambda v: "%5.1f%%" % (100 * v)) if pct else (lambda v: "%6.2f" % v)
                cells.append("%s / %s / %s" % (f(vals[0]), f(vals[1]),
                                                ("%+5.1f" % (100 * (vals[0] - vals[1]))) if pct else "%+5.2f" % (vals[0] - vals[1])))
                diffs.append((vals[0] - vals[1]) * (100 if pct else 1))
            did = "%+6.2f" % (diffs[0] - diffs[1]) if None not in diffs else "-"
            print("%-18s | %-26s | %-26s | %-8s | %s" % (label, cells[0], cells[1], did, "/".join(map(str, ns))))
    print("\n读法：差 = 庄 − 闲（百分比指标单位是百分点）。差中差 = 强手差 − 我们差；只看样本足够（每格 ≥ 几百）且绝对值大的行。")


if __name__ == "__main__":
    main()
