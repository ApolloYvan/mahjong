import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mj.mc.determinize import determinize, determinize_batch, hand_size  # noqa: E402
from mj.tiles import ALL_TILES, to_counts  # noqa: E402


def _sample_snapshot():
    my_hand = [ALL_TILES[i % 34] for i in range(13)]
    return {
        "seat": 0, "turn": 0, "phase": "draw", "dealer": 0, "round_no": 1,
        "wall_remaining": 60, "scores": [0, 0, 0, 0],
        "my_hand": my_hand, "discards": [[], [], [], []],
        "melds": [[], [], [], []], "drawn_tile": "",
    }


class TestHandSize(unittest.TestCase):
    def test_no_melds_is_13(self):
        snap = _sample_snapshot()
        self.assertEqual(hand_size(1, snap), 13)

    def test_current_turn_draw_adds_one(self):
        snap = _sample_snapshot()
        self.assertEqual(hand_size(0, snap), 14)

    def test_each_meld_removes_three(self):
        snap = _sample_snapshot()
        snap["melds"][1] = [{"kind": "chi", "tiles": ["1w", "2w", "3w"]},
                            {"kind": "peng", "tiles": ["东", "东", "东"]}]
        self.assertEqual(hand_size(1, snap), 13 - 6)


class TestDeterminize(unittest.TestCase):
    def test_partition_sizes_and_disjoint(self):
        snap = _sample_snapshot()
        deal = determinize(snap, rng=__import__("random").Random(0))
        self.assertEqual(set(deal.keys()), {1, 2, 3, "wall"})
        for seat in (1, 2, 3):
            self.assertEqual(len(deal[seat]), hand_size(seat, snap))
        self.assertEqual(len(deal["wall"]), snap["wall_remaining"])
        all_tiles = deal[1] + deal[2] + deal[3] + deal["wall"] + snap["my_hand"]
        counts = to_counts(all_tiles)
        self.assertTrue(all(0 <= c <= 4 for c in counts))

    def test_batch_is_deterministic_given_seed(self):
        snap = _sample_snapshot()
        a = determinize_batch(snap, 5, seed=42)
        b = determinize_batch(snap, 5, seed=42)
        self.assertEqual(a, b)

    def test_batch_differs_across_seeds_usually(self):
        snap = _sample_snapshot()
        a = determinize_batch(snap, 1, seed=1)
        b = determinize_batch(snap, 1, seed=2)
        self.assertNotEqual(a, b)

    def test_weight_hook_not_implemented(self):
        snap = _sample_snapshot()
        with self.assertRaises(NotImplementedError):
            determinize(snap, opponent_weight_hook=lambda *a: 1.0)


if __name__ == "__main__":
    unittest.main()
