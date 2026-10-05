"""临时诊断：验证判胡引擎能否识别基本胡牌形。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mj.rules import evaluate, baotou, seven_pairs
from mj.tiles import TILE_INDEX, to_counts
from mj.bot import can_hu, hu_result, _meld_count, _melds_for_seat

cases = [
    # (手牌, 摸牌, 副露组数, 描述)
    (["1w", "1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w", "9w", "9t", "9t"], "9t", 0, "无效形(应为None)"),
    (["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w", "1t", "1t", "9b", "9b"], "9b", 0, "平胡-三顺+将"),
    (["1w", "1w", "2w", "2w", "3w", "3w", "5t", "5t", "6t", "6t", "8b", "8b", "9b"], "9b", 0, "七对"),
    (["1w", "1w", "1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w", "9t", "9t"], "9t", 0, "刻子+顺子"),
    (["白", "白", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w", "9w", "9t", "9t"], "9t", 0, "含财神"),
    (["1w", "2w", "3w", "4w", "5w", "6w", "5t", "5t", "5t", "8b"], "8b", 1, "副露1组+顺+刻"),
    (["4w", "5w", "6w", "5t", "5t", "5t", "8b"], "8b", 2, "副露2组+顺+刻"),
]

for hand, draw, melds, name in cases:
    counts13 = to_counts(hand)
    result = evaluate(counts13, TILE_INDEX[draw], meld_groups=melds)
    meld_lists = [[] for _ in range(4)]
    meld_lists[0] = [{"kind": "peng", "tiles": ["1t", "1t", "1t"]} for _ in range(melds)]
    snapshot = {"my_hand": hand + [draw], "drawn_tile": draw, "seat": 0, "turn": 0,
                "phase": "draw", "wall_remaining": 40, "melds": meld_lists}
    ch = can_hu(snapshot, {})
    hr = hu_result(snapshot, {})
    ok = "WIN" if result else "None"
    print(f"{name}: melds={melds} evaluate={ok} can_hu={ch} hu_result={'fan=' + str(hr['fan']) if hr else None}")

