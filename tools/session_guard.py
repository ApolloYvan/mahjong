"""进程存活检测——只读工具，只做检测与告警，不做自动重启。

背景（2026-09-22 审计）：房间 a_282b85347354 的决策日志在 2026-09-22T16:04:15
戛然而止——没有 result、没有 error，服务端事件流却一直跑到 16:14，该房后
5.5/8 局由服务端代打（timeout{kind:"discard"} 远高于其余房间）。当前任何非
正常退出都不留痕迹，本工具补上这块可观测性缺口。

两种模式，信号来源不同：

- 实时模式（默认）：只用当天还在增长的 logs/<date>.jsonl——房间的
  models/events/ 结算文件此时通常还没有落地。规则很简单：某房间超过
  --stale-seconds（默认 90）秒没有新的 decision/result/error 记录，且从未见过
  kind="result" 且 payload.state.snapshot.phase == "finished" → 判 degraded。

- --replay 模式（离线复盘，用于自证 B1）：历史房间的 logs 与 events 都已完整
  落地，可以用服务端 events 里的 timeout{kind:"discard"}（服务端代打次数，
  ground truth）做更强的信号——本地日志的"没有新记录"在复盘时已经没有意义
  （文件早就不再增长）。一个房间只要满足下列任一条即判 degraded：
    (a) 房内存在任何一局本地日志从未出现过 kind="result" 且 finished=true
        （对应 a_282b85347354：进程死了，一局都没等到结算）；
    (b) 我方 timeout{kind:"discard"}/局 超过 --timeout-threshold（默认
        0.15，实测 21 房中 19 房 ≤0.101，另外两房 0.487 与 5.949，阈值取在
        两者之间）（对应 a_8afae15f071d：进程没死，但响应经常慢到被服务端
        代打）。

用法：
    python3 tools/session_guard.py --logs "logs/*.jsonl"
    python3 tools/session_guard.py --replay --logs "logs/*.jsonl" --events "models/events/*.json"
"""
import argparse
import glob
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone

OUR_UID = "u_fd06550b5fb3"
DEFAULT_STALE_SECONDS = 90.0
DEFAULT_TIMEOUT_THRESHOLD = 0.15
LIVENESS_KINDS = ("decision", "result", "error")


def _room_of(game_id):
    parts = (game_id or "").split("_")
    if len(parts) < 2:
        return game_id
    return parts[0] + "_" + parts[1]


def _is_finished_result(kind, payload):
    if kind != "result":
        return False
    snap = ((payload.get("state") or {}).get("snapshot")) or {}
    return snap.get("phase") == "finished"


def _iter_log_records(logs_glob):
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
                if obj.get("kind") not in LIVENESS_KINDS:
                    continue
                payload = obj.get("payload") or {}
                game_id = payload.get("game_id")
                if not game_id:
                    continue
                try:
                    dt = datetime.fromisoformat(obj["time"])
                except (KeyError, ValueError):
                    continue
                yield dt, obj["kind"], game_id, payload


def collect_room_liveness(logs_glob):
    """room -> {"last_time": datetime, "ever_finished": bool}（跨该房间全部 game_id 聚合）。"""
    rooms = defaultdict(lambda: {"last_time": None, "ever_finished": False})
    for dt, kind, game_id, payload in _iter_log_records(logs_glob):
        room = _room_of(game_id)
        info = rooms[room]
        if info["last_time"] is None or dt > info["last_time"]:
            info["last_time"] = dt
        if _is_finished_result(kind, payload):
            info["ever_finished"] = True
    return rooms


def collect_game_finished(logs_glob):
    """game_id -> ever_finished bool（复盘模式需要按局判断，不能只看房间聚合——
    房间里有一局没结算，其余局结算了，房间聚合会把问题冲掉）。"""
    games = defaultdict(bool)
    for _dt, kind, game_id, payload in _iter_log_records(logs_glob):
        if _is_finished_result(kind, payload):
            games[game_id] = True
        elif game_id not in games:
            games[game_id] = False
    return games


