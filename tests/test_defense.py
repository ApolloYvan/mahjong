import unittest

from mj.defense import opp_stats, penalties
from mj.tiles import TILE_INDEX

TILE_HONOR_ZHONG = TILE_INDEX["中"]


def snapshot(melds, discards=None, wall=30, seat=0, hand=None):
    return {
        "seat": seat,
        "wall_remaining": wall,
        "melds": melds,
        "discards": discards or [[], [], [], []],
        "my_hand": hand or [],
    }


CHI = {"kind": "chi", "tiles": ["1w", "2w", "3w"]}
CHI2 = {"kind": "chi", "tiles": ["4w", "5w", "6w"]}
PENG_DONG = {"kind": "peng", "tiles": ["东", "东", "东"]}
CHI_B = {"kind": "chi", "tiles": ["1b", "2b", "3b"]}


class DefenseNeedTests(unittest.TestCase):
    def test_no_sprint_no_penalties(self):
        melds = [[], [CHI], [], []]
        self.assertEqual(penalties(snapshot(melds)), {})

    def test_wall_deep_no_penalties(self):
        melds = [[], [CHI, CHI2], [], []]
        self.assertEqual(penalties(snapshot(melds, wall=50)), {})

    def test_sprint_collecting_suit_feeds(self):
        melds = [[], [CHI, CHI2], [], []]
        result = penalties(snapshot(melds))
        self.assertEqual(result.get("7w"), 900)
        self.assertNotIn("9t", result)

    def test_river_discounts_given_up_suit(self):
        melds = [[], [CHI, CHI2], [], []]
        discards = [[], ["1w", "2w", "3w", "9w"], [], []]
        result = penalties(snapshot(melds, discards))
        # 需求 6-4=2 → 480 + 邻接 120 + 剩余 180
        self.assertEqual(result.get("7w"), 780)

    def test_discarded_honor_safe_to_feed(self):
        melds = [[], [PENG_DONG, CHI_B], [], []]
        discards = [[], ["中"], [], []]
        result = penalties(snapshot(melds, discards))
        self.assertNotIn("中", result)
        # 碰风险 120 + 剩余 180
        self.assertEqual(result.get("发"), 300)

    def test_adjacent_to_exposed_meld_feeds(self):
        melds = [[], [PENG_DONG, CHI_B], [], []]
        result = penalties(snapshot(melds))
        self.assertIn("2b", result)
        self.assertNotIn("5t", result)

    def test_opp_stats_need_and_honors(self):
        stats = opp_stats(1, [[], ["1w", "中"], [], []], [[], [CHI, PENG_DONG], [], []])
        self.assertEqual(stats["meld_groups"], 2)
        self.assertEqual(stats["need"][0], 2)
        self.assertEqual(stats["river_honors"], {TILE_HONOR_ZHONG})


if __name__ == "__main__":
    unittest.main()
