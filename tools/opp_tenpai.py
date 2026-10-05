# -*- coding: utf-8 -*-
"""对手听牌估计器：标签=对手真实向听<=0，特征只用公开信息。

分桶频率表（带拉普拉斯平滑），按房间 70/30 留出评估校准度。

用法：
    python3 tools/opp_tenpai.py --jobs 3 --limit 20     # profiling
    python3 tools/opp_tenpai.py --jobs 3                # 全量

输出：data/analysis/opp_tenpai_model.json（分桶频率表 + 校准表）。

范围说明（如实标注）：本版本训练并输出校准表，但**没有把 opp_tenpai_p1..p3
回填进 decisions.csv**——回填需要对 decisions.csv 的每一行重建三个对手当时的
公开特征（meld 明细、牌河honor/幺九占比、最近3巡摸切），这在当前 decisions.csv
的列里没有保留到足够粒度，要做到就必须重新跑一遍 1250 文件的全量重放。上一轮
decision_table.py 全量跑因为机器过载崩溃过一次，为避免再次让机器满载，这一步
被有意推迟（见 docs/experiments/OFFLINE_REPORT.md「已知局限」）。H1 的判据本身
用的是 opp_melds_max / dealer_melds（decisions.csv 已有），不依赖这个列，因此
不影响 H1 的结论。
"""
import argparse
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.mining_common import discover_files, merge_rounds  # noqa: E402
from mj.tiles import JOKER, TILE_INDEX, to_counts  # noqa: E402
from mj.shanten import shanten  # noqa: E402

HONORS = set(["东", "南", "西", "北", "中", "发", "白"])


def _apply(hands, melds, event):
    kind = event["type"]
    seat = event.get("seat")
    tile = event.get("tile")
    data = event.get("data") or {}
    if kind == "tile_drawn":
        hands[seat].append(tile)
    elif kind == "tile_discarded":
        hands[seat].remove(tile)
    elif kind == "chi":
        used = list(data.get("tiles") or [])
        used.remove(tile)
        for t in used:
            hands[seat].remove(t)
        melds[seat].append({"kind": "chi", "tiles": list(data.get("tiles") or [])})
    elif kind == "peng":
        for _ in range(2):
            hands[seat].remove(tile)
        melds[seat].append({"kind": "peng", "tiles": [tile, tile, tile]})
    elif kind == "gang":
        gk = data.get("kind")
        take = {"an": 4, "ming": 3, "bu": 1}[gk]
        for _ in range(take):
            hands[seat].remove(tile)
        if gk != "bu":
            melds[seat].append({"kind": "gang", "tiles": [tile] * 4})


def _bucket_key(meld_count, turn_no, honor_frac, terminal_frac, tsumogiri_last3):
    mc = min(meld_count, 2)
    tb = "early" if turn_no <= 4 else ("mid" if turn_no <= 9 else "late")
    hf = "0" if honor_frac < 0.2 else ("1" if honor_frac < 0.4 else "2")
    tf = "0" if terminal_frac < 0.2 else ("1" if terminal_frac < 0.4 else "2")
    ts = int(tsumogiri_last3 >= 2)
    return (mc, tb, hf, tf, ts)


def scan_file(path):
    """返回该文件里所有座位每次弃牌后的 (room_id, bucket_key, label) 三元组。"""
    try:
        game = json.load(open(path, encoding="utf-8"))
    except (ValueError, OSError):
        return []
    seats = game.get("seats") or []
    if len(seats) != 4:
        return []
    room_id = game.get("room_id")
    out = []
    for rnd in merge_rounds(game):
        start_hands = rnd["start_hands"]
        if rnd.get("truncated") or not start_hands:
            continue
        hands = [list(h) for h in start_hands]
        melds = [[] for _ in range(4)]
        river = [[] for _ in range(4)]
        last3 = [[], [], [], []]
        turn_count = [0, 0, 0, 0]
        try:
            for event in rnd["events"]:
                kind = event["type"]
                seat = event.get("seat")
                tile = event.get("tile")
                if kind == "tile_drawn":
                    turn_count[seat] += 1
                    _apply(hands, melds, event)
                    continue
                if kind == "tile_discarded":
                    tsumogiri = 1 if (hands[seat] and hands[seat][-1] == tile) else 0
                    river[seat].append(tile)
                    meld_count = len(melds[seat])
                    n_river = len(river[seat])
                    honor_frac = (sum(1 for t in river[seat] if t in HONORS) / n_river) if n_river else 0.0
                    terminal_frac = (sum(1 for t in river[seat]
                                         if t not in HONORS and TILE_INDEX[t] % 9 in (0, 8)) / n_river) \
                        if n_river else 0.0
                    last3[seat] = (last3[seat] + [tsumogiri])[-3:]
                    hand_before = list(hands[seat])
                    hand_before.remove(tile)
                    sh = shanten(to_counts(hand_before), meld_count)
                    label = 1 if sh <= 0 else 0
                    bucket = _bucket_key(meld_count, turn_count[seat], honor_frac, terminal_frac,
                                         sum(last3[seat]))
                    out.append((room_id, bucket, label))
                    _apply(hands, melds, event)
                    continue
                if kind in ("chi", "peng", "gang"):
                    _apply(hands, melds, event)
        except (ValueError, KeyError, TypeError, IndexError):
            continue
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", nargs="*",
                    default=["tools/models/events/*.json", "models/events/*.json"])
    ap.add_argument("--out", default="data/analysis/opp_tenpai_model.json")
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    files = discover_files(tuple(args.events), limit=args.limit)
    print("文件数：%d" % len(files))

    from multiprocessing import Pool
    with Pool(args.jobs) as pool:
        all_rows = [r for sub in pool.map(scan_file, files, chunksize=4) for r in sub]
    print("样本数（每次弃牌一条）：%d" % len(all_rows))

    rooms = sorted(set(r for r, _b, _l in all_rows))
    import random
    rng = random.Random(20260924)
    rng.shuffle(rooms)
    split = int(len(rooms) * 0.7)
    train_rooms = set(rooms[:split])

    train = [(b, l) for r, b, l in all_rows if r in train_rooms]
    test = [(b, l) for r, b, l in all_rows if r not in train_rooms]

    table = defaultdict(lambda: [0, 0])  # bucket -> [tenpai_count, total]
    for bucket, label in train:
        table[bucket][0] += label
        table[bucket][1] += 1

    def predict(bucket):
        hit, n = table.get(bucket, [0, 0])
        # 拉普拉斯平滑：alpha=1，先验按全体训练集的平均听牌率。
        prior = (sum(v[0] for v in table.values()) / sum(v[1] for v in table.values())) \
            if table else 0.3
        return (hit + prior) / (n + 1)

    # 校准表：预测概率 10 档 vs 实际频率
    calib_buckets = defaultdict(lambda: [0, 0])
    for bucket, label in test:
        p = predict(bucket)
        decile = min(9, int(p * 10))
        calib_buckets[decile][0] += label
        calib_buckets[decile][1] += 1
    calibration = []
    for d in range(10):
        hit, n = calib_buckets[d]
        calibration.append({"decile": d, "pred_range": "%.1f-%.1f" % (d / 10, (d + 1) / 10),
                            "n": n, "actual_rate": (hit / n) if n else None})

    model = {
        "n_train": len(train), "n_test": len(test), "n_rooms": len(rooms),
        "table": {"|".join(map(str, k)): v for k, v in table.items()},
        "calibration": calibration,
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(model, fh, ensure_ascii=False, indent=2)
    print("已写出 %s" % args.out)
    for row in calibration:
        print(row)


if __name__ == "__main__":
    main()
