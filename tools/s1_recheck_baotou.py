# -*- coding: utf-8 -*-
"""S1 correctness 修复后的重新核算专用重放：给 decisions.csv 里每一条
``hu_or_not`` 行补一个新字段 ``baotou_reachable_after_decline``——在摸到这张
牌、决定弃胡的那一刻（13 张摸前手牌 + 这张摸到的牌 = 14 张），是否存在
一张非财神弃牌，打出后 ``rules.baotou(counts, meld_groups)`` 为真。

这是 ``mj/hu_strategy.py::_decline_discard_choice`` correctness 修复（见
docs/experiments/OFFLINE_REPORT.md「五问结论」S1）新增的硬性前提条件——
旧版 S1 分析（``tools/five_questions.py``）统计覆盖率/Δ(y_score) 时，
population 只按（是否爆头 × 财神数 × 是否摸白）过滤，**没有要求这张 14 张
手牌真的存在一张能转爆头的弃牌**。本脚本把这个条件补上，供
``tools/five_questions.py`` 重新计算用。

只输出 (room_id, game_id, round_no, seq, seat, baotou_reachable_after_decline)
五元组 + 1 个结果列，按 key 与 decisions.csv 做内存 join，不重算其余字段
（沿用已有的 act_type/is_baotou/jokers/y_score 等列）。

用法：
    python3 tools/s1_recheck_baotou.py --jobs 3
"""
import argparse
import csv
import json
import os
import sys
import time
from collections import defaultdict
from multiprocessing import Pool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.mining_common import discover_files, merge_rounds, mark_auto_discards  # noqa: E402
from tools.decision_table import _apply_event, build_cohort_map, scan_rankings  # noqa: E402
from mj.tiles import JOKER, TILE_INDEX, to_counts  # noqa: E402
from mj.rules import baotou as rules_baotou, evaluate as rules_evaluate  # noqa: E402

OUT_FIELDS = ["room_id", "game_id", "round_no", "seq", "seat",
              "baotou_reachable_after_decline"]


def _reachable(hand14, meld_groups):
    for cand in set(t for t in hand14 if t != JOKER):
        left = list(hand14)
        left.remove(cand)
        if rules_baotou(to_counts(left), meld_groups):
            return True
    return False


def replay_round(rnd, room_id, game_id):
    out = []
    start_hands = rnd["start_hands"]
    if rnd.get("truncated") or not start_hands:
        return out
    events = rnd["events"]
    auto_idx = mark_auto_discards(events)
    hands = [list(h) for h in start_hands]
    melds = [[] for _ in range(4)]
    chain = [0, 0, 0, 0]

    for idx, event in enumerate(events):
        kind = event["type"]
        seat = event.get("seat")
        tile = event.get("tile")
        seq = event.get("seq")
        if kind == "tile_drawn":
            meld_groups = len(melds[seat])
            counts13_pre = to_counts(hands[seat])
            try:
                result = rules_evaluate(counts13_pre, TILE_INDEX[tile],
                                        chain_count=chain[seat], piao=0,
                                        meld_groups=meld_groups)
            except (KeyError, TypeError):
                result = None
            if result and idx not in auto_idx:
                hand14 = list(hands[seat]) + [tile]
                reach = _reachable(hand14, meld_groups)
                out.append({"room_id": room_id, "game_id": game_id,
                           "round_no": rnd["round_no"], "seq": seq, "seat": seat,
                           "baotou_reachable_after_decline": int(reach)})
            _apply_event(hands, melds, event)
            continue
        if kind == "tile_discarded":
            stable13 = list(hands[seat])
            stable13.remove(tile)
            is_bt_after = rules_baotou(to_counts(stable13), len(melds[seat]))
            if tile == JOKER and is_bt_after:
                chain[seat] += 1
            else:
                chain[seat] = 0
            _apply_event(hands, melds, event)
            continue
        if kind in ("chi", "peng", "gang"):
            if kind == "gang":
                chain[seat] += 1
            _apply_event(hands, melds, event)
            continue
    return out


def process_file(path):
    try:
        game = json.load(open(path, encoding="utf-8"))
    except (ValueError, OSError):
        return []
    seats = game.get("seats") or []
    if len(seats) != 4:
        return []
    room_id = game.get("room_id")
    game_id = game.get("game_id")
    rounds_info = {r.get("round_no"): r for r in (game.get("rounds") or [])}
    out = []
    for rnd in merge_rounds(game):
        if rounds_info.get(rnd["round_no"]) is None:
            continue
        out.extend(replay_round(rnd, room_id, game_id))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", nargs="*",
                    default=["tools/models/events/*.json", "models/events/*.json"])
    ap.add_argument("--out", default="data/analysis/s1_recheck_baotou.csv")
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    t0 = time.time()
    files = discover_files(tuple(args.events), limit=args.limit)
    print("文件数：%d" % len(files))

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    n = 0
    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=OUT_FIELDS)
        w.writeheader()
        with Pool(args.jobs) as pool:
            for rows in pool.imap_unordered(process_file, files, chunksize=2):
                for row in rows:
                    w.writerow(row)
                n += len(rows)
    print("行数：%d  耗时 %.1fs" % (n, time.time() - t0))
    print("已写出 %s" % args.out)


if __name__ == "__main__":
    main()
