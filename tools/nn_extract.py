"""弃牌小模型的训练数据：高手每一次弃牌 → 同向听层里每个候选一行特征（mj/nn_discard.vector）。

    python3 tools/nn_extract.py                       # 写 data/analysis/nn_discard.jsonl
    python3 tools/nn_extract.py --jobs 8

范围与线上一致：不在财飘链上（本局没打过财神）、不是抓打圈、弃牌前手牌是 14 张口径；
高手的弃牌不在最低向听层（主动拆听等，约 1%）跳过；只有一个候选的跳过。
测试集（对局文件 md5 % 5 == 0，与 tools/discard_fit.py 同一切分）额外记录现行策略的选择 cur，
训练脚本用它报告「现行 vs 模型」在同一批高手决策上的一致率。
"""
import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from multiprocessing import Pool

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from mining_common import discover_files, merge_rounds  # noqa: E402
from mining_common import seat_names  # noqa: E402
from baotou_funnel import MASTERS  # noqa: E402
from mj.ev import choose_route_discard  # noqa: E402
from mj.fit import load_weights  # noqa: E402
from mj.nn_discard import FEATURES, vector  # noqa: E402
from mj.shanten import route_shanten  # noqa: E402
from mj.strategy import choose_discard  # noqa: E402
from mj.tiles import TILE_INDEX, to_counts  # noqa: E402

JOKER = "白"
OUT = "data/analysis/nn_discard.jsonl"
CUR_RULES = {"_weights": {"rule_nn_discard_enabled": 0}}     # 现行策略 = 当前 weights.json，但不走小模型
_STATE = {}


def is_test(base):
    return int(hashlib.md5(base.encode()).hexdigest()[:8], 16) % 5 == 0


def _init():
    _STATE["weights"] = load_weights()


def _r(v):
    return int(v) if v == int(v) else round(v, 4)


def extract_file(path):
    weights = _STATE["weights"]
    stats = Counter()
    try:
        with open(path, encoding="utf-8") as source:
            game = json.load(source)
    except (OSError, ValueError):
        return [], stats
    names = seat_names(game)
    if len(names) != 4 or not any(n in MASTERS for n in names):
        return [], stats
    base = os.path.basename(path)
    test = is_test(base)
    info = {r.get("round_no"): r for r in game.get("rounds") or []}
    rows = []
    for rnd in merge_rounds(game):
        meta = info.get(rnd["round_no"])
        if not meta or rnd.get("truncated") or not rnd.get("start_hands"):
            continue
        dealer = meta.get("dealer", rnd.get("dealer"))
        hands = [list(h) for h in rnd["start_hands"]]
        wall = 136 - sum(len(h) for h in hands)
        melds = [0] * 4
        seen = [0] * 34
        piaoed = [False] * 4
        try:
            for ev in rnd["events"]:
                kind, seat, tile = ev["type"], ev.get("seat"), ev.get("tile")
                data = ev.get("data") or {}
                if kind == "tile_drawn":
                    hands[seat].append(tile)
                    wall -= 1
                elif kind == "tile_discarded":
                    hand = hands[seat]
                    if (names[seat] in MASTERS and not piaoed[seat] and not data.get("catch_play")
                            and len(hand) + 3 * melds[seat] == 14):
                        stats["points"] += 1
                        row = _point(hand, tile, melds[seat], seen, wall, seat == dealer,
                                     [melds[s] for s in range(4) if s != seat], weights, test, stats)
                        if row:
                            row.update(f=base, p=names[seat], t=int(test))
                            rows.append(row)
                    hand.remove(tile)
                    seen[TILE_INDEX[tile]] += 1
                    if tile == JOKER:
                        piaoed[seat] = True
                elif kind == "chi":
                    used = list(data.get("tiles") or [])
                    used.remove(tile)
                    for t in used:
                        hands[seat].remove(t)
                        seen[TILE_INDEX[t]] += 1
                    melds[seat] += 1
                elif kind == "peng":
                    for _ in range(2):
                        hands[seat].remove(tile)
                    seen[TILE_INDEX[tile]] += 2
                    melds[seat] += 1
                elif kind == "gang":
                    take = {"an": 4, "ming": 3, "bu": 1}[data.get("kind")]
                    for _ in range(take):
                        hands[seat].remove(tile)
                    seen[TILE_INDEX[tile]] += take
                    if data.get("kind") != "bu":
                        melds[seat] += 1
        except (ValueError, KeyError, IndexError, TypeError):
            stats["bad_round"] += 1
            continue
    return rows, stats


def _point(hand, actual, groups, seen, wall, is_dealer, opp_melds, weights, test, stats):
    unique = sorted(set(hand))
    cur_sh = {}
    for t in unique:
        rest = list(hand)
        rest.remove(t)
        cur_sh[t] = route_shanten(tuple(to_counts(rest)), groups)
    best = min(cur_sh.values())
    tier = [t for t in unique if cur_sh[t] == best]
    if actual not in tier:
        stats["outtier"] += 1
        return None
    if len(tier) == 1:
        stats["single"] += 1
        return None
    own = to_counts(hand)
    visible = [min(4, seen[i] + own[i]) for i in range(34)]
    ctx = {"wall": wall, "dealer": is_dealer, "opp_melds": opp_melds}
    rules = {"_ctx": ctx, "dealer_hint": True} if is_dealer else {"_ctx": ctx}
    X = [[_r(v) for v in vector(hand, t, groups, visible, weights, ctx, rules)] for t in tier]
    cur = -1
    if test:
        pick = (choose_route_discard(hand, groups, 0, 0, dict(CUR_RULES, dealer_hint=True), visible) if is_dealer
                else choose_discard(hand, groups, 0, 0, CUR_RULES, visible))
        cur = tier.index(pick) if pick in tier else -1
    return {"X": X, "y": tier.index(actual), "cur": cur, "j": int(JOKER in hand), "d": int(is_dealer)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()
    files = discover_files()
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    total, n = Counter(), 0
    with open(args.out, "w", encoding="utf-8") as sink, Pool(args.jobs, initializer=_init) as pool:
        for done, (rows, stats) in enumerate(pool.imap_unordered(extract_file, files, chunksize=4), 1):
            total.update(stats)
            for row in rows:
                sink.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
                n += 1
            if done % 100 == 0 or done == len(files):
                print("  进度 %d / %d 个文件，已写 %d 个决策点" % (done, len(files), n), flush=True)
    print("\n已写出 %s：%d 个决策点，每个候选 %d 维特征" % (args.out, n, len(FEATURES)))
    print("高手弃牌点 %d；不在最低向听层跳过 %d；只有一个候选跳过 %d；坏局 %d" % (
        total["points"], total["outtier"], total["single"], total["bad_round"]))


if __name__ == "__main__":
    main()
