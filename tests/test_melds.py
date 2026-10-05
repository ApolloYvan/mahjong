import unittest

from mj.melds import apply_chi, apply_gang, apply_peng, can_chi, hand_shape_valid, meld_count


class MeldTests(unittest.TestCase):
    def test_peng(self):
        hand = ["5w", "5w", "1w"]
        melds = []
        self.assertTrue(apply_peng(hand, melds, "5w"))
        self.assertEqual(meld_count(melds, "peng"), 1)

    def test_chi_limit(self):
        hand = ["1w", "2w", "4w", "5w"]
        melds = [{"kind": "chi", "tiles": ["1w", "2w", "3w"]}, {"kind": "chi", "tiles": ["4w", "5w", "6w"]}]
        self.assertFalse(can_chi(melds))
        self.assertFalse(apply_chi(hand, melds, ["1w", "2w"], "3w"))

    def test_shape_after_peng(self):
        hand = ["1w"] * 11
        melds = [{"kind": "peng", "tiles": ["5w"] * 3}]
        self.assertTrue(hand_shape_valid(hand, melds))

        hand = ["白", "白", "白", "白"]
        melds = []
        self.assertTrue(apply_gang(hand, melds, "白"))
