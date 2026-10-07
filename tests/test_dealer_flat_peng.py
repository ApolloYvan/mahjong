"""2026-10-07：dealer_joker_flat_peng_veto——做庄且有财神时，不碰向听不变、也不成爆头的牌；闲家/关开关时不变。"""
import unittest

from mj.fit import thread_weights_overlay
from mj.responses import choose_peng

HAND = ["3b", "3t", "3w", "4b", "4t", "5b", "5b", "6b", "8w", "9b", "9b", "南", "白"]   # 碰 5b：向听 2 → 2


def _snap(dealer, hand=HAND):
    return {"window_tile": "5b", "my_hand": list(hand), "seat": 0, "dealer": dealer,
            "melds": [[], [], [], []], "wall_remaining": 60}


class DealerFlatPengTests(unittest.TestCase):
    def test_off_by_default_keeps_peng(self):
        with thread_weights_overlay({"dealer_joker_flat_peng_veto": 0}):
            self.assertEqual(choose_peng(_snap(dealer=0)), {"action": "peng", "tile": "5b"})

    def test_dealer_with_joker_vetoed(self):
        with thread_weights_overlay({"dealer_joker_flat_peng_veto": 1}):
            self.assertIsNone(choose_peng(_snap(dealer=0)))

    def test_idle_unchanged(self):
        with thread_weights_overlay({"dealer_joker_flat_peng_veto": 1}):
            self.assertEqual(choose_peng(_snap(dealer=1)), {"action": "peng", "tile": "5b"})

    def test_dealer_without_joker_unchanged(self):
        hand = [t if t != "白" else "1w" for t in HAND]
        with thread_weights_overlay({"dealer_joker_flat_peng_veto": 1}):
            self.assertEqual(choose_peng(_snap(dealer=0, hand=hand)),
                             choose_peng({**_snap(dealer=1, hand=hand)}))


if __name__ == "__main__":
    unittest.main()
