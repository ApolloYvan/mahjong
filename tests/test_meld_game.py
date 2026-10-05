import unittest

from mj.gamesim import _evaluate_hand


class MeldGameTests(unittest.TestCase):
    def test_open_meld_uses_remaining_groups(self):
        hand = ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w", "东", "东"]
        melds = [{"kind": "peng", "tiles": ["发"] * 3}]
        result = _evaluate_hand(hand, melds=melds)
        self.assertIsNotNone(result)

    def test_open_meld_disables_seven_pairs(self):
        hand = ["1w", "1w", "2w", "2w", "3w", "3w", "4w", "4w", "5w", "5w", "6w"]
        melds = [{"kind": "peng", "tiles": ["发"] * 3}]
        self.assertIsNone(_evaluate_hand(hand, melds=melds))
