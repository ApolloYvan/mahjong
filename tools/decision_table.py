# -*- coding: utf-8 -*-
"""高手情境策略挖掘：把 events 全量重放成一张「决策点」CSV。

每一行 = 本方玩家真实做出的一次决策（弃牌 / 胡还是飘），排除服务端代打
（``auto=1``，不写入 CSV）。反事实列 ``our_policy_act`` 通过构造与生产
``mj/bot.py`` 完全一致的 snapshot 字典、直接调用 ``mj.bot.choose_discard``
得到——不重新实现 use_route 分流逻辑，从根本上规避
docs/STRATEGY_MINING.md 5.1 提醒的"policy_diff 只测 choose_discard、
漏了分流"的错误。``our_policy_act_baseline_only`` 复刻
``tools/policy_diff.py`` 的调用方式（chain_count=0, piao=0, rules={},
visible=None），供第八节回归对账。

范围说明（诚实标注，见 docs/experiments/OFFLINE_REPORT.md）：本版本实现
``discard`` 与 ``hu_or_not`` 两类决策点。``claim``（吃/碰/杠响应窗口）
未实现——构造响应窗口 snapshot 需要额外的 window/responding_seats 语义，
时间关系本轮未做，留作后续。

用法：
    python3 tools/decision_table.py --limit 20 --jobs 4
    python3 tools/decision_table.py --jobs 8                      # 全量
    python3 tools/decision_table.py --no-counterfactual --jobs 8  # 跳过反事实（最快）
"""
import argparse
import bisect
import csv
import json
import os
import sys
import time
from collections import defaultdict
from multiprocessing import Pool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.mining_common import (  # noqa: E402
    OUR_UID, discover_files, merge_rounds, mark_auto_discards, act_cat, to_baotou,
)

from mj.tiles import JOKER, JOKER_IDX, TILE_INDEX, to_counts  # noqa: E402
from mj.shanten import shanten, pair_shanten, combined_route, route_shanten, ukeire as ukeire_fn  # noqa: E402
from mj.rules import baotou as rules_baotou, evaluate as rules_evaluate  # noqa: E402
from mj.joker_ev import hand_wait_cover  # noqa: E402
import mj.bot as bot  # noqa: E402
from mj.strategy import choose_discard as baseline_choose_discard  # noqa: E402

CSV_FIELDS = [
    "room_id", "game_id", "round_no", "seq", "seat", "uid", "cohort", "dtype",
    "is_dealer", "turn_no", "wall_left", "to_dead",
    "shanten_std", "shanten_7p", "shanten_route", "ukeire", "ukeire_live", "wait_cover",
    "to_baotou", "jokers", "pairs", "triplets", "lone", "melds", "chi_used",
    "chain", "piao_out", "joker_total",
    "opp_melds_max", "opp_melds_sum", "dealer_melds", "catch_play", "i_am_exempt",
    "visible_joker", "opp_tenpai_p1", "opp_tenpai_p2", "opp_tenpai_p3",
    "sess_score", "sess_rank", "gap_to_1st", "gap_to_4th", "rounds_left",
    "round_idx", "wall_remaining", "gap_to_next_up", "gap_to_next_down",
    "act_type", "act_tile", "act_cat", "our_policy_act", "our_policy_act_baseline_only", "tier_gap",
    "y_hu", "y_fan", "y_score", "y_bt", "y_tenpai_3", "y_opp_hu_4",
    "fan_from_score", "auto", "is_baotou",
]


# ---------------------------------------------------------------------------
# 手牌特征（统一输入：hand_tiles = 一手实际牌名列表，13 张稳定态）
# ---------------------------------------------------------------------------

def _shape_counts(hand_tiles):
    counts = to_counts(hand_tiles)
    pairs = triplets = lone = 0
    for i, n in enumerate(counts):
        if i == JOKER_IDX:
            continue
        if n == 2:
            pairs += 1
        elif n >= 3:
            triplets += 1
        elif n == 1:
            lone += 1
    return pairs, triplets, lone


