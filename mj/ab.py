"""离线 A/B 指标：向听、进张、财神保留与七对潜力。"""
import json
import os
import random
from collections import Counter

from .ev import choose_route_discard, hand_profile
from .strategy import choose_discard
from .tiles import ALL_TILES, JOKER


def random_hand(rng, size=14):
    wall = [tile for tile in ALL_TILES for _ in range(4)]
    rng.shuffle(wall)
    return wall[:size]


def _after(hand, discard):
    remaining = list(hand)
    remaining.remove(discard)
    return remaining


def run(seed=20260910, rounds=1000, output="models/ab_report.json"):
    rng = random.Random(seed)
    stats = Counter()
    rows = []
    for _ in range(rounds):
        hand = random_hand(rng)
        baseline = choose_discard(hand)
        route = choose_route_discard(hand)
        base_profile = hand_profile(_after(hand, baseline))
        route_profile = hand_profile(_after(hand, route))
        stats["different"] += baseline != route
        stats["baseline_shanten_better"] += base_profile["shanten"] < route_profile["shanten"]
        stats["route_shanten_better"] += route_profile["shanten"] < base_profile["shanten"]
        stats["baseline_ukeire_better"] += base_profile["ukeire"] > route_profile["ukeire"]
        stats["route_ukeire_better"] += route_profile["ukeire"] > base_profile["ukeire"]
        stats["baseline_keeps_joker"] += baseline != JOKER or hand.count(JOKER) > 1
        stats["route_keeps_joker"] += route != JOKER or hand.count(JOKER) > 1
        rows.append({"base_shanten": base_profile["shanten"], "route_shanten": route_profile["shanten"], "base_ukeire": base_profile["ukeire"], "route_ukeire": route_profile["ukeire"]})
    report = {"seed": seed, "rounds": rounds, "stats": dict(stats), "rows": rows[:100]}
    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
    with open(output, "w", encoding="utf-8") as target:
        json.dump(report, target, ensure_ascii=False, indent=2)
    return report
