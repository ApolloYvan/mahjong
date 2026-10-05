"""fan-calc 对拍：本地判胡引擎 vs 服务端同口径工具。"""
import argparse
import json
import os
import random
import ssl
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mj.rules import evaluate
from mj.tiles import ALL_TILES, TILE_INDEX, to_counts

SERVER = "https://10.240.169.190:18080"
CONTEXT = ssl._create_unverified_context()
JOKER = "白"
SUIT_TILES = [t for t in ALL_TILES if t != JOKER]


def server_calc(hand, draw, count, piao, base=1):
    body = json.dumps({
        "hand": hand,
        "draw": draw,
        "chain": {"count": count, "piao": piao},
        "base": base,
    }).encode()
    request = urllib.request.Request(SERVER + "/portal/api/tools/fan-calc", data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=10, context=CONTEXT) as response:
        return json.loads(response.read().decode("utf-8"))


def random_full_counts(rng):
    """随机 14 张（含将/面子倾向的纯随机）。"""
    counts = [0] * 34
    tiles = []
    pool = SUIT_TILES * 4 + [JOKER] * 4
    while len(tiles) < 14:
        tile = rng.choice(pool)
        if tiles.count(tile) < 4:
            tiles.append(tile)
    return tiles


def near_win_hand(rng, jokers=0):
    """构造 13 张差一张成牌的手牌，返回 (hand, draw)。"""
    for _ in range(200):
        counts = [0] * 34
        groups = []
        # 4 组面子/刻子
        for _ in range(4):
            kind = rng.random()
            if kind < 0.5:
                suit = rng.choice("wbt")
                rank = rng.randint(1, 7)
                tiles = [f"{rank}{suit}", f"{rank + 1}{suit}", f"{rank + 2}{suit}"]
            else:
                tile = rng.choice(SUIT_TILES)
                tiles = [tile] * 3
            groups.append(tiles)
        pair_tile = rng.choice(SUIT_TILES)
        tiles = [t for g in groups for t in g] + [pair_tile] * 2
        if jokers:
            drop = rng.sample([i for i, t in enumerate(tiles) if t != JOKER], min(jokers, len(tiles)))
            for i in sorted(drop, reverse=True):
                tiles[i] = JOKER
        if any(tiles.count(t) > 4 for t in set(tiles)):
            continue
        draw = rng.choice(tiles)
        hand = list(tiles)
        hand.remove(draw)
        if len(hand) == 13:
            return hand, draw
    return None


def seven_pairs_hand(rng, quads=0):
    tiles = []
    pool = SUIT_TILES[:]
    rng.shuffle(pool)
    for tile in pool:
        need = 4 if quads else 2
        take = min(need, 4)
        tiles += [tile] * take
        if len(tiles) >= 14:
            break
    tiles = tiles[:14]
    if len(tiles) < 14:
        return None
    draw = rng.choice(tiles)
    hand = list(tiles)
    hand.remove(draw)
    return hand, draw


def sample_cases(rng, total):
    cases = []
    while len(cases) < total:
        kind = rng.random()
        if kind < 0.35:
            made = near_win_hand(rng, jokers=rng.choice([0, 0, 1, 2]))
            if made:
                cases.append((*made, "near-win"))
        elif kind < 0.5:
            made = seven_pairs_hand(rng, quads=rng.choice([0, 0, 1]))
            if made:
                cases.append((*made, "seven-pairs"))
        else:
            tiles = random_full_counts(rng)
            draw = rng.choice(tiles)
            hand = list(tiles)
            hand.remove(draw)
            cases.append((hand, draw, "random"))
    return cases[:total]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=200)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--output", default="models/fan_duipai_mismatch.json")
    args = parser.parse_args()

    rng = random.Random(args.seed)
    cases = sample_cases(rng, args.samples)
    mismatch = []
    stats = {"checked": 0, "both_hu": 0, "both_no": 0, "server_hu_only": 0, "local_hu_only": 0, "fan_mismatch": 0, "baotou_mismatch": 0}

    for hand, draw, kind in cases:
        white_in_hand = hand.count(JOKER)
        count = rng.choice([0, 0, 0, 1, 1, 2, 3])
        piao = rng.choice([0, 0, count, count])
        piao = min(piao, count, max(0, 4 - white_in_hand - (1 if draw == JOKER else 0)))
        try:
            server = server_calc(hand, draw, count, piao)
        except Exception as error:
            print("服务端调用失败:", repr(error))
            time.sleep(1)
            continue
        time.sleep(0.12)
        local = evaluate(to_counts(hand), TILE_INDEX[draw], chain_count=count, piao=piao)
        stats["checked"] += 1
        s_hu = bool(server.get("hu"))
        l_hu = bool(local)
        if s_hu and l_hu:
            stats["both_hu"] += 1
            if int(server.get("fan", 0)) != int(local.get("fan", 0)):
                stats["fan_mismatch"] += 1
                mismatch.append({"kind": kind, "hand": hand, "draw": draw, "chain": [count, piao],
                                 "server": server, "local": local})
            if bool(server.get("baotou")) != bool(local.get("baotou")):
                stats["baotou_mismatch"] += 1
                mismatch.append({"kind": kind + "+baotou", "hand": hand, "draw": draw, "chain": [count, piao],
                                 "server": server, "local": local})
        elif s_hu:
            stats["server_hu_only"] += 1
            mismatch.append({"kind": kind + "+server-hu-only", "hand": hand, "draw": draw, "chain": [count, piao],
                             "server": server, "local": None})
        elif l_hu:
            stats["local_hu_only"] += 1
            mismatch.append({"kind": kind + "+local-hu-only", "hand": hand, "draw": draw, "chain": [count, piao],
                             "server": server, "local": local})
        else:
            stats["both_no"] += 1

    print(json.dumps(stats, ensure_ascii=False))
    if mismatch:
        os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as target:
            json.dump(mismatch, target, ensure_ascii=False, indent=2)
        print(f"不一致 {len(mismatch)} 例，已写入 {args.output}")
        for item in mismatch[:5]:
            print(json.dumps(item, ensure_ascii=False)[:400])
    else:
        print("完全一致 ✓")


if __name__ == "__main__":
    main()
