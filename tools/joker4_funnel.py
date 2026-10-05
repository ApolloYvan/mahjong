# -*- coding: utf-8 -*-
"""爆头漏斗：「合计 = 已飘白板 + 手中白板」推到 4 才收手，这条路线的真实成功率。

背景（2026-09-24）：14 局 32 番回放里，赢家在最后收手那一摸全部满足
`链次数 + 手中白板 == 4`，而且**合计<4 时即使已经爆头也坚决不胡**——最赤裸的
一局连续放弃四次爆头自摸，只为等第 4 张白板（32 番 = 2^3 三财飘 × 2 四白板 ×
2 爆头）。我们的 `choose_hu_or_piao` 只能返回 hu 或 discard 白，**没有"我能胡
但我不胡"这个分支**，所以永远拿不到最后那两个 ×2。

但那 14 局全是**赢家**回放：所有"弃胡等白板、结果被人抢先或流局"的手根本不在
样本里，弃胡的真实成本不可见。本工具在全量语料上把漏斗补全——对每一个座位、
每一局，找到它**第一次爆头**的时刻，记下当时的合计，再看它最终有没有胡、几番。

    python3 tools/joker4_funnel.py
    python3 tools/joker4_funnel.py --jobs 8
"""
import argparse
import glob
import json
import os
import sys
from collections import defaultdict
from multiprocessing import Pool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mj.rules import baotou, seven_pairs
from mj.tiles import to_counts

OUR_UID = "u_fd06550b5fb3"
JOKER = "白"


def merge_rounds(game):
    by = {}
    for block in game["blocks"]:
        rno = block["round_no"]
        item = by.setdefault(rno, dict(round_no=rno, start_hands=None,
                                       dealer=block.get("dealer"), events=[]))
        hands = block.get("start_hands")
        if hands and all(hands):
            item["start_hands"] = hands
        item["events"].extend(block["events"])
    out = []
    for rno in sorted(by):
        by[rno]["events"].sort(key=lambda e: e["seq"])
        out.append(by[rno])
    return out


def _est_fan(hand, groups, flown, total):
    """这一刻立刻自摸能拿几番（按真实番数公式估算，用来和"继续等"作比较）。

    1 × 分支因子 × 2^链次数 × (4白板×2) × (爆头×2)。分支因子只算门清七对；
    豪华七对等更高分支这里保守按 2 计，宁可低估"立刻胡"的价值。
    """
    counts = to_counts(hand)
    branch = 2 if (groups == 0 and seven_pairs(counts) is not None) else 1
    return branch * (2 ** min(flown, 3)) * (2 if total >= 4 else 1) * 2


def scan(path):
    try:
        game = json.load(open(path, encoding="utf-8"))
    except (ValueError, OSError):
        return []
    uids = [s["user_id"] for s in game["seats"]]
    results = {r["round_no"]: r for r in game["rounds"]}
    out = []
    for rnd in merge_rounds(game):
        info = results.get(rnd["round_no"])
        if not info or not rnd["start_hands"]:
            continue
        winner = None if info.get("is_draw") else info.get("winner")
        dealer = rnd.get("dealer")
        fan = 0
        if winner is not None:
            got = (info.get("scores") or [0] * 4)[winner]
            per = 24 if dealer == winner else 10
            fan = max(1, int(round(got / float(per)))) if got > 0 else 0

        hands = [list(h) for h in rnd["start_hands"]]
        melds, flown = [0] * 4, [0] * 4
        # 每座位一条记录：第一次爆头时的合计 / 曾达到的最高合计 / 首次爆头时可拿几番
        first = [None] * 4
        best = [0] * 4
        ever_bt = [False] * 4
        try:
            for event in rnd["events"]:
                kind, seat, tile = event["type"], event.get("seat"), event.get("tile")
                data = event.get("data") or {}
                if kind == "tile_drawn":
                    hands[seat].append(tile)
                elif kind == "tile_discarded":
                    hands[seat].remove(tile)
                    if tile == JOKER:
                        flown[seat] += 1
                elif kind == "chi":
                    used = list(data.get("tiles") or [])
                    used.remove(tile)
                    for t in used:
                        hands[seat].remove(t)
                    melds[seat] += 1
                elif kind == "peng":
                    for _ in range(2):
                        hands[seat].remove(tile)
                    melds[seat] += 1
                elif kind == "gang":
                    for _ in range({"an": 4, "ming": 3, "bu": 1}[data.get("kind")]):
                        hands[seat].remove(tile)
                    if data.get("kind") != "bu":
                        melds[seat] += 1
                else:
                    continue
                if kind != "tile_discarded" or seat is None:
                    continue
                # 弃牌后是 13 张的稳定态，只在这里判爆头，避免 14 张时误判
                hand = hands[seat]
                if len(hand) + 3 * melds[seat] != 13:
                    continue
                total = hand.count(JOKER) + flown[seat]
                if total > best[seat]:
                    best[seat] = total
                if not baotou(to_counts(hand), melds[seat]):
                    continue
                ever_bt[seat] = True
                if first[seat] is None:
                    first[seat] = (total, _est_fan(hand, melds[seat], flown[seat], total))
        except (ValueError, KeyError, TypeError):
            continue
        for seat in range(4):
            if not ever_bt[seat]:
                continue
            ft, est = first[seat]
            out.append((uids[seat], ft, est, best[seat],
                        1 if seat == winner else 0,
                        fan if seat == winner else 0))
    return out


