"""做庄时我们和强手差在哪：全语料按（庄/闲 × 起手财神 0/1/2+）分格，比结果和打法风格（不重放策略，几十秒）。

    python3 tools/dealer_gap.py --since 2026-10-06T05:00

强手 = 样本外核实过的 6 人 + batch_gap_report 2026-10-07 列出的同桌强手（历史 ≥400 局、扣牌运 ≥+0.4）。
「我们(当前)」= --since 之后的房（v1.2 权重起），其余为「我们(以前)」。
结算：庄自摸 +24×番（三家各 −8×番）；闲自摸 +10×番（庄 −8×番、另一闲 −1×番）；胡者接庄。
"""
import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from multiprocessing import Pool

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from baotou_funnel import TOP_OOS, recent_rooms  # noqa: E402
from mining_common import OUR_NAME, discover_files, merge_rounds, seat_names  # noqa: E402

JOKER = "白"
STRONG = set(TOP_OOS) | {
    "ccc", "Nomad", "康陶应雀", "我胡汉三又回来了", "IA控超手人龄麻年02", "貔貅-6340", "Astra-0", "嘎达嘎达",
    "Deepseek胡", "今晚打老虎", "晴总总，该请桂语山房了", "别炸我庄求你了", "菜菜子", "白映", "连败之道"}
_RECENT = set()


def _init(recent):
    _RECENT.update(recent)


def scan(path):
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
            groups[s] = "我们(当前)" if room in _RECENT else "我们(以前)"
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
        scores = meta.get("scores") or [0] * 4
        dealer = meta.get("dealer", rnd.get("dealer"))
        winner = None if meta.get("is_draw") else meta.get("winner")
        draws, melds, claims = [0] * 4, [0] * 4, [0] * 4
        fan, detail, win_draw, win_melds = None, [], None, None
        for ev in rnd["events"]:
            kind, seat = ev.get("type"), ev.get("seat")
            data = ev.get("data") or {}
            if kind == "tile_drawn":
                draws[seat] += 1
            elif kind in ("chi", "peng"):
                melds[seat] += 1
                claims[seat] += 1
            elif kind == "gang" and data.get("kind") != "bu":
                melds[seat] += 1
            elif kind == "round_ended":
                if not data.get("draw") and winner is not None:
                    fan, detail = data.get("fan"), data.get("detail") or []
                    win_draw, win_melds = draws[winner], melds[winner]
                break
        for s, g in groups.items():
            won = winner == s
            out.append((g, s == dealer, min(hands[s].count(JOKER), 2), scores[s], won,
                        fan if won else None, won and any("爆头" in x for x in detail),
                        win_draw if won else None, win_melds if won else None, claims[s],
                        winner is not None and not won, room, rnd["round_no"]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-10-06T05:00")
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--arm", choices=("A", "B", "C"), default=None, help="bot --ab 模式下只看分到这一组的房（日志 ab_assign）")
    args = ap.parse_args()
    recent = recent_rooms(args.since)
    if args.arm:
        from latency_check import ab_labels
        labels = ab_labels(args.since, None)
        recent = {r for r in recent if labels.get(r) == args.arm}
    rows = []
    with Pool(args.jobs, initializer=_init, initargs=(recent,)) as pool:
        for part in pool.imap_unordered(scan, discover_files(), chunksize=8):
            rows += part
    G = ("强手", "我们(当前)", "我们(以前)")
    print("「我们(当前)」= %s 之后 %d 房" % (args.since, len(recent)))
    hdr = "%-11s %-10s %6s %8s %7s %6s %7s %8s %7s %7s %8s"
    print(hdr % ("格", "组", "局数", "分/局", "胜率", "番/胡", "爆头占胡", "胡在第几摸", "≤8摸胡", "吃碰/局", "胡时副露"))
    for d in (True, False):
        for j in (0, 1, 2, "合计"):
            for g in G:
                sub = [r for r in rows if r[0] == g and r[1] == d and (j == "合计" or r[2] == j)]
                if len(sub) < 30:
                    continue
                n = len(sub)
                wins = [r for r in sub if r[4]]
                wf = [r for r in wins if r[5]]
                wd = [r for r in wins if r[7] is not None]
                print(hdr % ("%s %s白" % ("庄" if d else "闲", j if j in (0, 1, "合计") else "2+"), g, n,
                             "%+.2f" % (sum(r[3] for r in sub) / n), "%.1f%%" % (100.0 * len(wins) / n),
                             "%.2f" % (sum(r[5] for r in wf) / max(1, len(wf))),
                             "%.1f%%" % (100.0 * sum(r[6] for r in wins) / max(1, len(wins))),
                             "%.2f" % (sum(r[7] for r in wd) / max(1, len(wd))),
                             "%.1f%%" % (100.0 * sum(r[7] <= 8 for r in wd) / max(1, len(wd))),
                             "%.2f" % (sum(r[9] for r in sub) / n),
                             "%.2f" % (sum(r[8] for r in wd) / max(1, len(wd)))))
            print()
    # 连庄：做庄局之后下一局是否仍做庄（= 庄胡）
    print("连庄率（做庄局里自己胡 = 下一局继续坐庄）见上表「庄 合计」的胜率。")


if __name__ == "__main__":
    main()
