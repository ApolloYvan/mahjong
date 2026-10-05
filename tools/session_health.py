"""对局健康体检：一条命令回答"这批房是打输的，还是没打上"。

背景见 docs/audit/STALL_2026-09-23.md：2026-09-23 的连续对战里，
Python 3.9 上 `socket.timeout` 不是 `TimeoutError`，三处 `except TimeoutError`
成为死代码，socket 超时被 `retries` 放大成数分钟阻塞，期间服务端替我们打牌。
两个房间因此 69~73% 的弃牌由服务端代打，胜率塌到 1~3/77 —— 那是弃权不是打输。
本工具把该判据固化，只读 events 与 logs，不联网。

    python3 tools/session_health.py                          # 最近一批（按文件时间）
    python3 tools/session_health.py --rooms a_xxx a_yyy
    python3 tools/session_health.py --events "tools/models/events/*.json" --gaps
"""
import argparse
import datetime as dt
import glob
import json
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

OUR_UID = "u_fd06550b5fb3"
# 验收线（docs/audit/STALL_2026-09-23.md §4）
MAX_DISCARD_TIMEOUT = 50      # 每房我方 discard 超时次数
MAX_AUTOPLAY_RATE = 0.10      # 我方弃牌中由服务端代打的比例
MAX_LOG_GAP_SECONDS = 60      # 决策日志中允许的最长整进程静默


def room_of(path):
    return os.path.basename(path).rsplit("_r1_", 1)[0]


def _paths(globs):
    """多个 glob 去重（同名文件可能同时存在于 models/ 和 tools/models/）。"""
    out, seen = [], set()
    for pattern in (globs if isinstance(globs, (list, tuple)) else [globs]):
        for path in sorted(glob.glob(pattern)):
            name = os.path.basename(path)
            if name in seen:
                continue
            seen.add(name)
            out.append(path)
    return out


def room_start(paths):
    """房间首个事件的 unix 时间戳，用于 --last 排序。"""
    first = {}
    for path in paths:
        room = room_of(path)
        if room in first:
            continue
        try:
            game = json.load(open(path, encoding="utf-8"))
        except (ValueError, OSError):
            continue
        for block in game.get("blocks", []):
            stamp = next((e["ts"] for e in block.get("events", []) if e.get("ts")), None)
            if stamp:
                first[room] = stamp
                break
    return first


def scan_rooms(events_globs, rooms=None):
    by_room = defaultdict(lambda: dict(tables=0, rounds=0, wins=0, discards=0,
                                       autoplay=0, to=Counter(), score=0))
    for path in _paths(events_globs):
        room = room_of(path)
        if rooms and room not in rooms:
            continue
        try:
            game = json.load(open(path, encoding="utf-8"))
        except (ValueError, OSError):
            continue
        seat = next((i for i, s in enumerate(game["seats"]) if s["user_id"] == OUR_UID), None)
        if seat is None:
            continue
        item = by_room[room]
        item["tables"] += 1
        for rnd in game["rounds"]:
            item["rounds"] += 1
            item["wins"] += (rnd.get("winner") == seat)
            item["score"] += (rnd.get("scores") or [0] * 4)[seat]
        for block in game["blocks"]:
            pending = False
            for event in block["events"]:
                if event.get("seat") != seat:
                    continue
                if event["type"] == "timeout":
                    kind = (event.get("data") or {}).get("kind")
                    item["to"][str(kind)] += 1
                    if kind == "discard":
                        pending = True
                elif event["type"] == "tile_discarded":
                    item["discards"] += 1
                    if pending:
                        item["autoplay"] += 1
                    pending = False
    return by_room


def log_gaps(log_glob, threshold):
    gaps = []
    prev = None
    for path in sorted(glob.glob(log_glob)):
        with open(path, encoding="utf-8", errors="replace") as source:
            for line in source:
                i = line.find('"time": "')
                if i < 0:
                    continue
                stamp = line[i + 9:line.find('"', i + 9)]
                try:
                    cur = dt.datetime.fromisoformat(stamp)   # 注意：字段是 32 字符含时区
                except ValueError:
                    continue
                if prev is not None and cur > prev and (cur - prev).total_seconds() >= threshold:
                    gaps.append((prev, cur, (cur - prev).total_seconds()))
                if prev is None or cur > prev:
                    prev = cur
    return gaps


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", nargs="*",
                        default=["models/events/*.json", "tools/models/events/*.json"],
                        help="采集工具可能写在 tools/models/events/，默认两处都扫")
    parser.add_argument("--last", type=int,
                        help="只看最近 N 个房（按首个事件时间排序），省得手打房间号")
    parser.add_argument("--rooms", nargs="*")
    parser.add_argument("--logs", default="logs/*.jsonl")
    parser.add_argument("--gaps", action="store_true", help="附带扫描决策日志的整进程静默")
    args = parser.parse_args()

    wanted = set(args.rooms) if args.rooms else None
    if args.last:
        starts = room_start(_paths(args.events))
        recent = sorted(starts, key=lambda r: starts[r])[-args.last:]
        wanted = set(recent) if wanted is None else (wanted & set(recent))
    by_room = scan_rooms(args.events, wanted)
    if not by_room:
        print("没有匹配到房间。检查 --events 路径（采集工具可能写在 tools/models/events/）")
        return

    print("%-18s %4s %5s %5s %8s %10s %12s %s"
          % ("房间", "桌", "局", "胜", "胜率", "分/局", "discard超时", "服务端代打"))
    print("-" * 92)
    bad = []
    tot = dict(rounds=0, wins=0, score=0, discards=0, autoplay=0)
    for room in sorted(by_room):
        d = by_room[room]
        if not d["rounds"]:
            continue
        rate = d["autoplay"] / max(1, d["discards"])
        dto = d["to"].get("discard", 0)
        flag = ""
        if dto > MAX_DISCARD_TIMEOUT or rate > MAX_AUTOPLAY_RATE:
            flag = "  <<< 报废"
            bad.append(room)
        else:
            for k in tot:
                tot[k] += d[k]
        print("%-18s %4d %5d %5d %7.1f%% %+10.3f %12d %10.0f%%%s"
              % (room, d["tables"], d["rounds"], d["wins"], 100 * d["wins"] / d["rounds"],
                 d["score"] / d["rounds"], dto, 100 * rate, flag))
    print("-" * 92)
    if tot["rounds"]:
        print("健康房合计: %d 局  胜率 %.1f%%  分/局 %+.3f   (代打 %.1f%%)"
              % (tot["rounds"], 100 * tot["wins"] / tot["rounds"],
                 tot["score"] / tot["rounds"], 100 * tot["autoplay"] / max(1, tot["discards"])))
    print("对照基线: 修复前 21 房 1669 局  胜率 20.7%  分/局 -0.838   打平需 25.4%")
    if bad:
        print("\n报废房（超过验收线 discard超时>%d 或 代打>%.0f%%）: %s"
              % (MAX_DISCARD_TIMEOUT, 100 * MAX_AUTOPLAY_RATE, ", ".join(bad)))
        print("这些房不是打输，是 bot 没打上——不要把它们算进策略评估。")

    if args.gaps:
        gaps = log_gaps(args.logs, MAX_LOG_GAP_SECONDS)
        print("\n决策日志中 >=%d 秒的整进程静默: %d 次" % (MAX_LOG_GAP_SECONDS, len(gaps)))
        for a, b, d in gaps[-12:]:
            print("   %s -> %s   %6.0f 秒" % (str(a)[:19], str(b)[11:19], d))


if __name__ == "__main__":
    main()