def collect_timeout_rate(events_glob):
    """room -> (timeout_discard_count, round_count)，我方 seat 由每局 seats[].user_id 定位。"""
    timeout_count = defaultdict(int)
    round_count = defaultdict(int)
    for path in sorted(glob.glob(events_glob)):
        try:
            data = json.load(open(path, encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        seats = data.get("seats")
        if not isinstance(seats, list):
            continue
        our_idx = None
        for i, seat in enumerate(seats):
            if isinstance(seat, dict) and seat.get("user_id") == OUR_UID:
                our_idx = i
        if our_idx is None:
            continue
        game_id = data.get("game_id")
        room = _room_of(game_id)
        round_count[room] += len(data.get("rounds") or [])
        for block in data.get("blocks") or []:
            for ev in block.get("events") or []:
                if ev.get("type") != "timeout" or ev.get("seat") != our_idx:
                    continue
                if (ev.get("data") or {}).get("kind") == "discard":
                    timeout_count[room] += 1
    return timeout_count, round_count


def live_check(logs_glob, stale_seconds=DEFAULT_STALE_SECONDS, now=None):
    """实时模式：返回 room -> degraded(bool)。now 缺省为当前 UTC 时间（测试可注入固定值）。"""
    now = now or datetime.now(timezone.utc)
    rooms = collect_room_liveness(logs_glob)
    result = {}
    for room, info in rooms.items():
        last_time = info["last_time"]
        if last_time is None:
            continue
        silent_for = (now - last_time).total_seconds()
        degraded = silent_for > stale_seconds and not info["ever_finished"]
        result[room] = degraded
    return result


def replay_report(logs_glob, events_glob, timeout_threshold=DEFAULT_TIMEOUT_THRESHOLD):
    """复盘模式：返回 room -> {"degraded": bool, "reason": [...], "timeout_discard_per_round": float,
    "any_game_never_finished": bool}。"""
    games_finished = collect_game_finished(logs_glob)
    timeout_count, round_count = collect_timeout_rate(events_glob)

    rooms_never_finished = defaultdict(bool)
    for game_id, finished in games_finished.items():
        if not finished:
            rooms_never_finished[_room_of(game_id)] = True

    report = {}
    # 作用域冻结：只判定 models/events/ 里实际存在的房间（与 pair_route_metric.py
    # 口径一致）。logs/ 会持续增长，出现没有对应 events 的新房间是正常的，不能
    # 把它们当成"从未结算"误判为 degraded。
    for room in sorted(round_count):
        rounds = round_count.get(room, 0)
        rate = (timeout_count.get(room, 0) / rounds) if rounds else 0.0
        never_finished = bool(rooms_never_finished.get(room))
        reasons = []
        if never_finished:
            reasons.append("local_log_never_reached_finished_result")
        if rate > timeout_threshold:
            reasons.append(f"timeout_discard_per_round={rate:.3f}>threshold={timeout_threshold}")
        report[room] = {
            "degraded": bool(reasons),
            "reason": reasons,
            "timeout_discard_per_round": rate,
            "any_game_never_finished": never_finished,
        }
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--logs", default="logs/*.jsonl")
    parser.add_argument("--events", default="models/events/*.json")
    parser.add_argument("--replay", action="store_true", help="离线复盘历史 logs+events，而不是实时检查")
    parser.add_argument("--stale-seconds", type=float, default=DEFAULT_STALE_SECONDS)
    parser.add_argument("--timeout-threshold", type=float, default=DEFAULT_TIMEOUT_THRESHOLD)
    args = parser.parse_args()

    if args.replay:
        report = replay_report(args.logs, args.events, args.timeout_threshold)
        degraded_rooms = [room for room, info in report.items() if info["degraded"]]
        print(json.dumps(report, ensure_ascii=False, indent=2))
        for room in degraded_rooms:
            print(f"DEGRADED: {room} reasons={report[room]['reason']}", file=sys.stderr)
        print(f"共 {len(report)} 房，{len(degraded_rooms)} 房 degraded: {degraded_rooms}")
        return 1 if degraded_rooms else 0

    result = live_check(args.logs, args.stale_seconds)
    degraded_rooms = [room for room, degraded in result.items() if degraded]
    for room in degraded_rooms:
        print(f"DEGRADED: {room} 超过 {args.stale_seconds}s 无新记录且未结算", file=sys.stderr)
    if not degraded_rooms:
        print(f"OK: {len(result)} 房均正常")
    return 1 if degraded_rooms else 0


if __name__ == "__main__":
    sys.exit(main())
