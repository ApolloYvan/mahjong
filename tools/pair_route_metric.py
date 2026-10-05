"""七对路线劫持门清弃牌分层——只读诊断工具（不写 models/ 或 logs/）。

用法:
    python3 tools/pair_route_metric.py --logs "logs/*.jsonl" --events "models/events/*.json" --baseline /tmp/before.json
    python3 tools/pair_route_metric.py --compare /tmp/before.json /tmp/after.json

统计口径见 docs/audit/IMPL_PROMPT_P0_2026-09-22.md 第 2 节。
"""
import argparse
import glob
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mj.shanten import shanten, pair_shanten
from mj.tiles import to_counts

try:
    from mj.shanten import pair_route_allowed as _pair_route_allowed
except ImportError:
    _pair_route_allowed = None

OUR_UID = "u_fd06550b5fb3"


def _room_of(game_id):
    """room_id 由 game_id 的前两段派生（如 'a_d4dba30c1a75_r1_b7_t0' -> 'a_d4dba30c1a75'）。
    不信任 JSON 里的 room_id 字段——部分 events 文件该字段为 null（见
    a_ddb4e75f4167_r1_b2_t0.json），文件名/game_id 前缀是唯一可靠来源。"""
    parts = (game_id or "").split("_")
    if len(parts) < 2:
        return game_id
    return parts[0] + "_" + parts[1]