def _hand_quality(hand_tiles, meld_groups):
    counts13 = to_counts(hand_tiles)
    shanten_std = shanten(counts13, meld_groups)
    shanten_7p = pair_shanten(counts13) if meld_groups == 0 else None
    shanten_route, waits = combined_route(counts13, meld_groups)
    hit, _total = hand_wait_cover(tuple(counts13), meld_groups)
    tb = to_baotou(counts13, meld_groups)
    is_bt = rules_baotou(counts13, meld_groups)
    return {
        "shanten_std": shanten_std, "shanten_7p": shanten_7p, "shanten_route": shanten_route,
        "ukeire": len(ukeire_fn(counts13, meld_groups)), "ukeire_live": len(waits),
        "wait_cover": hit, "to_baotou": tb, "is_baotou": is_bt,
    }


# ---------------------------------------------------------------------------
# 生产 snapshot 构造，直接复用 mj.bot.choose_discard（含 use_route 分流）
# ---------------------------------------------------------------------------

def _melds_payload(melds_all):
    return [[{"kind": g["kind"], "tiles": list(g["tiles"])} for g in seat_melds]
            for seat_melds in melds_all]


def _counterfactual_discard(seat, hand, melds_all, discards_all, dealer, chain_count,
                            catch_play, god_seat, wall_left):
    snapshot = {
        "seat": seat,
        "my_hand": list(hand),
        "dealer": dealer,
        "melds": _melds_payload(melds_all),
        "discards": [list(r) for r in discards_all],
        "wall_remaining": wall_left,
        "drawn_tile": hand[-1] if hand else None,
        "phase": "draw",
        "god": {"catch_play": bool(catch_play), "god_discarder_seat": god_seat,
                "chain_count": chain_count},
        "chain_count": chain_count,
    }
    try:
        decision = bot.choose_discard(snapshot, rules={})
    except Exception:
        return None
    return decision.get("tile") if decision else None


def _baseline_only_discard(hand, meld_groups):
    """复刻 tools/policy_diff.py 的调用方式，供第八节回归对账。"""
    try:
        return baseline_choose_discard(list(hand), meld_groups, 0, 0, {}, None)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 行构造
# ---------------------------------------------------------------------------

def _make_row(dtype, room_id, game_id, round_no, seq, seat, uid, cohort, is_dealer, turn_no,
              wall_left, hand_tiles, meld_groups, chain, piao_out, melds_all, discards_all,
              dealer_seat, catch_play, god_seat, act_type, act_tile, chi_used, auto=0):
    q = _hand_quality(hand_tiles, meld_groups)
    pairs, triplets, lone = _shape_counts(hand_tiles)
    jokers = hand_tiles.count(JOKER)

    opp_melds = [len(melds_all[s]) for s in range(4) if s != seat]
    opp_melds_max = max(opp_melds) if opp_melds else 0
    opp_melds_sum = sum(opp_melds)
    dealer_melds = len(melds_all[dealer_seat]) if dealer_seat is not None else None
    visible_joker = sum(r.count(JOKER) for r in discards_all) + sum(
        1 for s in range(4) for g in melds_all[s] for t in g["tiles"] if t == JOKER)

    row = {
        "room_id": room_id, "game_id": game_id, "round_no": round_no, "seq": seq,
        "seat": seat, "uid": uid, "cohort": cohort, "dtype": dtype,
        "is_dealer": int(bool(is_dealer)), "turn_no": turn_no, "wall_left": wall_left,
        "to_dead": wall_left - 20,
        "shanten_std": q["shanten_std"], "shanten_7p": q["shanten_7p"],
        "shanten_route": q["shanten_route"], "ukeire": q["ukeire"], "ukeire_live": q["ukeire_live"],
        "wait_cover": q["wait_cover"], "to_baotou": q["to_baotou"],
        "jokers": jokers, "pairs": pairs, "triplets": triplets, "lone": lone,
        "melds": meld_groups, "chi_used": chi_used,
        "chain": chain, "piao_out": piao_out, "joker_total": jokers + piao_out,
        "opp_melds_max": opp_melds_max, "opp_melds_sum": opp_melds_sum, "dealer_melds": dealer_melds,
        "catch_play": int(bool(catch_play)), "i_am_exempt": int(god_seat == seat),
        "visible_joker": visible_joker,
        "opp_tenpai_p1": None, "opp_tenpai_p2": None, "opp_tenpai_p3": None,
        "sess_score": None, "sess_rank": None, "gap_to_1st": None, "gap_to_4th": None,
        "rounds_left": None, "round_idx": round_no, "wall_remaining": wall_left,
        "gap_to_next_up": None, "gap_to_next_down": None,
        "act_type": act_type, "act_tile": act_tile, "act_cat": act_cat(act_tile) if act_tile else None,
        "our_policy_act": None, "our_policy_act_baseline_only": None, "tier_gap": None,
        "y_hu": None, "y_fan": None, "y_score": None, "y_bt": None, "y_tenpai_3": None,
        "y_opp_hu_4": None, "fan_from_score": None, "auto": auto,
        "is_baotou": int(bool(q["is_baotou"])),
        "_is_baotou": q["is_baotou"], "_shanten_route": q["shanten_route"],
    }
    return row


