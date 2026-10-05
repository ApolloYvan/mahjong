"""完整牌墙自摸模拟：含财神、七对、杠与杠开。"""
import json
import os
import random
from collections import Counter

from .melds import apply_gang, apply_peng, hand_shape_valid
from .ev import choose_route_discard
from .rules import evaluate, seven_pairs, win_standard
from .strategy import choose_discard
from .tiles import ALL_TILES, TILE_INDEX, to_counts



def wall(rng):
    tiles = [tile for tile in ALL_TILES for _ in range(4)]
    rng.shuffle(tiles)
    return tiles


def _evaluate_hand(hand, chain_count=0, piao=0, gang_open=False, melds=None):
    melds = melds or []
    counts = to_counts(hand)
    groups = 4 - len(melds)
    result = evaluate(to_counts(hand[:-1]), TILE_INDEX[hand[-1]], chain_count=chain_count, piao=piao, meld_groups=len(melds))
    if result is not None:
        if gang_open:
            result["detail"].append("杠开")
        return result
    if win_standard(counts, meld_groups=len(melds)):
        return {"hu": True, "fan": 2 ** chain_count, "detail": ["杠开"] if gang_open else ["平胡"]}
    pairs = seven_pairs(counts) if not melds else None
    if pairs is not None:
        return {"hu": True, "fan": {1: 2, 2: 4, 3: 8}.get(pairs, 16), "quads": pairs, "detail": ["七对子"]}
    return None


def _remove_gang(hand, tile):
    left = list(hand)
    if left.count(tile) < 4:
        return None
    for _ in range(4):
        left.remove(tile)
    return left


def play(seed, strategy, max_draws=80):
    rng = random.Random(seed)
    tiles = wall(rng)
    hand = tiles[:14]
    rest = tiles[14:]
    melds = []
    chain = 0
    piao = 0
    gang_open = False
    for draws in range(max_draws):
        result = _evaluate_hand(hand, chain, piao, gang_open, melds)
        if result is not None:
            return {"win": True, "draws": draws, "fan": result["fan"],
                    "detail": result.get("detail", [])}
        if len(rest) <= 20:
            gang_allowed = False
        else:
            gang_allowed = True
        gang = None
        if gang_allowed:
            candidate = hand[-1] if hand.count(hand[-1]) == 4 else None
            if candidate and apply_gang(hand, melds, candidate):
                gang = {"tile": candidate}
        if gang:
            if not rest:
                break
            hand.append(rest.pop())
            chain += 1
            gang_open = True
            continue
        discard = strategy(hand)
        hand.remove(discard)
        if not rest:
            break
        hand.append(rest.pop())
        gang_open = False
    return {"win": False, "draws": max_draws, "fan": 0}


def run_games(rounds=100, seed=20260910, output="models/game_simulation.json"):
    stats = Counter()
    detail_counts = Counter()
    for i in range(rounds):
        for name, strategy in (("baseline", choose_discard), ("route", choose_route_discard)):
            result = play(seed + i, strategy)
            stats[f"{name}_wins"] += result["win"]
            stats[f"{name}_draw_total"] += result["draws"]
            stats[f"{name}_fan_total"] += result["fan"]
            for detail in result.get("detail", []):
                detail_counts[f"{name}:{detail}"] += 1
    report = {"rounds": rounds, "stats": dict(stats), "detail_counts": dict(detail_counts)}
    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
    with open(output, "w", encoding="utf-8") as target:
        json.dump(report, target, ensure_ascii=False, indent=2)
    return report
