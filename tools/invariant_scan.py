"""明显错误扫描：在实战对局里找我方违反基本原则的动作（找 bug，不是评估策略）。
历史上两次最大的提升（摸财神拒胡 243:0、S1）都来自这类"同一局面下明显不对"的扫描。

检查项（只看我方、非服务端代打的决策）：
  A 能胡但打出了一张牌，打完不听牌（S1/飘打完都应仍听牌）
  B 主动拆听：有能保持听牌的打法，却打成不听牌
  C 吃/碰后（打出最好的一张）向听反而变差
  D 非爆头听牌状态下打出财神（不是飘）
  E 墙剩 ≤20 张时还杠
每类给出次数、每局次数、前几个样例（局面 + 当时动作）。

    python3 tools/invariant_scan.py --since 2026-10-06T05:00            # 这之后的房间
"""
import argparse
import glob
import json
import os
import sys
from collections import Counter
from multiprocessing import Pool

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "tools", "train"))
os.environ.setdefault("MJ_WEIGHTS_NO_FILE", "1")

from baotou_funnel import room_first_seen  # noqa: E402
from build_dataset import iter_decisions  # noqa: E402
from mining_common import OUR_NAME  # noqa: E402
from mj import bot  # noqa: E402
from mj.rules import baotou  # noqa: E402
from mj.shanten import route_shanten  # noqa: E402
from mj.tiles import JOKER, to_counts  # noqa: E402


def _sh(tiles, groups):
    return route_shanten(tuple(to_counts(tiles)), groups)


def _after(hand, tile):
    rest = list(hand)
    rest.remove(tile)
    return rest


def scan(path):
    out = []
    n_draw = 0
    for rec in iter_decisions(path, keep=lambda nm, ph, ac: 1.0 if nm == OUR_NAME else 0.0, seed=0):
        snap, act = rec["snapshot"], rec["action"]
        me = snap["seat"]
        hand = snap["my_hand"]
        groups = len(snap["melds"][me])
        a = act.get("action")
        tile = act.get("tile")
        ex = {"game": os.path.basename(path), "round": rec["round_no"], "hand": " ".join(sorted(hand)),
              "drawn": snap.get("drawn_tile"), "melds": groups, "wall": snap.get("wall_remaining"), "act": "%s:%s" % (a, tile)}
        if rec["phase"] == "draw":
            n_draw += 1
            god = snap.get("god") or {}
            restricted = god.get("catch_play") and god.get("god_discarder_seat") != me
            if a == "gang" and (snap.get("wall_remaining") or 99) <= 20:
                out.append(("E 墙剩≤20 仍杠", ex))
            if a != "discard" or tile not in hand or restricted:
                continue
            rest = _after(hand, tile)
            sh_after = _sh(rest, groups)
            if bot.hu_result(snap) and sh_after > 0:
                out.append(("A 能胡却打成不听牌", ex))
                continue
            if sh_after > 0 and any(_sh(_after(hand, t), groups) <= 0 for t in set(hand)):
                out.append(("B 主动拆听", ex))
            if tile == JOKER and not baotou(tuple(to_counts(rest)), groups):
                out.append(("D 非爆头状态打出财神", ex))
        elif a in ("chi", "peng"):
            before = _sh(hand, groups)
            take = [act.get("tile")] if a == "chi" else [tile, tile]
            if a == "chi":
                take = list(act.get("tiles") or [])
            rest = list(hand)
            try:
                for t in take:
                    rest.remove(t)
            except ValueError:
                continue
            best = min((_sh(_after(rest, d), groups + 1) for d in set(rest)), default=9)
            if best > before:
                out.append(("C 吃碰后向听变差", ex))
    return n_draw, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True)
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--examples", type=int, default=5)
    args = ap.parse_args()
    first = room_first_seen()
    rooms = {r for r, t in first.items() if t >= args.since}
    files = [p for p in glob.glob("models/events/*.json") if os.path.basename(p).split("_r1")[0] in rooms]
    n_draw, rows = 0, []
    with Pool(args.jobs) as pool:
        for nd, out in pool.imap_unordered(scan, files, chunksize=2):
            n_draw += nd
            rows += out
    n_rounds = len(files) * 8
    print("=== invariant_scan：%s 之后 %d 房、%d 个对局文件、约 %d 局、我方摸牌决策 %d 次 ===" % (
        args.since, len(rooms), len(files), n_rounds, n_draw))
    c = Counter(k for k, _ in rows)
    for k in sorted(c):
        print("\n%s：%d 次（每局 %.3f）" % (k, c[k], c[k] / max(1, n_rounds)))
        for _, ex in [r for r in rows if r[0] == k][:args.examples]:
            print("    %s 第%s局 手牌[%s] 摸%s 露%s 墙%s → %s" % (ex["game"][:20], ex["round"], ex["hand"], ex["drawn"] or "-",
                                                         ex["melds"], ex["wall"], ex["act"]))
    if not c:
        print("没有发现任何违反项。")


if __name__ == "__main__":
    main()