# ---------------------------------------------------------------------------
# 单局重放
# ---------------------------------------------------------------------------

def _apply_event(hands, melds, event):
    kind = event["type"]
    seat = event.get("seat")
    tile = event.get("tile")
    data = event.get("data") or {}
    if kind == "tile_drawn":
        hands[seat].append(tile)
    elif kind == "tile_discarded":
        hands[seat].remove(tile)
    elif kind == "chi":
        used = list(data.get("tiles") or [])
        used.remove(tile)
        for t in used:
            hands[seat].remove(t)
        melds[seat].append({"kind": "chi", "tiles": list(data.get("tiles") or [])})
    elif kind == "peng":
        for _ in range(2):
            hands[seat].remove(tile)
        melds[seat].append({"kind": "peng", "tiles": [tile, tile, tile]})
    elif kind == "gang":
        gk = data.get("kind")
        take = {"an": 4, "ming": 3, "bu": 1}[gk]
        for _ in range(take):
            hands[seat].remove(tile)
        if gk == "bu":
            for group in melds[seat]:
                if group["kind"] == "peng" and group["tiles"][0] == tile:
                    group["kind"] = "gang_bu"
                    group["tiles"] = [tile] * 4
                    break
        else:
            melds[seat].append({"kind": "gang", "tiles": [tile] * 4})


