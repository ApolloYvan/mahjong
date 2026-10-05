# -*- coding: utf-8 -*-
"""对某个对手的同桌交手记录：我们到底有没有跟他打过、打过多少、各自打成什么样。

为什么要有这个：周榜前列的名字在本地语料里常常**一局都没有**（玄武-2346 在
1238 个对局文件里出现 0 次），而"我们跟他差在哪"这类分析必须先确认样本存在，
否则后面所有结论都是空中楼阁。本工具一次扫完四个数据源，不联网。

    python3 tools/head_to_head.py --player 玄武-2346     # 查一个人
    python3 tools/head_to_head.py                        # 列出所有同桌过的对手
    python3 tools/head_to_head.py --logs                 # 额外扫决策日志（慢，~1GB/天）
"""
import argparse
import glob
import io
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

OUR_UID = "u_fd06550b5fb3"
OUR_NAME = "重生之我是雀神"


def _load_concat(path):
    """一个文件里可能装着多个 JSON 对象（data/replay.jsonl 是首尾相接的缩进 JSON）。"""
    try:
        text = io.open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return []
    decoder, out, i, n = json.JSONDecoder(), [], 0, len(text)
    while i < n:
        while i < n and text[i] in " \t\r\n":
            i += 1
        if i >= n:
            break
        try:
            obj, i = decoder.raw_decode(text, i)
        except ValueError:
            break
        if isinstance(obj, list):
            out.extend(g for g in obj if isinstance(g, dict))
        elif isinstance(obj, dict):
            out.append(obj)
    return out


def _fan(score, is_dealer):
    per = 24 if is_dealer else 10
    return max(1, int(round(score / float(per)))) if score > 0 else 0


def _rounds_events(game):
    """blocks/events 形态：产出 (round_no, dealer, winner, is_draw, scores)。"""
    dealer_of = {}
    for block in game.get("blocks") or []:
        dealer_of.setdefault(block.get("round_no"), block.get("dealer"))
    for rnd in game.get("rounds") or []:
        yield (rnd.get("round_no"), rnd.get("dealer", dealer_of.get(rnd.get("round_no"))),
               rnd.get("winner"), bool(rnd.get("is_draw")), rnd.get("scores") or [0] * 4)


def _rounds_frames(game):
    """带逐帧 snapshot 的形态：一条记录就是一局，结算在 round_ended 那一帧。"""
    for frame in game.get("frames") or []:
        ev = frame.get("ev") or {}
        if ev.get("type") != "round_ended":
            continue
        data = ev.get("data") or {}
        yield (data.get("round_no"), data.get("dealer"), ev.get("seat"),
               bool(data.get("draw")), data.get("scores") or [0] * 4)
        return


def scan_file(path, games):
    """返回 (对手uid -> [名字, 同桌局数, 我们胜, 我们总分, 他胜, 他总分, 他番合计, 房间集合])"""
    acc = {}
    for game in games:
        seats = game.get("seats") or []
        if len(seats) != 4:
            continue
        uids = [s.get("user_id") for s in seats]
        names = [s.get("name") or s.get("user_id") or "?" for s in seats]
        if OUR_UID not in uids:
            continue
        us = uids.index(OUR_UID)
        room = (game.get("game_id") or os.path.basename(path)).split("_r")[0]
        rounds = list(_rounds_frames(game)) or list(_rounds_events(game))
        for _rno, dealer, winner, is_draw, scores in rounds:
            if is_draw or winner is None:
                continue
            for seat in range(4):
                if seat == us:
                    continue
                cell = acc.setdefault(uids[seat],
                                      [names[seat], 0, 0, 0.0, 0, 0.0, 0, set()])
                cell[1] += 1
                cell[3] += scores[us] if us < len(scores) else 0
                cell[5] += scores[seat] if seat < len(scores) else 0
                cell[7].add(room)
                if winner == us:
                    cell[2] += 1
                elif winner == seat:
                    cell[4] += 1
                    cell[6] += _fan(scores[seat], dealer == seat)
    return acc


def merge(into, other):
    for uid, cell in other.items():
        dst = into.setdefault(uid, [cell[0], 0, 0, 0.0, 0, 0.0, 0, set()])
        dst[0] = cell[0]
        for i in (1, 2, 4, 6):
            dst[i] += cell[i]
        for i in (3, 5):
            dst[i] += cell[i]
        dst[7] |= cell[7]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--player", help="对手名字，支持子串匹配")
    ap.add_argument("--sources", nargs="*", default=[
        "tools/models/events/*.json", "models/events/*.json",
        "models/replays/*.json", "data/replay.jsonl"])
    ap.add_argument("--logs", action="store_true",
                    help="额外扫 logs/*.jsonl 找名字（只回答'出现过没有'，不统计战绩）")
    ap.add_argument("--top", type=int, default=25)
    a = ap.parse_args()

    total, seen, files = {}, set(), 0
    for pattern in a.sources:
        for path in sorted(glob.glob(pattern)):
            key = os.path.basename(path)
            if key in seen and not path.endswith(".jsonl"):
                continue
            seen.add(key)
            files += 1
            merge(total, scan_file(path, _load_concat(path)))

    print("扫了 %d 个数据文件，我们同桌过的对手共 %d 人\n" % (files, len(total)))

    rows = sorted(total.items(), key=lambda kv: -kv[1][1])
    if a.player:
        rows = [(uid, c) for uid, c in rows if a.player in (c[0] or "")]
        if not rows:
            print("❌ 在这些数据源里，我们和「%s」**没有任何同桌记录**。" % a.player)
            print("   数据源：%s" % "  ".join(a.sources))
            print("\n   同桌最多的 10 个对手（可以改盯这些人）：")
            for uid, c in sorted(total.items(), key=lambda kv: -kv[1][1])[:10]:
                print("     %-22s %5d 局" % (c[0][:20], c[1]))
            if a.logs:
                hit = 0
                for path in sorted(glob.glob("logs/*.jsonl")):
                    with io.open(path, encoding="utf-8", errors="replace") as fh:
                        for line in fh:
                            if a.player in line:
                                hit += 1
                                if hit == 1:
                                    print("\n   ⚠️ 但在 %s 的决策日志里出现过；"
                                          "说明打过、只是对局文件没采集到。" % path)
                                break
                if not hit:
                    print("\n   决策日志里也没有——我们确实从未和他同桌。")
            else:
                print("\n   加 --logs 可以再查一遍决策日志（对局文件可能漏采）。")
            return

    print("%-22s %6s %8s %9s %8s %9s %8s   %s"
          % ("对手", "同桌局", "我们胜率", "我们分/局", "他胜率", "他分/局", "他番/胡", "房间数"))
    print("-" * 108)
    for uid, c in rows[:a.top]:
        name, n, we_win, we_pts, he_win, he_pts, he_fan, rooms = c
        print("%-22s %6d %7.1f%% %+9.3f %7.1f%% %+9.3f %8.2f   %d"
              % (name[:20], n, 100.0 * we_win / n, we_pts / n,
                 100.0 * he_win / n, he_pts / n, he_fan / max(1, he_win), len(rooms)))
    if a.player and rows:
        print("\n房间号（可直接喂给 highfan_digest / session_health）：")
        for uid, c in rows:
            print("  %s: %s" % (c[0], " ".join(sorted(c[7]))))
    print("\n注：同一局里三个对手各记一次，所以「同桌局」之和 = 3 × 实际局数。"
          "胜率分母是同桌局，口径与 tools/session_health.py 一致。")


if __name__ == "__main__":
    main()
