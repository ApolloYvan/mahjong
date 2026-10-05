"""「吃了向听也不变好」的吃：吃 vs 不吃，本局结果差多少（高手 / 我们以前 / 我们当前）。

    python3 tools/neutral_chi_study.py

背景：tools/dealer_study.py 显示这类吃高手吃 ~27%，我们以前 ~45%，现在（rule_reject_neutral_chi_enabled）~9%。
每家每局只取第一次遇到的这类吃窗口（之后的局面已被这次选择改变），按
  起手财神 0/1+ × 吃前向听 1/2/3+ × 已副露 0/1/2 × 庄闲
分层，层内比较「吃了」和「没吃」两组本局的胜率、平均得分，再按层的样本量加权汇总。
这是观察数据：同一层里仍可能有"好牌才吃"的偏差，只作为是否值得放宽的证据之一。
"""
import argparse
import json
import os
import sys
from collections import defaultdict
from multiprocessing import Pool

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from mining_common import discover_files, merge_rounds  # noqa: E402
from mining_common import seat_names  # noqa: E402
from baotou_funnel import MASTERS, OUR, recent_rooms  # noqa: E402
from dealer_study import RESPONSE_TYPES, _after_claim, _chi_takes, _sh  # noqa: E402

JOKER = "白"
_STATE = {}


def _init(recent, exclude):
    _STATE["recent"], _STATE["exclude"] = set(recent), set(exclude)


def scan(path):
    out = []
    try:
        with open(path, encoding="utf-8") as source:
            game = json.load(source)
    except (OSError, ValueError):
        return out
    names = seat_names(game)
    room = game.get("room_id") or ""
    if len(names) != 4 or room in _STATE["exclude"]:
        return out
    groups = {}
    for s, n in enumerate(names):
        if n in MASTERS:
            groups[s] = "高手"
        elif n == OUR:
            groups[s] = "我们(当前)" if room in _STATE["recent"] else "我们(以前)"
    if not groups:
        return out
    info = {r.get("round_no"): r for r in game.get("rounds") or []}
    for rnd in merge_rounds(game):
        meta = info.get(rnd["round_no"])
        if not meta or rnd.get("truncated") or not rnd.get("start_hands"):
            continue
        hands = [list(h) for h in rnd["start_hands"]]
        if not all(hands):
            continue
        dealer = meta.get("dealer", rnd.get("dealer"))
        start_j = {s: min(hands[s].count(JOKER), 1) for s in groups}
        melds, chis = [0] * 4, [0] * 4
        first = {}
        events = rnd["events"]
        try:
            for idx, ev in enumerate(events):
                kind, seat, tile = ev["type"], ev.get("seat"), ev.get("tile")
                data = ev.get("data") or {}
                if kind == "tile_drawn":
                    hands[seat].append(tile)
                elif kind == "tile_discarded":
                    hands[seat].remove(tile)
                    r = (seat + 1) % 4
                    if (tile == JOKER or data.get("catch_play") or r not in groups or r in first
                            or chis[r] >= 2):
                        continue
                    takes = _chi_takes(hands[r], tile)
                    if not takes:
                        continue
                    window = []
                    for ev2 in events[idx + 1:idx + 13]:
                        if ev2["type"] not in RESPONSE_TYPES:
                            break
                        window.append(ev2)
                    if any(e["type"] in ("peng", "gang") for e in window):
                        continue
                    acts = [e["type"] for e in window if e.get("seat") == r]
                    took = "chi" in acts
                    if not (took or (len(acts) >= 2 and acts[-1] == "pass")):
                        continue
                    before = _sh(hands[r], melds[r])
                    if min(_after_claim(hands[r], tk, melds[r]) for tk in takes) < before:
                        continue            # 向听变好的吃不在本研究范围
                    first[r] = (start_j[r], min(before, 3), min(melds[r], 2), "庄" if r == dealer else "闲", took)
                elif kind in ("chi", "peng", "gang"):
                    if kind == "chi":
                        used = list(data.get("tiles") or [])
                        used.remove(tile)
                        for x in used:
                            hands[seat].remove(x)
                        chis[seat] += 1
                    elif kind == "peng":
                        for _ in range(2):
                            hands[seat].remove(tile)
                    else:
                        gk = data.get("kind")
                        for _ in range({"an": 4, "ming": 3, "bu": 1}[gk]):
                            hands[seat].remove(tile)
                        if gk == "bu":
                            continue
                    melds[seat] += 1
        except (ValueError, KeyError, IndexError, TypeError):
            continue
        winner = None if meta.get("is_draw") else meta.get("winner")
        scores = meta.get("scores") or [0] * 4
        for s, (j, sh, m, role, took) in first.items():
            out.append((groups[s], (j, sh, m, role), took, winner == s, scores[s]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-09-27T16:19")
    ap.add_argument("--exclude", nargs="*", default=["a_81df00ba3d44"], help="不计入的房间（bot 不在线等）")
    ap.add_argument("--jobs", type=int, default=6)
    args = ap.parse_args()
    recent = recent_rooms(args.since)
    files = discover_files()
    rows = []
    with Pool(args.jobs, initializer=_init, initargs=(recent, args.exclude)) as pool:
        for done, part in enumerate(pool.imap_unordered(scan, files, chunksize=8), 1):
            rows += part
            if done % 500 == 0 or done == len(files):
                print("  已扫 %d / %d" % (done, len(files)), flush=True)

    cell = defaultdict(lambda: [[0, 0, 0.0], [0, 0, 0.0]])     # (g, stratum) -> [没吃, 吃] 各 [n, 胡, 分]
    for g, stratum, took, won, score in rows:
        c = cell[(g, stratum)][int(took)]
        c[0] += 1
        c[1] += won
        c[2] += score

    print("\n=== 「吃了向听不变好」的吃：吃 vs 不吃 ===")
    print("%-10s %8s %7s | %-24s | %-24s | %s" % ("组", "窗口数", "吃比例", "吃了：次数 胜率 平均分", "没吃：次数 胜率 平均分",
                                               "分层加权 吃−不吃（分/局）"))
    for g in ("高手", "我们(以前)", "我们(当前)"):
        n_all = t_all = 0
        agg = [[0, 0, 0.0], [0, 0, 0.0]]
        wsum = dsum = 0.0
        for (gg, stratum), (no, yes) in cell.items():
            if gg != g:
                continue
            n_all += no[0] + yes[0]
            t_all += yes[0]
            for k in (0, 1):
                for i in range(3):
                    agg[k][i] += (no, yes)[k][i]
            if no[0] >= 5 and yes[0] >= 5:
                w = no[0] + yes[0]
                wsum += w
                dsum += w * (yes[2] / yes[0] - no[2] / no[0])

        def fmt(c):
            return "%5d %5.1f%% %+7.2f" % (c[0], 100 * c[1] / c[0], c[2] / c[0]) if c[0] else "%24s" % "-"
        if n_all:
            print("%-10s %8d %6.1f%% | %-24s | %-24s | %s" % (
                g, n_all, 100 * t_all / n_all, fmt(agg[1]), fmt(agg[0]),
                "%+.2f（覆盖 %d 个窗口）" % (dsum / wsum, wsum) if wsum else "-"))
    print("\n读法：高手/我们以前 这两组样本大；若它们的「分层加权 吃−不吃」都明显为正，说明这类吃平均是赚的，")
    print("  我们现在几乎全不吃就是在丢分，应放宽 rule_reject_neutral_chi；若接近 0 或为负，维持现状。")


if __name__ == "__main__":
    main()