def replay_round(rnd, cohort_of, uids, compute_cf_uids, no_counterfactual, fan_true,
                  room_id, game_id, info):
    stats = defaultdict(int)
    start_hands = rnd["start_hands"]
    if rnd.get("truncated") or not start_hands:
        stats["dropped_truncated_or_nohand"] += 1
        return [], stats

    events = rnd["events"]
    auto_idx = mark_auto_discards(events)
    stats["auto_discards"] += len(auto_idx)
    stats["total_discards"] += sum(1 for e in events if e["type"] == "tile_discarded")

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

    winner_final_draw_seq = None
    if winner is not None and not is_draw:
        for ev in events:
            if ev["type"] == "tile_drawn" and ev.get("seat") == winner:
                winner_final_draw_seq = ev["seq"]

    draw_seqs_all = [ev["seq"] for ev in events if ev["type"] == "tile_drawn"]

    rows = []
    per_seat_rows = defaultdict(list)

    try:
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
                if result and idx not in auto_idx:
                    is_final_win = (winner == seat and seq == winner_final_draw_seq)
                    wall_left = 136 - total_start - drawn_count
                    row = _make_row(
                        dtype="hu_or_not", room_id=room_id, game_id=game_id,
                        round_no=rnd["round_no"], seq=seq, seat=seat, uid=uids[seat],
                        cohort=cohort_of(uids[seat]), is_dealer=(dealer == seat),
                        turn_no=turn_count[seat] + 1, wall_left=wall_left,
                        hand_tiles=list(hands[seat]), meld_groups=meld_groups,
                        chain=chain[seat], piao_out=piao_out[seat],
                        melds_all=melds, discards_all=discards_river, dealer_seat=dealer,
                        catch_play=catch_active, god_seat=god_seat,
                        act_type=("hu" if is_final_win else "decline_hu"), act_tile=tile,
                        chi_used=sum(1 for g in melds[seat] if g["kind"] == "chi"),
                    )
                    if not no_counterfactual and cohort_of(uids[seat]) in compute_cf_uids:
                        # our_policy_act：生产 can_hu 口径（非「有财必靠」时摸白即可胡）。
                        row["our_policy_act"] = "hu" if result else "discard"
                    rows.append(row)
                    per_seat_rows[seat].append(len(rows) - 1)
                _apply_event(hands, melds, event)
                turn_count[seat] += 1
                continue

            if kind == "tile_discarded":
                is_auto = idx in auto_idx
                cp_flag = bool(data.get("catch_play"))
                meld_groups = len(melds[seat])
                hand_before = list(hands[seat])
                wall_left = 136 - total_start - drawn_count
                stable13 = list(hand_before)
                stable13.remove(tile)
                is_baotou_after = rules_baotou(to_counts(stable13), meld_groups)

                if not is_auto:
                    row = _make_row(
                        dtype="discard", room_id=room_id, game_id=game_id,
                        round_no=rnd["round_no"], seq=seq, seat=seat, uid=uids[seat],
                        cohort=cohort_of(uids[seat]), is_dealer=(dealer == seat),
                        turn_no=turn_count[seat], wall_left=wall_left,
                        hand_tiles=stable13, meld_groups=meld_groups,
                        chain=chain[seat], piao_out=piao_out[seat],
                        melds_all=melds, discards_all=discards_river, dealer_seat=dealer,
                        catch_play=cp_flag, god_seat=god_seat,
                        act_type="discard", act_tile=tile,
                        chi_used=sum(1 for g in melds[seat] if g["kind"] == "chi"),
                    )
                    if not no_counterfactual and cohort_of(uids[seat]) in compute_cf_uids:
                        row["our_policy_act"] = _counterfactual_discard(
                            seat, hand_before, melds, discards_river, dealer, chain[seat],
                            cp_flag, god_seat, wall_left)
                        row["our_policy_act_baseline_only"] = _baseline_only_discard(hand_before, meld_groups)
                        currents = {}
                        for cand in set(hand_before):
                            rest = list(hand_before)
                            rest.remove(cand)
                            currents[cand] = route_shanten(to_counts(rest), meld_groups)
                        if currents:
                            best = min(currents.values())
                            row["tier_gap"] = currents.get(tile, best) - best
                    rows.append(row)
                    per_seat_rows[seat].append(len(rows) - 1)

                if cp_flag and not catch_active:
                    catch_active = True
                    god_seat = seat
                elif not cp_flag:
                    catch_active = False
                    god_seat = None

                if tile == JOKER and is_baotou_after:
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
        # end for
    except (ValueError, KeyError, TypeError, IndexError):
        stats["dropped_replay_error"] += 1
        return [], stats

    # -- 局级结果标签回填 --
    for seat_i in range(4):
        won = (winner == seat_i and not is_draw)
        score = round_scores[seat_i] if seat_i < len(round_scores) else 0
        per_base = 24 if dealer == seat_i else 10
        fan_back = round(score / float(per_base), 3) if (won and score and per_base) else None
        for ridx in per_seat_rows[seat_i]:
            r = rows[ridx]
            r["y_hu"] = int(won)
            r["y_fan"] = fan_true if won else 0
            r["y_score"] = score
            r["fan_from_score"] = fan_back

    for seat_i in range(4):
        idxs = per_seat_rows[seat_i]
        for pos, ridx in enumerate(idxs):
            row = rows[ridx]
            future = idxs[pos + 1:]
            bt_hit = any(rows[j].get("_is_baotou") for j in future)
            tenpai_hit = any(rows[j]["dtype"] == "discard" and rows[j].get("_shanten_route") is not None
                             and rows[j]["_shanten_route"] <= 0 for j in future[:3])
            row["y_bt"] = int(bt_hit)
            row["y_tenpai_3"] = int(tenpai_hit)

    draw_seqs_sorted = sorted(draw_seqs_all)
    for row in rows:
        seat_i = row["seat"]
        pos = bisect.bisect_right(draw_seqs_sorted, row["seq"])
        window_seqs = draw_seqs_sorted[pos:pos + 16]
        hit = False
        if (winner is not None and not is_draw and winner != seat_i
                and winner_final_draw_seq is not None and window_seqs):
            if window_seqs[0] <= winner_final_draw_seq <= window_seqs[-1]:
                hit = True
        row["y_opp_hu_4"] = int(hit)

    for row in rows:
        row.pop("_is_baotou", None)
        row.pop("_shanten_route", None)

    return rows, stats


