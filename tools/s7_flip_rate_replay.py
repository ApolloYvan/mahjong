# -*- coding: utf-8 -*-
"""S7 追加核实（用户反馈）：population②（shanten_before==1, jokers_before==1）
的"行为改变比例"不许用理论上界，要像 S1/S6 那样重放真实生产代码。

只对 "ours" 的这个情境做定向重放：对每一个真实历史决策点，重建 14 张
弃牌前手牌，分别在 `rule_single_joker_baotou_enabled` 关/开两种权重下调用
`mj.bot.choose_discard`（生产真实分流，不自己重写判据），比较两次选择的
弃牌是否不同——不同就计一次"翻转"。

用法：
    python3 tools/s7_flip_rate_replay.py --jobs 3
"""
import argparse
import json
import os
import sys
import time
from collections import defaultdict
from multiprocessing import Pool
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.mining_common import discover_files, merge_rounds, mark_auto_discards  # noqa: E402
from tools.decision_table import _apply_event, build_cohort_map, scan_rankings  # noqa: E402
from mj.tiles import JOKER, TILE_INDEX, to_counts  # noqa: E402
from mj.shanten import route_shanten  # noqa: E402
import mj.fit as fit_mod  # noqa: E402
import mj.ev as ev_mod  # noqa: E402
import mj.strategy as strategy_mod  # noqa: E402
import mj.bot as bot_mod  # noqa: E402

WEIGHTS_OFF = dict(fit_mod.DEFAULT_WEIGHTS)
WEIGHTS_ON = dict(fit_mod.DEFAULT_WEIGHTS, rule_menqing_baotou_enabled=1)


def _melds_payload(melds_all):
    return [[{"kind": g["kind"], "tiles": list(g["tiles"])} for g in seat_melds]
            for seat_melds in melds_all]


def _choose(snapshot):
    decision = bot_mod.choose_discard(snapshot, rules={})
    return decision.get("tile") if decision else None


def replay_round(rnd, cohort_of, uids, room_id, game_id, info):
    out = []
    start_hands = rnd["start_hands"]
    if rnd.get("truncated") or not start_hands:
        return out
    events = rnd["events"]
    auto_idx = mark_auto_discards(events)
    dealer = info.get("dealer") if info else rnd.get("dealer")

    hands = [list(h) for h in start_hands]
    total_start = sum(len(h) for h in start_hands)
    melds = [[] for _ in range(4)]
    discards_river = [[] for _ in range(4)]
    drawn_count = 0

    for idx, event in enumerate(events):
        kind = event["type"]
        seat = event.get("seat")
        tile = event.get("tile")
        data = event.get("data") or {}

        if kind == "tile_drawn":
            drawn_count += 1
            hands[seat].append(tile)
            continue

        if kind == "tile_discarded":
            wall_left = 136 - total_start - drawn_count
            if idx not in auto_idx and cohort_of(uids[seat]) == "ours":
                hand14 = list(hands[seat])
                meld_groups = len(melds[seat])
                counts_full = to_counts(hand14)
                jokers_before = counts_full[TILE_INDEX[JOKER]]
                # 与 s3_baotou.csv 完全同口径：shanten_before 是这 14 张手牌
                # 里，去掉任意一张候选弃牌后能达到的最快向听（不含刚摸到的
                # 那张本身的特殊地位，逐个候选取最小）。
                best_shanten = None
                for cand in set(hand14):
                    left = list(hand14)
                    left.remove(cand)
                    s = route_shanten(to_counts(left), meld_groups)
                    if best_shanten is None or s < best_shanten:
                        best_shanten = s
                if meld_groups == 0 and jokers_before >= 1 and best_shanten in (1, 2):
                    snapshot = {
                        "seat": seat, "my_hand": hand14, "dealer": dealer,
                        "melds": _melds_payload(melds),
                        "discards": [list(r) for r in discards_river],
                        "wall_remaining": wall_left, "drawn_tile": tile, "phase": "draw",
                        "god": {"catch_play": False, "god_discarder_seat": None,
                               "chain_count": 0, "piao": 0},
                    }
                    with patch.object(ev_mod, "load_weights", lambda *a, **k: WEIGHTS_OFF), \
                         patch.object(strategy_mod, "load_weights", lambda *a, **k: WEIGHTS_OFF):
                        off = _choose(dict(snapshot))
                    with patch.object(ev_mod, "load_weights", lambda *a, **k: WEIGHTS_ON), \
                         patch.object(strategy_mod, "load_weights", lambda *a, **k: WEIGHTS_ON):
                        on = _choose(dict(snapshot))
                    jbucket = "1" if jokers_before == 1 else "2+"
                    out.append({"room_id": room_id, "game_id": game_id,
                               "round_no": rnd["round_no"], "seq": event.get("seq"),
                               "seat": seat, "off_tile": off, "on_tile": on,
                               "jbucket": jbucket,
                               "flipped": int(off != on)})
            _apply_event(hands, melds, event)
            continue

        if kind in ("chi", "peng", "gang"):
            _apply_event(hands, melds, event)
            continue

    return out


