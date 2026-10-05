# -*- coding: utf-8 -*-
"""S2/S3 收敛任务专用的定向重放：claim（吃/碰/杠响应窗口，含 pass）决策点 +
discard 行的爆头可达性特征。不是全量 decision_table.py 的替代品，是一次
增量、更轻量的重放（跳过反事实 bot 调用），复用 tools/mining_common.py 与
tools/decision_table.py 的事件重放机制。

背景：data/analysis/decisions.csv（上一轮产物）只有 discard/hu_or_not 两类
决策点，没有 claim。见 docs/STRATEGY_MINING.md、docs/experiments/OFFLINE_REPORT.md
「已知局限」第一条。本文件补上 claim，并给 discard 行加一个新维度（爆头是否
本回合可达、是否被放弃）——这是本轮 S2/S3 的数据基础。

产出两张表：
  data/analysis/claims.csv     -- 每个真实吃/碰/杠响应窗口一行（含 pass）
  data/analysis/s3_baotou.csv  -- 每个真实弃牌决策一行，附加「打出前」维度

方法论说明（如实标注的近似，见脚本内注释）：
  服务端日志对响应窗口的记录不是「一格决策=一条事件」的干净结构（同一弃牌
  后经常看到同一座位出现两条 pass/timeout，且没有 window 字段区分是碰窗口
  还是杠窗口），逐字段解析这套模板的语义成本很高且价值不确定。本脚本改用
  「自己按吃碰硬规则计算这一路合法候选」（与 mj/responses.py 的门禁同构）
  + 「在紧随其后的事件窗口里找有没有一条真正的 chi/peng/gang 事件命中该
  座位」的方式确定真实动作：命中 = 接受；未命中但窗口内出现该座位的
  timeout(kind=response) = 服务端代打/超时强制放弃，按 auto=1 排除；
  未命中但窗口内出现该座位的 pass = 真实主动放弃；两者都没出现 = 无法
  核实，跳过（计入 stats["claim_unconfirmed"]，不写入结果，不臆测）。

用法：
    python3 tools/mine_claims_baotou.py --jobs 3
    python3 tools/mine_claims_baotou.py --limit 20 --jobs 2   # profiling
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

from tools.mining_common import (  # noqa: E402
    discover_files, merge_rounds, mark_auto_discards, act_cat,
)
from tools.decision_table import _apply_event  # noqa: E402
from mj.tiles import JOKER, INDEX_TILE, TILE_INDEX, to_counts  # noqa: E402
from mj.shanten import route_shanten  # noqa: E402
from mj.rules import baotou as rules_baotou  # noqa: E402
from mj.responses import claim_assessment  # noqa: E402
from mj.melds import can_chi  # noqa: E402
from tools.decision_table import build_cohort_map, scan_rankings  # noqa: E402

LOOKAHEAD = 12
RESPONSE_TYPES = ("pass", "timeout", "chi", "peng", "gang")

CLAIM_FIELDS = [
    "room_id", "game_id", "round_no", "seq", "seat", "uid", "cohort",
    "is_dealer", "turn_no", "wall_left", "window_kind", "melds_before",
    "jokers_before", "n_chi_options", "can_peng", "can_gang", "catch_play",
    "before_shanten", "before_ukeire", "best_after_shanten", "best_after_ukeire",
    "best_after_baotou", "gate_allowed", "act_type", "act_chi_combo",
    "y_hu", "y_fan", "y_score",
]

BAOTOU_FIELDS = [
    "room_id", "game_id", "round_no", "seq", "seat", "uid", "cohort",
    "is_dealer", "turn_no", "wall_left", "melds", "jokers_before",
    "shanten_before", "baotou_reachable", "took_baotou", "forwent_baotou",
    "act_tile", "act_cat", "y_hu", "y_fan", "y_score",
]


def _chi_options(hand, tile):
    """镜像 mj.responses.choose_chi 的候选枚举（不含门禁判定），供 S2 重放
    统计"有几种吃法"、离线核验哪一种被选中。逻辑与该函数完全一致。"""
    index = TILE_INDEX.get(tile)
    if index is None or index >= 27:
        return []
    options = []
    for offsets in ((-2, -1), (-1, 1), (1, 2)):
        idxs = [index + o for o in offsets]
        if min(idxs) < 0 or max(idxs) >= 27:
            continue
        if any(i // 9 != index // 9 for i in idxs):
            continue
        names = [INDEX_TILE[i] for i in idxs]
        if all(hand.count(n) for n in names):
            options.append((offsets, names))
    return options


def _combo_label(offsets):
    return {(-2, -1): "low", (-1, 1): "mid", (1, 2): "high"}[offsets]


def _best_chi(snapshot, options):
    """与 choose_chi 相同的排序键（不含 PREFER_BAOTOU_SUCCESSOR，该常量默认
    False），返回 (assessment, offsets, names) 里 assessment 最优的一项。"""
    best = None
    for offsets, names in options:
        assessment = claim_assessment(snapshot, names)
        key = (assessment["after_shanten"] if assessment["after_shanten"] is not None else 99,
               -(assessment["after_ukeire"] or 0))
        if best is None or key < best[0]:
            best = (key, assessment, offsets, names)
    if best is None:
        return None, None, None
    return best[1], best[2], best[3]


def replay_round_claims(rnd, cohort_of, uids, room_id, game_id, info):
    stats = defaultdict(int)
    start_hands = rnd["start_hands"]
    if rnd.get("truncated") or not start_hands:
        stats["dropped_truncated_or_nohand"] += 1
        return [], [], stats

    events = rnd["events"]
    n = len(events)
    auto_idx_discard = mark_auto_discards(events)

    dealer = info.get("dealer") if info else rnd.get("dealer")
    winner = info.get("winner") if info else None
    is_draw = bool(info.get("is_draw")) if info else False
    round_scores = (info.get("scores") or [0, 0, 0, 0]) if info else [0, 0, 0, 0]

    hands = [list(h) for h in start_hands]
    total_start = sum(len(h) for h in start_hands)
    melds = [[] for _ in range(4)]
    turn_count = [0, 0, 0, 0]
    drawn_count = 0

    claim_rows = []
    baotou_rows = []
    per_seat_claim_idx = defaultdict(list)
    per_seat_bt_idx = defaultdict(list)

    for idx, event in enumerate(events):
        kind = event["type"]
        seat = event.get("seat")
        tile = event.get("tile")
        data = event.get("data") or {}
        seq = event.get("seq")

        if kind == "tile_drawn":
            drawn_count += 1
            turn_count[seat] += 1
            _apply_event(hands, melds, event)
            continue

        if kind == "tile_discarded":
            wall_left = 136 - total_start - drawn_count
            is_auto_discard = idx in auto_idx_discard
            meld_groups_disc = len(melds[seat])
            hand_before14 = list(hands[seat])
            cp_flag = bool(data.get("catch_play"))

            if not is_auto_discard:
                unique_tiles = sorted(set(hand_before14))
                jokers_before = hand_before14.count(JOKER)
                best_shanten = None
                bt_reach = False
                for cand in unique_tiles:
                    left = list(hand_before14)
                    left.remove(cand)
                    counts_l = to_counts(left)
                    s = route_shanten(counts_l, meld_groups_disc)
                    if best_shanten is None or s < best_shanten:
                        best_shanten = s
                    if rules_baotou(counts_l, meld_groups_disc):
                        bt_reach = True
                stable13 = list(hand_before14)
                stable13.remove(tile)
                took_bt = bool(rules_baotou(to_counts(stable13), meld_groups_disc))
                row = {
                    "room_id": room_id, "game_id": game_id, "round_no": rnd["round_no"],
                    "seq": seq, "seat": seat, "uid": uids[seat], "cohort": cohort_of(uids[seat]),
                    "is_dealer": int(dealer == seat), "turn_no": turn_count[seat],
                    "wall_left": wall_left, "melds": meld_groups_disc,
                    "jokers_before": jokers_before, "shanten_before": best_shanten,
                    "baotou_reachable": int(bt_reach), "took_baotou": int(took_bt),
                    "forwent_baotou": int(bt_reach and not took_bt),
                    "act_tile": tile, "act_cat": act_cat(tile),
                    "y_hu": None, "y_fan": None, "y_score": None,
                }
                baotou_rows.append(row)
                per_seat_bt_idx[seat].append(len(baotou_rows) - 1)

            # -- claim windows opened by this discard (peek ahead, read-only) --
            disc_seat, disc_tile = seat, tile
            window = events[idx + 1:idx + 1 + LOOKAHEAD]
            cut = 0
            for ev2 in window:
                if ev2["type"] not in RESPONSE_TYPES:
                    break
                cut += 1
            window = window[:cut]

            accepted = None  # (seat, kind, data)
            pass_seats, timeout_seats = set(), set()
            for ev2 in window:
                t2 = ev2["type"]
                s2 = ev2.get("seat")
                if t2 in ("chi", "peng", "gang"):
                    if accepted is None:
                        accepted = (s2, t2, ev2.get("data") or {})
                elif t2 == "pass":
                    pass_seats.add(s2)
                elif t2 == "timeout" and (ev2.get("data") or {}).get("kind") == "response":
                    timeout_seats.add(s2)

            if disc_tile != JOKER and disc_seat is not None:
                chi_seat = (disc_seat + 1) % 4
                chi_opts = (_chi_options(hands[chi_seat], disc_tile)
                            if can_chi(melds[chi_seat]) else [])

                for r in range(4):
                    if r == disc_seat:
                        continue
                    can_peng = hands[r].count(disc_tile) >= 2
                    can_gang = hands[r].count(disc_tile) >= 3
                    if can_peng or can_gang:
                        if accepted and accepted[0] == r and accepted[1] in ("peng", "gang"):
                            act = accepted[1]
                            auto = False
                        elif accepted and accepted[0] != r:
                            act = "pass"; auto = False   # 被别家更高优先级抢先
                        elif r in pass_seats:
                            act = "pass"; auto = False
                        elif r in timeout_seats:
                            act = "pass"; auto = True
                        else:
                            stats["claim_unconfirmed"] += 1
                            act = None
                        if act is not None and not auto:
                            snap_r = {"my_hand": list(hands[r]), "melds": melds, "seat": r}
                            take = (disc_tile, disc_tile)
                            assessment = claim_assessment(snap_r, take) if can_peng else {
                                "allowed": None, "before_shanten": None, "before_ukeire": None,
                                "after_shanten": None, "after_ukeire": None, "after_baotou": None,
                            }
                            crow = {
                                "room_id": room_id, "game_id": game_id, "round_no": rnd["round_no"],
                                "seq": seq, "seat": r, "uid": uids[r], "cohort": cohort_of(uids[r]),
                                "is_dealer": int(dealer == r), "turn_no": turn_count[r],
                                "wall_left": wall_left, "window_kind": "peng_gang",
                                "melds_before": len(melds[r]), "jokers_before": hands[r].count(JOKER),
                                "n_chi_options": 0, "can_peng": int(can_peng), "can_gang": int(can_gang),
                                "catch_play": int(cp_flag),
                                "before_shanten": assessment.get("before_shanten"),
                                "before_ukeire": assessment.get("before_ukeire"),
                                "best_after_shanten": assessment.get("after_shanten"),
                                "best_after_ukeire": assessment.get("after_ukeire"),
                                "best_after_baotou": int(bool(assessment.get("after_baotou"))),
                                "gate_allowed": assessment.get("allowed"),
                                "act_type": act, "act_chi_combo": None,
                                "y_hu": None, "y_fan": None, "y_score": None,
                            }
                            claim_rows.append(crow)
                            per_seat_claim_idx[r].append(len(claim_rows) - 1)

                if chi_opts:
                    r = chi_seat
                    if accepted and accepted[0] == r and accepted[1] == "chi":
                        act = "chi"; auto = False
                        used = list((accepted[2].get("tiles")) or [])
                        try:
                            used.remove(disc_tile)
                        except ValueError:
                            pass
                        chosen_offsets = None
                        for offsets, names in chi_opts:
                            if sorted(names) == sorted(used):
                                chosen_offsets = offsets
                                break
                    elif accepted and accepted[0] == r and accepted[1] in ("peng", "gang"):
                        act = None; auto = False   # 同座位已用碰/杠响应，吃窗口作废
                    elif accepted and accepted[0] != r:
                        act = "pass"; auto = False; chosen_offsets = None
                    elif r in pass_seats:
                        act = "pass"; auto = False; chosen_offsets = None
                    elif r in timeout_seats:
                        act = "pass"; auto = True; chosen_offsets = None
                    else:
                        stats["claim_unconfirmed"] += 1
                        act = None
                    if act is not None and not auto:
                        snap_r = {"my_hand": list(hands[r]), "melds": melds, "seat": r}
                        best_assess, best_offsets, _best_names = _best_chi(snap_r, chi_opts)
                        crow = {
                            "room_id": room_id, "game_id": game_id, "round_no": rnd["round_no"],
                            "seq": seq, "seat": r, "uid": uids[r], "cohort": cohort_of(uids[r]),
                            "is_dealer": int(dealer == r), "turn_no": turn_count[r],
                            "wall_left": wall_left, "window_kind": "chi",
                            "melds_before": len(melds[r]), "jokers_before": hands[r].count(JOKER),
                            "n_chi_options": len(chi_opts), "can_peng": 0, "can_gang": 0,
                            "catch_play": int(cp_flag),
                            "before_shanten": best_assess.get("before_shanten") if best_assess else None,
                            "before_ukeire": best_assess.get("before_ukeire") if best_assess else None,
                            "best_after_shanten": best_assess.get("after_shanten") if best_assess else None,
                            "best_after_ukeire": best_assess.get("after_ukeire") if best_assess else None,
                            "best_after_baotou": int(bool(best_assess.get("after_baotou"))) if best_assess else 0,
                            "gate_allowed": best_assess.get("allowed") if best_assess else None,
                            "act_type": act,
                            "act_chi_combo": (_combo_label(chosen_offsets) if act == "chi" and chosen_offsets
                                              else (_combo_label(best_offsets) if act == "pass" and best_offsets
                                                    else None)),
                            "y_hu": None, "y_fan": None, "y_score": None,
                        }
                        claim_rows.append(crow)
                        per_seat_claim_idx[r].append(len(claim_rows) - 1)

            _apply_event(hands, melds, event)
            continue

        if kind in ("chi", "peng", "gang"):
            _apply_event(hands, melds, event)
            continue

    for seat_i in range(4):
        won = (winner == seat_i and not is_draw)
        score = round_scores[seat_i] if seat_i < len(round_scores) else 0
        for ridx in per_seat_claim_idx[seat_i]:
            r = claim_rows[ridx]
            r["y_hu"] = int(won); r["y_fan"] = None; r["y_score"] = score
        for ridx in per_seat_bt_idx[seat_i]:
            r = baotou_rows[ridx]
            r["y_hu"] = int(won); r["y_fan"] = None; r["y_score"] = score

    return claim_rows, baotou_rows, stats


_WORKER_STATE = {}


def _init_worker(cohort_map):
    _WORKER_STATE["cohort"] = cohort_map


def process_file(path):
    cohort_map = _WORKER_STATE.get("cohort", {})

    def cohort_of(uid):
        return cohort_map.get(uid, "other")

    stats = defaultdict(int)
    try:
        game = json.load(open(path, encoding="utf-8"))
    except (ValueError, OSError):
        stats["dropped_bad_json"] += 1
        return [], [], stats
    seats = game.get("seats") or []
    if len(seats) != 4:
        stats["dropped_bad_seats"] += 1
        return [], [], stats
    uids = [s.get("user_id") for s in seats]
    room_id = game.get("room_id")
    game_id = game.get("game_id")
    rounds_info = {r.get("round_no"): r for r in (game.get("rounds") or [])}

    all_claims, all_bt = [], []
    for rnd in merge_rounds(game):
        info = rounds_info.get(rnd["round_no"])
        if info is None:
            stats["dropped_no_round_info"] += 1
            continue
        claims, bts, rstats = replay_round_claims(rnd, cohort_of, uids, room_id, game_id, info)
        for k, v in rstats.items():
            stats[k] += v
        all_claims.extend(claims)
        all_bt.extend(bts)
    stats["claim_rows"] += len(all_claims)
    stats["baotou_rows"] += len(all_bt)
    return all_claims, all_bt, stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", nargs="*",
                    default=["tools/models/events/*.json", "models/events/*.json"])
    ap.add_argument("--out-claims", default="data/analysis/claims.csv")
    ap.add_argument("--out-baotou", default="data/analysis/s3_baotou.csv")
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    t0 = time.time()
    files = discover_files(tuple(args.events), limit=args.limit)
    print("文件数（去重后）：%d" % len(files))

    score = scan_rankings(files)
    cohort_map = build_cohort_map(score)
    t_rank = time.time()
    print("排名扫描耗时 %.1fs" % (t_rank - t0))

    os.makedirs(os.path.dirname(args.out_claims), exist_ok=True)
    total_stats = defaultdict(int)
    n_claims = n_bt = 0
    with open(args.out_claims, "w", newline="", encoding="utf-8") as fc, \
         open(args.out_baotou, "w", newline="", encoding="utf-8") as fb:
        wc = csv.DictWriter(fc, fieldnames=CLAIM_FIELDS, extrasaction="ignore")
        wb = csv.DictWriter(fb, fieldnames=BAOTOU_FIELDS, extrasaction="ignore")
        wc.writeheader(); wb.writeheader()
        with Pool(args.jobs, initializer=_init_worker, initargs=(cohort_map,)) as pool:
            for claims, bts, stats in pool.imap_unordered(process_file, files, chunksize=2):
                for row in claims:
                    wc.writerow(row)
                for row in bts:
                    wb.writerow(row)
                n_claims += len(claims); n_bt += len(bts)
                for k, v in stats.items():
                    total_stats[k] += v

    t1 = time.time()
    print("claim 行数：%d  baotou 行数：%d" % (n_claims, n_bt))
    print("统计：%s" % dict(total_stats))
    print("总耗时 %.1fs（%d 文件）" % (t1 - t0, len(files)))
    print("已写出 %s / %s" % (args.out_claims, args.out_baotou))


if __name__ == "__main__":
    main()