def fill_session_fields(rows, rounds_info, max_round_no):
    """场内累计标签回填（场 = 一个 events 文件，同 4 人连打）。

    2026-09-24 追加任务：``rounds_left`` 改用**该文件实际出现过的最大
    round_no**推算，不假设固定 8 局（有些场因为掉线/提前结束局数更少，
    个别场因流局连庄局数可能略超 8）。``gap_to_next_up/down`` 是「追加
    任务」新增列：与排名上一名/下一名的分差（并列裁决用；主排序键是累计
    总分，见 docs/HANDOFF_CURRENT.md「正式赛晋级排序链是
    total_score → place_points → god_count」——局内方差策略在理论上只影响
    分/局的方差，不影响其期望，见 OFFLINE_REPORT.md「局面特征」一节）。
    """
    cum = [0, 0, 0, 0]
    by_round = defaultdict(list)
    for r in rows:
        by_round[r["round_no"]].append(r)
    for rno in sorted(by_round):
        info = rounds_info.get(rno)
        for r in by_round[rno]:
            seat = r["seat"]
            order = sorted(range(4), key=lambda s: -cum[s])  # 座位按名次排序
            rank_pos = order.index(seat)  # 0-based 名次
            scores_sorted = [cum[s] for s in order]
            r["sess_score"] = cum[seat]
            r["sess_rank"] = rank_pos + 1
            r["gap_to_1st"] = scores_sorted[0] - cum[seat]
            r["gap_to_4th"] = cum[seat] - scores_sorted[-1]
            r["gap_to_next_up"] = 0 if rank_pos == 0 else scores_sorted[rank_pos - 1] - cum[seat]
            r["gap_to_next_down"] = 0 if rank_pos == 3 else cum[seat] - scores_sorted[rank_pos + 1]
            r["rounds_left"] = max(0, max_round_no - rno)
        if info and not info.get("is_draw"):
            sc = info.get("scores") or [0, 0, 0, 0]
            for s in range(4):
                cum[s] += sc[s] if s < len(sc) else 0


# ---------------------------------------------------------------------------
# worker（模块顶层，macOS spawn 安全）
# ---------------------------------------------------------------------------

_WORKER_STATE = {}


def _init_worker(cohort_map, compute_cf_uids, no_counterfactual):
    _WORKER_STATE["cohort"] = cohort_map
    _WORKER_STATE["compute_cf_uids"] = compute_cf_uids
    _WORKER_STATE["no_cf"] = no_counterfactual


def process_file(path):
    cohort_map = _WORKER_STATE.get("cohort", {})
    compute_cf_uids = _WORKER_STATE.get("compute_cf_uids", {"top", "ours"})
    no_cf = _WORKER_STATE.get("no_cf", False)

    def cohort_of(uid):
        return cohort_map.get(uid, "other")

    stats = defaultdict(int)
    try:
        game = json.load(open(path, encoding="utf-8"))
    except (ValueError, OSError):
        stats["dropped_bad_json"] += 1
        return [], stats
    seats = game.get("seats") or []
    if len(seats) != 4:
        stats["dropped_bad_seats"] += 1
        return [], stats
    uids = [s.get("user_id") for s in seats]
    room_id = game.get("room_id")
    game_id = game.get("game_id")
    rounds_info = {r.get("round_no"): r for r in (game.get("rounds") or [])}
    round_nos = [r.get("round_no") for r in (game.get("rounds") or []) if r.get("round_no") is not None]
    max_round_no = max(round_nos) if round_nos else 0
    stats["game_round_count_%d" % len(round_nos)] += 1

    all_rows = []
    for rnd in merge_rounds(game):
        stats["rounds_seen"] += 1
        info = rounds_info.get(rnd["round_no"])
        if info is None:
            stats["dropped_no_round_info"] += 1
            continue
        fan_true = 0
        if not info.get("is_draw") and info.get("winner") is not None:
            found_fan = None
            for ev in rnd["events"]:
                if ev["type"] == "round_ended":
                    found_fan = (ev.get("data") or {}).get("fan")
            if found_fan is None:
                stats["missing_round_ended_fan"] += 1
            else:
                fan_true = found_fan
        rows, rstats = replay_round(rnd, cohort_of, uids, compute_cf_uids, no_cf, fan_true,
                                    room_id, game_id, info)
        for k, v in rstats.items():
            stats[k] += v
        if rows:
            all_rows.extend(rows)
    fill_session_fields(all_rows, rounds_info, max_round_no)
    stats["rows_emitted"] += len(all_rows)
    return all_rows, stats