_WORKER_STATE = {}


def _init_worker(cohort_map):
    _WORKER_STATE["cohort"] = cohort_map


def process_file(path):
    cohort_map = _WORKER_STATE.get("cohort", {})

    def cohort_of(uid):
        return cohort_map.get(uid, "other")

    try:
        game = json.load(open(path, encoding="utf-8"))
    except (ValueError, OSError):
        return []
    seats = game.get("seats") or []
    if len(seats) != 4:
        return []
    uids = [s.get("user_id") for s in seats]
    if "u_fd06550b5fb3" not in uids:
        return []
    room_id, game_id = game.get("room_id"), game.get("game_id")
    rounds_info = {r.get("round_no"): r for r in (game.get("rounds") or [])}
    out = []
    for rnd in merge_rounds(game):
        info = rounds_info.get(rnd["round_no"])
        if info is None:
            continue
        out.extend(replay_round(rnd, cohort_of, uids, room_id, game_id, info))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", nargs="*",
                    default=["tools/models/events/*.json", "models/events/*.json"])
    ap.add_argument("--out", default="data/analysis/s7_flip_rate.csv")
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    t0 = time.time()
    files = discover_files(tuple(args.events), limit=args.limit)
    print("文件数：%d" % len(files))
    score = scan_rankings(files)
    cohort_map = build_cohort_map(score)

    our_files = []
    for path in files:
        try:
            game = json.load(open(path, encoding="utf-8"))
        except (ValueError, OSError):
            continue
        uids = [s.get("user_id") for s in (game.get("seats") or [])]
        if "u_fd06550b5fb3" in uids:
            our_files.append(path)
    print("含我方的对局文件数：%d" % len(our_files))

    import csv
    n = 0
    n_flip = 0
    bucket_n = defaultdict(int)
    bucket_flip = defaultdict(int)
    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["room_id", "game_id", "round_no", "seq", "seat",
                                          "off_tile", "on_tile", "jbucket", "flipped"])
        w.writeheader()
        with Pool(args.jobs, initializer=_init_worker, initargs=(cohort_map,)) as pool:
            for rows in pool.imap_unordered(process_file, our_files, chunksize=2):
                for row in rows:
                    w.writerow(row)
                    n += 1
                    n_flip += row["flipped"]
                    bucket_n[row["jbucket"]] += 1
                    bucket_flip[row["jbucket"]] += row["flipped"]

    print("命中数：%d  翻转数：%d  翻转率：%.4f" % (n, n_flip, n_flip / n if n else 0))
    for b in sorted(bucket_n):
        bn = bucket_n[b]
        bf = bucket_flip[b]
        print("  财神数=%s  命中数：%d  翻转数：%d  翻转率：%.4f" % (b, bn, bf, bf / bn if bn else 0))
    print("总耗时 %.1fs" % (time.time() - t0))
    print("已写出 %s" % args.out)


if __name__ == "__main__":
    main()
