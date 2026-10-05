import unittest

from mj.gamesim import _evaluate_hand


class GangSimulationTests(unittest.TestCase):
    def test_chain_doubles_fan(self):
        hand = ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w", "东", "东", "东", "发", "发"]
        result = _evaluate_hand(hand, chain_count=2, gang_open=True)
        self.assertIsNotNone(result)
        self.assertGreaterEqual(result["fan"], 4)
        self.assertIn("杠开", result["detail"])
