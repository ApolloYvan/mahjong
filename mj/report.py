"""基于完整测试房结果，生成每玩家胜率、得分与事件统计。

P0 修复（实战可靠性收敛任务七）：
1. 以去重后的 ``round_ended`` 事件（``blocks[*].events[*]``）作为结算真相，
   不再从事件文件顶层 ``data["rounds"]`` 读取——该字段本身是不完整的二次
   汇总（真实数据集中丢失了一次流局，见 docs/refactor/LIVE_TEST_001.md
   §6）。去重键为 ``(game_id, round_no)``，同一局同一轮只计一次。
2. 用每场事件文件的 ``seats``（``[{"user_id":..., "name":...}, ...]``，
   下标即物理座位号）把物理座位映射到真实玩家身份，按 ``user_id`` 汇总
   胜负/得分——十场对局中四名玩家的物理座位是轮换的，同一玩家在不同场
   次坐在不同座位，按座位号直接汇总会把四个不同玩家的数据混在一起
   （历史 bug，已废弃的口径见 ``seat_breakdown`` 字段的说明）。
3. ``seat_breakdown``（按物理座位聚合的旧口径）仍保留输出，但仅作为
   "物理座位维度的参考视图"，不代表任何单一真实玩家的战绩——调用方不得
   把它当作玩家结论使用。
"""
import glob
import json
import os
from collections import Counter, defaultdict


def _extract_round_ended_events(data):
    """从单个事件文件（``pull_all_events``/测试房采集格式）中提取去重后的
    ``round_ended`` 事件列表：{round_no, is_draw, winner_seat, scores}。
    去重键为 round_no（同一局同一轮的 round_ended 只保留首次出现的一条，
    防御同一事件被重复采集两次的场景）。"""
    seen_rounds = set()
    events = []
    for block in data.get("blocks", []) or []:
        for event in block.get("events", []) or []:
            if event.get("type") != "round_ended":
                continue
            payload = event.get("data") or {}
            round_no = payload.get("round_no")
            key = round_no if round_no is not None else event.get("seq")
            if key in seen_rounds:
                continue
            seen_rounds.add(key)
            events.append({
                "round_no": round_no,
                "is_draw": bool(payload.get("draw")),
                "winner_seat": event.get("seat") if event.get("seat", -1) >= 0 else None,
                "scores": payload.get("scores") or [],
            })
    return events


def build_report(pattern="models/events/*.json", output="models/report.json", room_id=None):
    games = 0
    rounds_total = 0
    draws = 0
    events = Counter()

    # 旧口径（仅供参考，见模块文档字符串）：按物理座位号聚合。
    seat_wins = Counter()
    seat_scores = defaultdict(int)

    # 新口径（权威）：按真实玩家 user_id 聚合。
    player_wins = Counter()
    player_scores = defaultdict(int)
    player_names = {}
    # (game_id, round_no) 级别去重，防止同一局事件文件在不同 glob 匹配路径
    # 下被重复统计（例如同时匹配到未压缩与历史归档两份拷贝）。
    seen_game_rounds = set()

    for path in glob.glob(pattern):
        with open(path, encoding="utf-8") as source:
            data = json.load(source)
        if not isinstance(data, dict):
            continue
        if room_id and data.get("room_id") != room_id:
            continue
        games += 1
        for block in data.get("blocks", []):
            for event in block.get("events", []):
                events[event.get("type", "unknown")] += 1

        game_id = data.get("game_id") or os.path.splitext(os.path.basename(path))[0]
        seats = data.get("seats") or []

        for round_event in _extract_round_ended_events(data):
            dedup_key = (game_id, round_event["round_no"])
            if dedup_key in seen_game_rounds:
                continue
            seen_game_rounds.add(dedup_key)
            rounds_total += 1
            if round_event["is_draw"]:
                draws += 1
                continue
            winner_seat = round_event["winner_seat"]
            if winner_seat is not None:
                seat_wins[str(winner_seat)] += 1
                if winner_seat < len(seats):
                    winner_uid = seats[winner_seat].get("user_id")
                    if winner_uid:
                        player_wins[winner_uid] += 1
                        player_names.setdefault(winner_uid, seats[winner_seat].get("name"))
            for seat, score in enumerate(round_event["scores"]):
                seat_scores[str(seat)] += score
                if seat < len(seats):
                    uid = seats[seat].get("user_id")
                    if uid:
                        player_scores[uid] += score
                        player_names.setdefault(uid, seats[seat].get("name"))

    players = {}
    all_uids = set(player_wins) | set(player_scores)
    for uid in all_uids:
        losses = 0
        wins = player_wins.get(uid, 0)
        score = player_scores.get(uid, 0)
        players[uid] = {
            "name": player_names.get(uid),
            "wins": wins,
            "score": score,
        }

    report = {
        "games": games,
        "rounds": rounds_total,
        "draws": draws,
        "events": dict(events),
        # 权威口径：按真实玩家 user_id 聚合（任务七要求的修复结果）。
        "players": players,
        # 旧口径：保留字段名兼容历史调用方，但语义仍按物理座位聚合——
        # 不代表任何单一真实玩家的战绩，仅供参考。
        "wins": dict(seat_wins),
        "scores": dict(seat_scores),
        "seat_breakdown": {
            "note": "本节按物理座位号（0-3）聚合，同一座位号在不同场次"
                    "代表不同真实玩家（座位轮换）。仅作参考视图，不得当作"
                    "任何单一玩家的战绩结论——权威结果见 players 字段。",
            "wins_by_seat": dict(seat_wins),
            "scores_by_seat": dict(seat_scores),
        },
    }
    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
    with open(output, "w", encoding="utf-8") as target:
        json.dump(report, target, ensure_ascii=False, indent=2)
    return report