# ---------------------------------------------------------------------------
# Phase 1: 排名扫描（轻量，单进程，只读 rounds）
# ---------------------------------------------------------------------------

def scan_rankings(files):
    score = defaultdict(lambda: [0, 0.0, ""])
    for path in files:
        try:
            game = json.load(open(path, encoding="utf-8"))
        except (ValueError, OSError):
            continue
        seats = game.get("seats") or []
        if len(seats) != 4:
            continue
        uids = [s.get("user_id") for s in seats]
        for i, uid in enumerate(uids):
            score[uid][2] = seats[i].get("name")
        for rnd in game.get("rounds") or []:
            if rnd.get("is_draw"):
                continue
            sc = rnd.get("scores") or [0, 0, 0, 0]
            for i, uid in enumerate(uids):
                score[uid][0] += 1
                score[uid][1] += sc[i] if i < len(sc) else 0
    return score


def build_cohort_map(score, top_uids_override=None):
    eligible = [(v[1] / v[0], uid) for uid, v in score.items() if v[0] >= 300 and uid != OUR_UID]
    eligible.sort(reverse=True)
    cohort = {}
    if top_uids_override:
        top_set = set(top_uids_override)
        for uid in top_set:
            cohort[uid] = "top"
        remaining = [u for _, u in eligible if u not in top_set]
    else:
        top_set = set(u for _, u in eligible[:10])
        for uid in top_set:
            cohort[uid] = "top"
        remaining = [u for _, u in eligible[10:]]
    bottom_set = set(remaining[-10:]) if remaining else set()
    for uid in bottom_set:
        cohort[uid] = "bottom"
    cohort[OUR_UID] = "ours"
    return cohort


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", nargs="*",
                    default=["tools/models/events/*.json", "models/events/*.json"])
    ap.add_argument("--out", default="data/analysis/decisions.csv")
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--limit", type=int, default=None, help="只跑前 N 个文件（profiling 用）")
    ap.add_argument("--no-counterfactual", action="store_true")
    ap.add_argument("--top-uids", nargs="*", default=None, help="覆盖自动排名的 top 名单（uid 列表）")
    args = ap.parse_args()

    t0 = time.time()
    files = discover_files(tuple(args.events), limit=args.limit)
    print("文件数（去重后）：%d" % len(files))

    score = scan_rankings(files)
    cohort_map = build_cohort_map(score, args.top_uids)
    n_top = sum(1 for v in cohort_map.values() if v == "top")
    n_bottom = sum(1 for v in cohort_map.values() if v == "bottom")
    print("cohort: top=%d bottom=%d ours=1（其余为 other）" % (n_top, n_bottom))
    t_rank = time.time()
    print("排名扫描耗时 %.1fs" % (t_rank - t0))

    compute_cf_uids = {"top", "ours"}
    os.makedirs(os.path.dirname(args.out), exist_ok=True)

    total_stats = defaultdict(int)
    n_rows = 0
    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        with Pool(args.jobs, initializer=_init_worker,
                  initargs=(cohort_map, compute_cf_uids, args.no_counterfactual)) as pool:
            for rows, stats in pool.imap_unordered(process_file, files, chunksize=2):
                for row in rows:
                    writer.writerow(row)
                n_rows += len(rows)
                for k, v in stats.items():
                    total_stats[k] += v

    t1 = time.time()
    print("决策行数：%d" % n_rows)
    print("统计：%s" % dict(total_stats))
    per_file = (t1 - t_rank) / max(1, len(files))
    print("总耗时 %.1fs（%d 文件，%.3fs/文件）" % (t1 - t0, len(files), per_file))
    if args.limit:
        est_full = per_file * 1250 + (t_rank - t0) * (1250.0 / max(1, len(files)))
        print("按此速率外推全量(~1250 文件)：约 %.1f 分钟" % (est_full / 60))
    print("已写出 %s" % args.out)


if __name__ == "__main__":
    main()
