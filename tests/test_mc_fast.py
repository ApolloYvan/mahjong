import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mj.mc.fast import hu_distance, is_baotou, is_hu  # noqa: E402
from mj.rules import _wins_any, baotou  # noqa: E402
from mj.shanten import pair_shanten, shanten  # noqa: E402
from mj.tiles import ALL_TILES, to_counts  # noqa: E402


def _random_hand(rng, n, max_joker=4):
    pool = []
    for t in ALL_TILES:
        pool.extend([t] * 4)
    rng.shuffle(pool)
    hand = pool[:n]
    return hand


class TestFastEquivalence(unittest.TestCase):
    def test_hu_distance_matches_min_of_shanten_and_pair_shanten(self):
        rng = random.Random(0)
        for _ in range(500):
            hand = _random_hand(rng, 13)
            counts = to_counts(hand)
            expected = min(shanten(counts, 0), pair_shanten(counts))
            self.assertEqual(hu_distance(counts, 0), expected)

    def test_hu_distance_with_melds_excludes_qidui(self):
        rng = random.Random(1)
        for _ in range(200):
            meld_groups = rng.randint(1, 4)
            hand = _random_hand(rng, 13 - 3 * meld_groups)
            counts = to_counts(hand)
            self.assertEqual(hu_distance(counts, meld_groups), shanten(counts, meld_groups))

    def test_is_hu_matches_wins_any(self):
        rng = random.Random(2)
        for _ in range(500):
            hand = _random_hand(rng, 14)
            counts = to_counts(hand)
            self.assertEqual(is_hu(counts, 0), bool(_wins_any(counts, 0)))

    def test_is_baotou_matches_rules_baotou(self):
        rng = random.Random(3)
        for _ in range(200):
            hand = _random_hand(rng, 13)
            counts = to_counts(hand)
            self.assertEqual(is_baotou(counts, 0), bool(baotou(counts, 0)))


if __name__ == "__main__":
    unittest.main()