def collect_room_scope(events_glob):
    """作用域冻结：models/events/ 里存在的房间集合是本次分析的全集。
    logs/ 会持续增长（新房间尚无对应 events），必须按这个集合过滤 decision_level /
    round_level，否则统计会随时间漂移。"""
    rooms = set()
    for path in sorted(glob.glob(events_glob)):
        try:
            data = json.load(open(path, encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        game_id = data.get("game_id")
        if game_id:
            rooms.add(_room_of(game_id))
    return rooms


def _pair_allowed_new(counts, meld_groups, pair_leads):
    """新口径；第 2 步实现 mj.shanten.pair_route_allowed 之前，占位返回旧口径
    pair_leads，保证工具在改动 mj/ 之前就能先跑出基线（第 0 节要求）。"""
    if _pair_route_allowed is None:
        return pair_leads
    return _pair_route_allowed(counts, meld_groups)


def collect_decisions(logs_glob, room_scope=None):
    """返回 decisions: list of dict，以及 per_round: (game_id, round_no) -> {pair_leads_count, pair_allowed_count, total}

    room_scope: 若提供，只统计 room 在此集合内的房间（作用域冻结，见 collect_room_scope）。"""
    decisions = []
    per_round = defaultdict(lambda: {"pair_leads": 0, "pair_allowed_new": 0, "total": 0})
    for path in sorted(glob.glob(logs_glob)):
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("kind") != "decision":
                    continue
                payload = obj.get("payload") or {}
                decision = payload.get("decision") or {}
                if decision.get("action") != "discard":
                    continue
                game_id = payload.get("game_id", "")
                if room_scope is not None and _room_of(game_id) not in room_scope:
                    continue
                seat = payload.get("seat")
                melds = payload.get("melds")
                if not isinstance(melds, list) or seat is None or seat >= len(melds):
                    continue
                meld_groups = len(melds[seat] or [])
                if meld_groups != 0:
                    continue
                hand = list(payload.get("hand") or [])
                tile = decision.get("tile")
                if tile is None or tile not in hand:
                    continue
                hand.remove(tile)
                if len(hand) != 13:
                    continue
                counts = to_counts(hand)
                std = shanten(counts, 0)
                pair = pair_shanten(counts)
                pair_leads = pair < std
                pair_allowed = _pair_allowed_new(counts, 0, pair_leads)
                round_no = payload.get("round_no")
                decisions.append({
                    "game_id": game_id, "round_no": round_no,
                    "pair_leads": pair_leads, "pair_allowed_new": pair_allowed,
                })
                key = (game_id, round_no)
                per_round[key]["total"] += 1
                if pair_leads:
                    per_round[key]["pair_leads"] += 1
                if pair_allowed:
                    per_round[key]["pair_allowed_new"] += 1
    return decisions, per_round


def collect_outcomes(events_glob):
    """(game_id, round_no) -> {"winner_is_us": bool, "score": int}，按 seats[].user_id 定位我方，
    不用物理座位号。round_ended 按 (game_id, round_no) 去重。"""
    outcomes = {}
    for path in sorted(glob.glob(events_glob)):
        try:
            data = json.load(open(path, encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        seats = data.get("seats")
        if not isinstance(seats, list) or len(seats) != 4:
            continue
        our_idx = None
        for i, s in enumerate(seats):
            if isinstance(s, dict) and s.get("user_id") == OUR_UID:
                our_idx = i
        if our_idx is None:
            continue
        game_id = data.get("game_id")
        for r in data.get("rounds") or []:
            round_no = r.get("round_no")
            key = (game_id, round_no)
            if key in outcomes:
                continue  # 分页 block 之间可能重复，按 (game_id, round_no) 去重
            winner = r.get("winner")
            scores = r.get("scores") or [0, 0, 0, 0]
            outcomes[key] = {
                "winner_is_us": winner is not None and winner == our_idx,
                "score": scores[our_idx] if our_idx < len(scores) else 0,
                "is_draw": bool(r.get("is_draw")),
            }
    return outcomes


def _bucket(count):
    if count == 0:
        return "0"
    if count <= 5:
        return "1-5"
    return "6+"


def build_round_level(per_round, outcomes, field):
    buckets = defaultdict(lambda: {"rounds": 0, "wins": 0, "score_sum": 0})
    for key, info in per_round.items():
        outcome = outcomes.get(key)
        if outcome is None:
            continue
        bucket = _bucket(info[field])
        buckets[bucket]["rounds"] += 1
        buckets[bucket]["score_sum"] += outcome["score"]
        if outcome["winner_is_us"]:
            buckets[bucket]["wins"] += 1
    result = {}
    for name in ("0", "1-5", "6+"):
        b = buckets.get(name, {"rounds": 0, "wins": 0, "score_sum": 0})
        rounds = b["rounds"]
        result[name] = {
            "rounds": rounds,
            "win_rate": (b["wins"] / rounds) if rounds else None,
            "score_per_round": (b["score_sum"] / rounds) if rounds else None,
        }
    return result


def build_report(logs_glob, events_glob):
    room_scope = collect_room_scope(events_glob)
    decisions, per_round = collect_decisions(logs_glob, room_scope=room_scope)
    outcomes = collect_outcomes(events_glob)
    total = len(decisions)
    pair_leads_count = sum(1 for d in decisions if d["pair_leads"])
    pair_allowed_count = sum(1 for d in decisions if d["pair_allowed_new"])
    return {
        "decision_level": {
            "total_open_discards": total,
            "pair_leads_ratio": (pair_leads_count / total) if total else None,
            "pair_allowed_new_ratio": (pair_allowed_count / total) if total else None,
        },
        "round_level": build_round_level(per_round, outcomes, "pair_leads"),
        "round_level_new": build_round_level(per_round, outcomes, "pair_allowed_new"),
    }


def _fmt_pct(x):
    return "n/a" if x is None else f"{x:.1%}"


def print_compare(before, after):
    print("=== decision_level ===")
    for k in ("pair_leads_ratio", "pair_allowed_new_ratio"):
        print(f"{k}: before={_fmt_pct(before['decision_level'].get(k))} after={_fmt_pct(after['decision_level'].get(k))}")
    for level in ("round_level", "round_level_new"):
        print(f"=== {level} ===")
        for bucket in ("0", "1-5", "6+"):
            b = before[level].get(bucket, {})
            a = after[level].get(bucket, {})
            print(f"[{bucket}] rounds: before={b.get('rounds')} after={a.get('rounds')} | "
                  f"win_rate: before={_fmt_pct(b.get('win_rate'))} after={_fmt_pct(a.get('win_rate'))} | "
                  f"score/round: before={b.get('score_per_round')} after={a.get('score_per_round')}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--logs", default="logs/*.jsonl")
    parser.add_argument("--events", default="models/events/*.json")
    parser.add_argument("--baseline", help="输出基线 JSON 到指定路径")
    parser.add_argument("--compare", nargs=2, metavar=("OLD", "NEW"), help="对比两个已生成的基线 JSON")
    args = parser.parse_args()

    if args.compare:
        before = json.load(open(args.compare[0], encoding="utf-8"))
        after = json.load(open(args.compare[1], encoding="utf-8"))
        print_compare(before, after)
        return

    report = build_report(args.logs, args.events)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.baseline:
        with open(args.baseline, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
