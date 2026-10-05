"""爆头/财飘专项离线测试：枚举接近听牌的 13 张手牌，验证策略取舍。"""
import json
import os
from collections import Counter

from .rules import baotou, evaluate
from .strategy import choose_discard
from .tiles import ALL_TILES, TILE_INDEX, to_counts


def run_special_cases(output="models/special_cases.json"):
    cases = [
        {"hand": ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w", "东", "东", "东", "发"], "draw": "发"},
        {"hand": ["1w", "1w", "2w", "2w", "3w", "3w", "4w", "4w", "5w", "5w", "6w", "白", "白"], "draw": "白"},
        {"hand": ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w", "东", "东", "南", "白"], "draw": "白"},
    ]
    stats = Counter()
    rows = []
    for case in cases:
        hand = case["hand"]
        draw = case["draw"]
        counts = to_counts(hand)
        is_bao = baotou(counts)
        result = evaluate(counts, TILE_INDEX[draw], chain_count=1, piao=1)
        action = choose_discard(hand)
        stats["baotou"] += is_bao
        stats["hu"] += bool(result)
        stats["discard_joker"] += action == "白"
        rows.append({"hand_before_draw": hand, "draw": draw, "hand_size": len(hand), "baotou": is_bao, "result": result, "discard": action})
    report = {"cases": len(cases), "stats": dict(stats), "rows": rows}
    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
    with open(output, "w", encoding="utf-8") as target:
        json.dump(report, target, ensure_ascii=False, indent=2)
    return report
