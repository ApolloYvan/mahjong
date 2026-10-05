"""一条命令复盘一批房：采集 → 每房首名 → 我们的表现 → S1 → 每个首名的全历史画像。

    python3 tools/room_report.py a_xxx a_yyy                         # 已采集过
    python3 tools/room_report.py a_xxx a_yyy --server https://10.240.169.190:18080   # 先采集
    python3 tools/room_report.py a_xxx --player 爆头研究所             # 额外指定要看的人

首名自动识别（每房累计分最高者，不含我们）。依次调用现有工具，不重复实现：
pipeline.py collect / batch_review.py / joker_playbook.py / head_to_head.py /
score_attribution.py，再加一段"4番+牌型拆分"（首名 vs 我们）。
"""
import argparse
import glob
import json
import os
import subprocess
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from batch_review import OUR, load_room, scan_game  # noqa: E402
from mining_common import seat_names  # noqa: E402

PY = sys.executable


def run(title, args):
    print("\n" + "=" * 100)
    print("【%s】" % title)
    print("=" * 100, flush=True)
    subprocess.run([PY] + args)


def room_files(rooms):
    files = []
    for room in rooms:
        files += sorted(glob.glob("models/events/%s_*.json" % room)
                        or glob.glob("tools/models/events/%s_*.json" % room))
    return files


def files_with(name):
    out, seen = [], set()
    for path in glob.glob("tools/models/events/*.json") + glob.glob("models/events/*.json"):
        base = os.path.basename(path)
        if base in seen:
            continue
        seen.add(base)
        with open(path, encoding="utf-8", errors="replace") as source:
            if name in source.read():
                out.append(path)
    return out


def champions(rooms):
    names = []
    for room in rooms:
        totals = Counter()
        for game in load_room(room).values():
            scan_game(game, totals, lambda _n: Counter())
        for name, _ in totals.most_common():
            if name != OUR:
                if name not in names:
                    names.append(name)
                break
    return names


def big_hands(paths, players):
    stat = {p: Counter() for p in players}
    wins = Counter()
    for path in paths:
        try:
            with open(path, encoding="utf-8") as source:
                game = json.JSONDecoder().raw_decode(source.read())[0]
        except (OSError, ValueError):
            continue
        names = seat_names(game)
        for block in game.get("blocks", []):
            for ev in block.get("events", []):
                if ev.get("type") != "round_ended":
                    continue
                data, seat = ev.get("data") or {}, ev.get("seat")
                fan = data.get("fan") or 0
                if seat is None or not fan or not (0 <= seat < len(names)):
                    continue
                name = names[seat]
                if name in stat:
                    wins[name] += 1
                    if fan >= 4:
                        stat[name]["·".join(data.get("detail") or []) + " =%d番" % fan] += 1
    for name in players:
        print("%s：胡 %d 次，其中 4番+ %d 次" % (name, wins[name], sum(stat[name].values())))
        for pattern, n in stat[name].most_common(8):
            print("    %3d  %s" % (n, pattern))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("rooms", nargs="+")
    ap.add_argument("--server", help="给了就先采集这些房")
    ap.add_argument("--player", nargs="*", default=[], help="额外要看的玩家名")
    ap.add_argument("--logs", default="logs/*.jsonl")
    args = ap.parse_args()

    if args.server:
        run("采集", ["tools/pipeline.py", "collect", args.server] + args.rooms)

    run("1. 每房首名 / 我们 vs 首名 vs 其余 / S1 触发结局",
        ["tools/batch_review.py"] + args.rooms + ["--logs", args.logs])
    files = room_files(args.rooms)
    if files:
        run("2. 本批房：持财神时的爆头率（财神数 × 副露）", ["tools/joker_playbook.py", "--events"] + files)

    players = champions(args.rooms) + [p for p in args.player if p]
    players = list(dict.fromkeys(players))
    for i, name in enumerate(players, 3):
        paths = files_with(name)
        run("%d. 首名「%s」全历史（%d 个文件）" % (i, name, len(paths)), ["tools/head_to_head.py", "--player", name])
        if not paths:
            continue
        run("%d.1 「%s」 vs 我们：分从哪来" % (i, name), ["tools/score_attribution.py", "--events"] + paths)
        run("%d.2 「%s」 vs 我们：持财神爆头率" % (i, name), ["tools/joker_playbook.py", "--events"] + paths)
        print("\n【%d.3 「%s」 vs 我们：4番+ 牌型拆分（同桌对局）】" % (i, name))
        big_hands(paths, [name, OUR])


if __name__ == "__main__":
    main()
