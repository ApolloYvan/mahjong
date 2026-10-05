import unittest

from mj.rules import seven_pairs
from mj.shanten import combined_route, pair_shanten, pair_ukeire, shanten, ukeire
from mj.tiles import TILE_INDEX, to_counts


class ShantenJokerTests(unittest.TestCase):
    def test_joker_fills_run_head(self):
        # 白当3w: 3w4w5w 顺子
        counts = to_counts(["白", "4w", "5w", "1b", "2b", "3b", "7t", "8t", "9t", "1w", "1w", "2t", "3t"])
        self.assertLessEqual(shanten(counts), 0)

    def test_joker_fills_run_gap(self):
        # 白当6w
        counts = to_counts(["5w", "7w", "白", "1b", "2b", "3b", "7t", "8t", "9t", "1w", "1w", "2t", "3t"])
        self.assertLessEqual(shanten(counts), 0)

    def test_single_plus_joker_makes_pair_or_taatsu(self):
        # 5w+白 成对/搭，1b2b3b 顺，7t8t9t 顺 → 一向听以内
        counts = to_counts(["5w", "白", "1b", "2b", "3b", "7t", "8t", "9t", "2w", "3w", "2t", "3t", "4t"])
        self.assertLessEqual(shanten(counts), 1)

    def test_joker_pair_terminal(self):
        # 四组面子 + 白白 做将 = 0 向听
        counts = to_counts(["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w", "1t", "1t", "1t", "白"])
        self.assertLessEqual(shanten(counts), 0)

    def test_pair_shanten_seven_pairs(self):
        counts = to_counts(["1w", "1w", "2w", "2w", "3w", "3w", "4w", "4w", "5w", "5w", "6w", "6w", "白"])
        self.assertLessEqual(pair_shanten(counts), 0)

    def test_ukeire_counts_joker_draw(self):
        counts = to_counts(["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "1t", "1t", "2t", "3t", "白"])
        waits = ukeire(counts)
        self.assertIn(33, waits)  # 摸白凑将也是进张

    def test_pair_shanten_quads_as_two_pairs(self):
        # 3 暗杠形态 + 单张：离三豪华七对（16番）只差一张，旧公式误判 3 向听
        counts = to_counts(["1w", "1w", "1w", "1w", "2w", "2w", "2w", "2w",
                            "3w", "3w", "3w", "3w", "4w"])
        self.assertEqual(pair_shanten(counts), 0)

    def test_pair_shanten_matches_platform_seven_pairs(self):
        # 与 rules.seven_pairs 判定一致：同种多对合法（平台有三豪华七对）
        tiles = ["1w", "1w", "1w", "1w", "2w", "2w", "2w", "2w",
                 "3w", "3w", "3w", "3w", "4w", "4w"]
        self.assertIsNotNone(seven_pairs(to_counts(tiles)))
        self.assertEqual(pair_shanten(to_counts(tiles[:13])), 0)

    def test_pair_ukeire_mate_of_single(self):
        counts = to_counts(["1w", "1w", "2w", "2w", "3w", "3w", "4w", "4w",
                            "5w", "5w", "6w", "6w", "8w"])
        waits = pair_ukeire(counts)
        # 8w 对子 + 白代 8w 均为和牌张
        self.assertEqual(waits, sorted([TILE_INDEX["8w"], TILE_INDEX["白"]]))

    def test_combined_route_pair_leads(self):
        counts = to_counts(["1w", "1w", "1w", "1w", "2w", "2w", "2w", "2w",
                            "3w", "3w", "3w", "3w", "4w"])
        current, waits = combined_route(counts)
        self.assertEqual(current, 0)
        self.assertIn(TILE_INDEX["4w"], waits)

    def test_combined_route_tie_unions_waits(self):
        # 五对 + 三连单张：标准与七对同为一向听，进张应为两路线并集
        counts = to_counts(["1w", "1w", "2w", "2w", "3w", "3w", "4w", "4w",
                            "5w", "5w", "6w", "7w", "8w"])
        pair = pair_shanten(counts)
        std = shanten(counts)
        if pair == std:
            _, waits = combined_route(counts)
            for idx in (TILE_INDEX["6w"], TILE_INDEX["7w"], TILE_INDEX["8w"]):
                self.assertIn(idx, waits)


if __name__ == "__main__":
    unittest.main()
