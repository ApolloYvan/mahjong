# -*- coding: utf-8 -*-
""""剩余差距"审计专用重放：只对 top cohort 的对局，用**当前生产代码**
（`mj.hu_strategy.choose_hu_or_piao` 与 `mj.bot.choose_action` 的真实响应
分流）重新计算 hu_or_not 与 claim 两类决策点在"开关全关"和"两个开关都开"
两种权重下会做什么，不自己重写任何判据。

硬约束（本轮任务要求）：
  - 不读、不写 models/weights.json（用户正在用它跑房）。权重通过
    ``unittest.mock.patch`` 显式注入 ``mj.hu_strategy.load_weights`` /
    ``mj.responses.load_weights``（这两个模块各自 ``from .fit import
    load_weights`` 绑定了独立的名字，必须分别打桩），不经过磁盘文件。
  - 只重放包含至少一名 top cohort 玩家的对局文件（不是全量语料）。
  - claim 决策点的判定窗口检测复用 tools/mine_claims_baotou.py 里已经
    验证过的方法（真实 chi/peng/gang/pass/timeout 事件核对，不是自己猜
    响应协议）。

输出：
  data/analysis/top_policy_hu.csv      -- top cohort 的 hu_or_not 决策点
  data/analysis/top_policy_claims.csv  -- top cohort 的 claim 决策点

用法：
    python3 tools/top_policy_replay.py --jobs 3
"""
import argparse
import csv
import json
import os
import sys
import time
from collections import defaultdict
from multiprocessing import Pool
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.mining_common import discover_files, merge_rounds, mark_auto_discards, act_cat  # noqa: E402
from tools.decision_table import _apply_event, build_cohort_map, scan_rankings  # noqa: E402
from tools.mine_claims_baotou import _chi_options, _best_chi  # noqa: E402
from mj.tiles import JOKER, TILE_INDEX, to_counts  # noqa: E402
from mj.rules import evaluate as rules_evaluate, baotou as rules_baotou  # noqa: E402
from mj.shanten import combined_route  # noqa: E402
import mj.fit as fit_mod  # noqa: E402
import mj.hu_strategy as hu_strategy_mod  # noqa: E402
import mj.responses as responses_mod  # noqa: E402
import mj.bot as bot_mod  # noqa: E402

WEIGHTS_OFF = dict(fit_mod.DEFAULT_WEIGHTS)
WEIGHTS_ON = dict(fit_mod.DEFAULT_WEIGHTS,
                  rule_decline_joker_hold_enabled=1,
                  rule_tenpai_peng_gang_relax_enabled=1)

HU_FIELDS = ["room_id", "game_id", "round_no", "seq", "seat", "uid",
             "is_dealer", "turn_no", "wall_left", "jokers_before", "drew_joker",
             "meld_groups", "shanten_before", "act_type",
             "our_policy_act_off", "our_policy_act_on",
             "y_hu", "y_score"]

CLAIM_FIELDS = ["room_id", "game_id", "round_no", "seq", "seat", "uid",
                "is_dealer", "wall_left", "window_kind", "melds_before",
                "jokers_before", "before_shanten", "catch_play",
                "act_type", "our_policy_act_off", "our_policy_act_on",
                "y_hu", "y_score"]


def _melds_payload(melds_all):
    return [[{"kind": g["kind"], "tiles": list(g["tiles"])} for g in seat_melds]
            for seat_melds in melds_all]


def _hu_policy(snapshot, hu_result):
    with patch.object(hu_strategy_mod, "load_weights", lambda *a, **k: WEIGHTS_OFF):
        off = hu_strategy_mod.choose_hu_or_piao(dict(snapshot), hu_result)
    with patch.object(hu_strategy_mod, "load_weights", lambda *a, **k: WEIGHTS_ON):
        on = hu_strategy_mod.choose_hu_or_piao(dict(snapshot), hu_result)
    return (off or {}).get("action"), (on or {}).get("action")


def _claim_policy(snapshot):
    with patch.object(responses_mod, "load_weights", lambda *a, **k: WEIGHTS_OFF):
        off = bot_mod.choose_action(dict(snapshot))
    with patch.object(responses_mod, "load_weights", lambda *a, **k: WEIGHTS_ON):
        on = bot_mod.choose_action(dict(snapshot))
    return (off or {}).get("action"), (on or {}).get("action")


LOOKAHEAD = 12
RESPONSE_TYPES = ("pass", "timeout", "chi", "peng", "gang")


