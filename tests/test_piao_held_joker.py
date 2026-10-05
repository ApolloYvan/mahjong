"""锁死：财飘可以打出「手里原有」的财神，但必须打掉之后仍然爆头。

2026-09-23 实测（全平台 295 次弃财神，其中「打之前手牌本来就能胡」的真财飘 62 次）：

    飘的是手里原有的财神   37 次（60%）   ← 主流用法
    飘的是刚摸到的财神     25 次（40%）
    我们 88 次弃财神里，手里原有的只有 2 次      ← 约 176 次机会被跳过

按「打掉后是否仍爆头」拆开看结局：

    A 打掉后仍爆头   n=46  最终胡牌 42 次 = 91.3%  胡时平均番 4.29
    B 打掉后不再爆头 n=16  最终胡牌  5 次 = 31.2%  胡时平均番 1.00

A 的期望番 0.913×4.29 = 3.92 对比立刻胡的 2——接近翻倍、风险 8.7%；
B 是拿确定的胡牌换窄听。所以只做 A。
"""
import unittest

from mj.hu_strategy import choose_hu_or_piao
from mj.tiles import JOKER

LOOSE = {"success_floor": 0.0, "fail_cost": 0.0}     # 只验触发条件，不验 EV 阈值


def _snapshot(hand, drawn, meld_groups=0, **kw):
    melds = [[{"kind": "peng"} for _ in range(meld_groups)], [], [], []]
    base = {"phase": "draw", "seat": 0, "my_hand": list(hand), "drawn_tile": drawn,
            "melds": melds, "wall_remaining": 60, "dealer": 1,
            "chain_count": 0, "piao": 0, "piao_success_rate": 0.9}
    base.update(kw)
    return base


def _hu(counts_hand, baotou=True, fan=2):
    from mj.tiles import to_counts
    return {"hu": True, "baotou": baotou, "fan": fan, "detail": [], "quads": 0,
            "counts": tuple(to_counts(counts_hand))}


class HeldJokerPiaoTests(unittest.TestCase):
    """手里原有的财神：打掉后仍爆头才飘。"""

    def test_drawn_joker_still_piao(self):
        """刚摸到的财神天然满足判据——原有行为不得回退。"""
        hand = ["1w", "1w", "1w", "2t", "2t", "2t", "3b", "3b", "3b", "5w", "5w", "9t", "9t", JOKER]
        action = choose_hu_or_piao(_snapshot(hand, JOKER), _hu(hand), LOOSE)
        self.assertEqual(action, {"action": "discard", "tile": JOKER})

    def test_no_joker_in_hand_never_piao(self):
        hand = ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w", "1t", "2t", "3t", "5b", "5b"]
        action = choose_hu_or_piao(_snapshot(hand, "5b"), _hu(hand), LOOSE)
        self.assertEqual(action["action"], "hu")

    def test_not_baotou_never_piao(self):
        hand = ["1w", "1w", "1w", "2t", "2t", "2t", "3b", "3b", "3b", "5w", "5w", "9t", "9t", JOKER]
        action = choose_hu_or_piao(_snapshot(hand, "9t"), _hu(hand, baotou=False), LOOSE)
        self.assertEqual(action["action"], "hu")

    def test_held_joker_blocked_when_discarding_it_breaks_baotou(self):
        """B 组：打掉手里那张财神之后不再爆头 —— 实测 31.2% 成功率、平均番 1.00，必须拒绝。"""
        hand = ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w", "1t", "2t", "3t", JOKER, "5b"]
        action = choose_hu_or_piao(_snapshot(hand, "5b"), _hu(hand), LOOSE)
        self.assertEqual(action["action"], "hu",
                         "打掉手里财神后不再爆头，不得财飘")

    def test_chain_cap_still_respected(self):
        hand = ["1w", "1w", "1w", "2t", "2t", "2t", "3b", "3b", "3b", "5w", "5w", "9t", "9t", JOKER]
        action = choose_hu_or_piao(_snapshot(hand, JOKER, chain_count=2, piao=1), _hu(hand), LOOSE)
        self.assertEqual(action["action"], "hu", "chain+piao>=3 时不得继续飘")

    def test_rollback_switch_disables_held_joker_piao(self):
        """piao_held_joker=0 必须完整回到「只飘刚摸到的那张」。"""
        hand = ["1w", "1w", "1w", "2t", "2t", "2t", "3b", "3b", "3b", "5w", "5w", "9t", "9t", JOKER]
        off = dict(LOOSE, piao_held_joker=0)
        self.assertEqual(choose_hu_or_piao(_snapshot(hand, "9t"), _hu(hand), off)["action"], "hu")
        self.assertEqual(choose_hu_or_piao(_snapshot(hand, JOKER), _hu(hand), off),
                         {"action": "discard", "tile": JOKER})
