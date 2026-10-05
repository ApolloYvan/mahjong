import unittest

from mj.gamesim import _evaluate_hand
from mj.tiles import to_counts


class SimulationRuleTests(unittest.TestCase):
    def test_joker_standard_fan(self):
        hand = ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w", "东", "东", "东", "发", "发"]
        result = _evaluate_hand(hand, chain_count=1)
        self.assertIsNotNone(result)
        self.assertGreaterEqual(result["fan"], 2)

    def test_seven_pairs_fan(self):
        hand = ["1w", "1w", "2w", "2w", "3w", "3w", "4w", "4w", "5w", "5w", "6w", "6w", "7w", "7w"]
        self.assertEqual(_evaluate_hand(hand)["fan"], 2)