def replay_round(rnd, cohort_of, uids, room_id, game_id, info):
    hu_rows, claim_rows = [], []
    start_hands = rnd["start_hands"]
    if rnd.get("truncated") or not start_hands:
        return hu_rows, claim_rows
    events = rnd["events"]
    n = len(events)
    auto_idx = mark_auto_discards(events)

    dealer = info.get("dealer") if info else rnd.get("dealer")
    winner = info.get("winner") if info else None
    is_draw = bool(info.get("is_draw")) if info else False
    round_scores = (info.get("scores") or [0, 0, 0, 0]) if info else [0, 0, 0, 0]

    hands = [list(h) for h in start_hands]
    total_start = sum(len(h) for h in start_hands)
    melds = [[] for _ in range(4)]
    discards_river = [[] for _ in range(4)]
    chain = [0, 0, 0, 0]
    piao_out = [0, 0, 0, 0]
    turn_count = [0, 0, 0, 0]
    drawn_count = 0
    catch_active = False
    god_seat = None

    per_seat_hu_idx = defaultdict(list)
    per_seat_claim_idx = defaultdict(list)

    # 与 tools/decision_table.py 完全一致的口径：一个玩家可能在同一局里
    # 多次"摸到能胡的牌"（每次都是一次真实的 hu_or_not 决策），只有其中
    # 真正对应最终和牌的那一次摸牌事件才算 act_type="hu"，其余全部是
    # "decline_hu"——按 winner 的最后一次 tile_drawn 的 seq 精确定位，不能
    # 简化成"这个座位最终赢了就都算 hu"（那样会把这局里所有更早的、真实
    # 弃胡的决策点全部错记成"hu"，是 2026-09-24 用 top_policy_hu.csv 与
    # decisions.csv 对账时发现的一个真实 bug，此处已修复）。
    winner_final_draw_seq = None
    if winner is not None and not is_draw:
        for ev in events:
            if ev["type"] == "tile_drawn" and ev.get("seat") == winner:
                winner_final_draw_seq = ev["seq"]

    for idx, event in enumerate(events):
        kind = event["type"]
        seat = event.get("seat")
        tile = event.get("tile")
        data = event.get("data") or {}
        seq = event.get("seq")

        if kind == "tile_drawn":
            drawn_count += 1
            meld_groups = len(melds[seat])
            counts13_pre = to_counts(hands[seat])
            try:
                result = rules_evaluate(counts13_pre, TILE_INDEX[tile], chain_count=chain[seat],
                                        piao=0, meld_groups=meld_groups)
            except (KeyError, TypeError):
                result = None
            if result and idx not in auto_idx and cohort_of(uids[seat]) == "top":
                wall_left = 136 - total_start - drawn_count
                is_final_win = (winner == seat and seq == winner_final_draw_seq)
                snapshot = {
                    "seat": seat, "my_hand": list(hands[seat]) + [tile], "dealer": dealer,
                    "melds": _melds_payload(melds), "discards": [list(r) for r in discards_river],
                    "wall_remaining": wall_left, "drawn_tile": tile, "phase": "draw",
                    "god": {"catch_play": bool(catch_active), "god_discarder_seat": god_seat,
                           "chain_count": chain[seat], "piao": piao_out[seat]},
                }
                off, on = _hu_policy(snapshot, result)
                shanten_before, _ = combined_route(counts13_pre, meld_groups)
                row = {
                    "room_id": room_id, "game_id": game_id, "round_no": rnd["round_no"],
                    "seq": seq, "seat": seat, "uid": uids[seat], "is_dealer": int(dealer == seat),
                    "turn_no": turn_count[seat], "wall_left": wall_left,
                    "jokers_before": counts13_pre[TILE_INDEX[JOKER]],
                    "drew_joker": int(tile == JOKER),
                    "meld_groups": meld_groups, "shanten_before": shanten_before,
                    "act_type": "hu" if is_final_win else "decline_hu",
                    "our_policy_act_off": off, "our_policy_act_on": on,
                    "y_hu": None, "y_score": None,
                }
                hu_rows.append(row)
                per_seat_hu_idx[seat].append(len(hu_rows) - 1)
            turn_count[seat] += 1
            _apply_event(hands, melds, event)
            continue

        if kind == "tile_discarded":
            wall_left = 136 - total_start - drawn_count
            cp_flag = bool(data.get("catch_play"))
            meld_groups_disc = len(melds[seat])
            hand_before14 = list(hands[seat])

            disc_seat, disc_tile = seat, tile
            window = events[idx + 1:idx + 1 + LOOKAHEAD]
            cut = 0
            for ev2 in window:
                if ev2["type"] not in RESPONSE_TYPES:
                    break
                cut += 1
            window = window[:cut]
            accepted = None
            pass_seats, timeout_seats = set(), set()
            for ev2 in window:
                t2, s2 = ev2["type"], ev2.get("seat")
                if t2 in ("chi", "peng", "gang"):
                    if accepted is None:
                        accepted = (s2, t2, ev2.get("data") or {})
                elif t2 == "pass":
                    pass_seats.add(s2)
                elif t2 == "timeout" and (ev2.get("data") or {}).get("kind") == "response":
                    timeout_seats.add(s2)

            if disc_tile != JOKER and disc_seat is not None:
                chi_seat = (disc_seat + 1) % 4
                melds_payload = _melds_payload(melds)

                for r in range(4):
                    if r == disc_seat or cohort_of(uids[r]) != "top":
                        continue
                    can_peng = hands[r].count(disc_tile) >= 2
                    can_gang = hands[r].count(disc_tile) >= 3
                    if can_peng or can_gang:
                        if accepted and accepted[0] == r and accepted[1] in ("peng", "gang"):
                            act, auto = accepted[1], False
                        elif accepted and accepted[0] != r:
                            act, auto = "pass", False
                        elif r in pass_seats:
                            act, auto = "pass", False
                        elif r in timeout_seats:
                            act, auto = "pass", True
                        else:
                            act = None
                        if act is not None and not auto:
                            before_shanten, _ = combined_route(to_counts(hands[r]), meld_groups=len(melds[r]))
                            snapshot = {
                                "seat": r, "my_hand": list(hands[r]), "melds": melds_payload,
                                "discards": [list(x) for x in discards_river],
                                "wall_remaining": wall_left, "dealer": dealer,
                                "phase": "response_peng", "responding_seats": [r],
                                "window_tile": disc_tile,
                                "god": {"catch_play": bool(catch_active),
                                       "god_discarder_seat": god_seat,
                                       "chain_count": chain[r], "piao": piao_out[r]},
                            }
                            off, on = _claim_policy(snapshot)
                            crow = {
                                "room_id": room_id, "game_id": game_id, "round_no": rnd["round_no"],
                                "seq": seq, "seat": r, "uid": uids[r], "is_dealer": int(dealer == r),
                                "wall_left": wall_left, "window_kind": "peng_gang",
                                "melds_before": len(melds[r]), "jokers_before": hands[r].count(JOKER),
                                "before_shanten": before_shanten, "catch_play": int(cp_flag),
                                "act_type": act, "our_policy_act_off": off, "our_policy_act_on": on,
                                "y_hu": None, "y_score": None,
                            }
                            claim_rows.append(crow)
                            per_seat_claim_idx[r].append(len(claim_rows) - 1)

                if disc_tile != JOKER:
                    r = chi_seat
                    if cohort_of(uids[r]) == "top":
                        chi_opts = _chi_options(hands[r], disc_tile)
                        if chi_opts:
                            if accepted and accepted[0] == r and accepted[1] == "chi":
                                act, auto = "chi", False
                            elif accepted and accepted[0] == r and accepted[1] in ("peng", "gang"):
                                act = None
                            elif accepted and accepted[0] != r:
                                act, auto = "pass", False
                            elif r in pass_seats:
                                act, auto = "pass", False
                            elif r in timeout_seats:
                                act, auto = "pass", True
                            else:
                                act = None
                            if act is not None and not auto:
                                before_shanten, _ = combined_route(to_counts(hands[r]), meld_groups=len(melds[r]))
                                snapshot = {
                                    "seat": r, "my_hand": list(hands[r]), "melds": melds_payload,
                                    "discards": [list(x) for x in discards_river],
                                    "wall_remaining": wall_left, "dealer": dealer,
                                    "phase": "response_chi", "responding_seats": [r],
                                    "window_tile": disc_tile,
                                    "god": {"catch_play": bool(catch_active), "god_discarder_seat": god_seat,
                                           "chain_count": chain[r], "piao": piao_out[r]},
                                }
                                off, on = _claim_policy(snapshot)
                                crow = {
                                    "room_id": room_id, "game_id": game_id, "round_no": rnd["round_no"],
                                    "seq": seq, "seat": r, "uid": uids[r], "is_dealer": int(dealer == r),
                                    "wall_left": wall_left, "window_kind": "chi",
                                    "melds_before": len(melds[r]), "jokers_before": hands[r].count(JOKER),
                                    "before_shanten": before_shanten, "catch_play": int(cp_flag),
                                    "act_type": act, "our_policy_act_off": off, "our_policy_act_on": on,
                                    "y_hu": None, "y_score": None,
                                }
                                claim_rows.append(crow)
                                per_seat_claim_idx[r].append(len(claim_rows) - 1)

            stable13 = list(hand_before14)
            stable13.remove(tile)
            is_bt_after = rules_baotou(to_counts(stable13), meld_groups_disc)
            if cp_flag and not catch_active:
                catch_active, god_seat = True, seat
            elif not cp_flag:
                catch_active, god_seat = False, None
            if tile == JOKER and is_bt_after:
                chain[seat] += 1
                piao_out[seat] += 1
            else:
                chain[seat] = 0
            discards_river[seat].append(tile)
            _apply_event(hands, melds, event)
            continue

        if kind in ("chi", "peng", "gang"):
            if kind == "gang":
                chain[seat] += 1
            _apply_event(hands, melds, event)
            continue

    for seat_i in range(4):
        won = (winner == seat_i and not is_draw)
        score = round_scores[seat_i] if seat_i < len(round_scores) else 0
        for ridx in per_seat_hu_idx[seat_i]:
            hu_rows[ridx]["y_hu"] = int(won)
            hu_rows[ridx]["y_score"] = score
        for ridx in per_seat_claim_idx[seat_i]:
            claim_rows[ridx]["y_hu"] = int(won)
            claim_rows[ridx]["y_score"] = score

    return hu_rows, claim_rows


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
        return [], []
    seats = game.get("seats") or []
    if len(seats) != 4:
        return [], []
    uids = [s.get("user_id") for s in seats]
    if not any(cohort_of(u) == "top" for u in uids):
        return [], []
    room_id, game_id = game.get("room_id"), game.get("game_id")
    rounds_info = {r.get("round_no"): r for r in (game.get("rounds") or [])}
    all_hu, all_claim = [], []
    for rnd in merge_rounds(game):
        info = rounds_info.get(rnd["round_no"])
        if info is None:
            continue
        hu_rows, claim_rows = replay_round(rnd, cohort_of, uids, room_id, game_id, info)
        all_hu.extend(hu_rows)
        all_claim.extend(claim_rows)
    return all_hu, all_claim


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", nargs="*",
                    default=["tools/models/events/*.json", "models/events/*.json"])
    ap.add_argument("--out-hu", default="data/analysis/top_policy_hu.csv")
    ap.add_argument("--out-claims", default="data/analysis/top_policy_claims.csv")
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    t0 = time.time()
    files = discover_files(tuple(args.events), limit=args.limit)
    print("文件数（全量，用于排名）：%d" % len(files))
    score = scan_rankings(files)
    cohort_map = build_cohort_map(score)
    top_uids = {u for u, c in cohort_map.items() if c == "top"}
    print("top cohort 人数：%d" % len(top_uids))

    top_files = []
    for path in files:
        try:
            game = json.load(open(path, encoding="utf-8"))
        except (ValueError, OSError):
            continue
        uids = [s.get("user_id") for s in (game.get("seats") or [])]
        if any(u in top_uids for u in uids):
            top_files.append(path)
    print("含 top cohort 玩家的对局文件数：%d" % len(top_files))

    os.makedirs(os.path.dirname(args.out_hu), exist_ok=True)
    n_hu = n_claim = 0
    with open(args.out_hu, "w", newline="", encoding="utf-8") as fh, \
         open(args.out_claims, "w", newline="", encoding="utf-8") as fc:
        wh = csv.DictWriter(fh, fieldnames=HU_FIELDS, extrasaction="ignore")
        wc = csv.DictWriter(fc, fieldnames=CLAIM_FIELDS, extrasaction="ignore")
        wh.writeheader(); wc.writeheader()
        with Pool(args.jobs, initializer=_init_worker, initargs=(cohort_map,)) as pool:
            for hu_rows, claim_rows in pool.imap_unordered(process_file, top_files, chunksize=2):
                for row in hu_rows:
                    wh.writerow(row)
                for row in claim_rows:
                    wc.writerow(row)
                n_hu += len(hu_rows); n_claim += len(claim_rows)

    print("hu_or_not 行数：%d  claim 行数：%d" % (n_hu, n_claim))
    print("总耗时 %.1fs" % (time.time() - t0))
    print("已写出 %s / %s" % (args.out_hu, args.out_claims))


if __name__ == "__main__":
    main()