def _table(title, rows, out):
    """rows: 合计 -> [手数, 胡牌数, 番合计, 立刻胡估算番合计, 凑到4的手数,
                      凑到4后的胡牌数, 凑到4后的番合计]"""
    out.append("\n=== %s ===" % title)
    out.append("%-6s %6s %8s %8s %9s   %10s %10s %9s"
               % ("首爆头合计", "手数", "最终胡", "胡牌率", "平均番",
                  "后来凑到4", "凑到4胡牌率", "凑到4平均番"))
    out.append("-" * 92)
    for key in sorted(rows):
        n, hu, fan, est, r4, r4hu, r4fan = rows[key]
        out.append("%-8s %6d %7d %8.1f%% %8.2f   %7d(%4.1f%%) %9.1f%% %8.2f"
                   % (key, n, hu, 100.0 * hu / n, fan / max(1, hu),
                      r4, 100.0 * r4 / n,
                      100.0 * r4hu / max(1, r4), r4fan / max(1, r4hu)))
        out.append("%-8s %6s %7s %8s %8s   立刻胡的估算番 %.2f"
                   % ("", "", "", "", "", est / float(n)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", nargs="*",
                        default=["models/events/*.json", "tools/models/events/*.json"])
    parser.add_argument("--jobs", type=int, default=6)
    args = parser.parse_args()

    files, seen = [], set()
    for pattern in args.events:
        for path in sorted(glob.glob(pattern)):
            name = os.path.basename(path)
            if name not in seen:
                seen.add(name)
                files.append(path)

    with Pool(args.jobs) as pool:
        rows = [r for sub in pool.map(scan, files, chunksize=4) for r in sub]

    def bucket():
        return defaultdict(lambda: [0, 0, 0, 0, 0, 0, 0])
    ours, theirs = bucket(), bucket()
    for uid, ft, est, best, won, fan in rows:
        key = "4+" if ft >= 4 else str(ft)
        cell = ours[key] if uid == OUR_UID else theirs[key]
        cell[0] += 1
        cell[1] += won
        cell[2] += fan
        cell[3] += est
        if best >= 4:
            cell[4] += 1
            cell[5] += won
            cell[6] += fan

    out = ["扫了 %d 个对局文件，%d 个「曾经爆头」的座位×局样本" % (len(files), len(rows))]
    _table("对手：首次爆头时的合计 → 最终结果", theirs, out)
    _table("我们：首次爆头时的合计 → 最终结果", ours, out)
    out.append("""
读法（这是替"合计<4 时该不该弃胡"定价的唯一依据）：
  「合计3」那一行是决策点——差一张白板。
  立刻胡 = 拿到"立刻胡的估算番"，成功率 100%。
  继续等 = 以"后来凑到4"的比例赌一把，成功了拿"凑到4平均番"，
           但还要再乘"凑到4胡牌率"（凑到了也可能被人抢先）。
  只有 凑到4比例 × 凑到4胡牌率 × 凑到4平均番 > 立刻胡的估算番，弃胡才成立。
对手那张表是套路的真实收益，我们那张表是我们现在的实际产出。""")
    print("\n".join(out))


if __name__ == "__main__":
    main()
