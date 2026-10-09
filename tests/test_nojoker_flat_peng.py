"""2026-10-08：flat_peng_veto_nojoker——无财神时不碰向听不变、也不成爆头的牌；有财神/关开关时不变。"""
import unittest

from mj.fit import thread_weights_overlay
from mj.responses import choose_peng

# 碰 5b：向听不变（同 test_dealer_flat_peng 的手牌，把财神换成 1w）
HAND = ["3b", "3t", "3w", "4b", "4t", "5b", "5b", "6b", "8w", "9b", "9b", "南", "1w"]
JOKER_HAND = [t if t != "1w" else "白" for t in HAND]


def _snap(hand, dealer=1):
    return {"window_tile": "5b", "my_hand": list(hand), "seat": 0, "dealer": dealer,
            "melds": [[], [], [], []], "wall_remaining": 60}


class NoJokerFlatPengTests(unittest.TestCase):
    def test_off_keeps_behavior(self):
        with thread_weights_overlay({"flat_peng_veto_nojoker": 0}):
            base = choose_peng(_snap(HAND))
        with thread_weights_overlay({"flat_peng_veto_nojoker": 0}):
            self.assertEqual(choose_peng(_snap(HAND)), base)

    def test_on_vetoes_without_joker(self):
        for dealer in (0, 1):
            with thread_weights_overlay({"flat_peng_veto_nojoker": 1, "dealer_joker_flat_peng_veto": 0}):
                self.assertIsNone(choose_peng(_snap(HAND, dealer)))

    def test_on_ignores_joker_hand(self):
        with thread_weights_overlay({"flat_peng_veto_nojoker": 1, "dealer_joker_flat_peng_veto": 0}):
            self.assertEqual(choose_peng(_snap(JOKER_HAND)), {"action": "peng", "tile": "5b"})


if __name__ == "__main__":
    unittest.main()
