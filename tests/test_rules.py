import random
import unittest

from mj.rules import _melds, baotou, evaluate, seven_pairs, win_standard
from mj.shanten import shanten
from mj.tiles import JOKER_IDX, TILE_INDEX, to_counts


class RulesTests(unittest.TestCase):
    def test_standard(self):
        tiles = ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w", "东", "东", "东", "发", "发"]
        self.assertTrue(win_standard(to_counts(tiles)))

    def test_joker_standard(self):
        tiles = ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w", "东", "东", "东", "发", "白"]
        self.assertTrue(win_standard(to_counts(tiles)))

    def test_seven_pairs(self):
        tiles = ["1w", "1w", "2w", "2w", "3w", "3w", "4w", "4w", "5w", "5w", "6w", "6w", "7w", "7w"]
        self.assertEqual(seven_pairs(to_counts(tiles)), 1)

    def test_seven_pairs_with_four_jokers(self):
        tiles = ["1w", "1w", "2w", "2w", "3w", "3w", "4w", "4w", "5w", "5w", "6w", "6w", "白", "白"]
        self.assertEqual(seven_pairs(to_counts(tiles)), 1)

    def test_evaluate_chain_and_four_jokers(self):
        hand = ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w", "东", "东", "东", "白"]
        result = evaluate(to_counts(hand), TILE_INDEX["白"], chain_count=1, piao=0)
        self.assertIsNotNone(result)
        self.assertGreaterEqual(result["fan"], 4)

    def test_baotou_false_for_normal_tenpai(self):
        hand = ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w", "东", "东", "南", "发"]
        self.assertFalse(baotou(to_counts(hand)))

        tiles = ["1w", "1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w", "东", "南", "发", "白"]
        self.assertFalse(win_standard(to_counts(tiles)))

    def test_shanten_win_is_zero_or_less(self):
        tiles = ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w", "东", "东", "东", "发", "发"]
        self.assertLessEqual(shanten(to_counts(tiles)), 0)

    def test_gang_open_independently_doubles_fan(self):
        """2026-09-24 correctness 修正（数据确认，见
        docs/experiments/OFFLINE_REPORT.md「剩余差距」十一.6）：此前认为
        "gang_open 已经计入 chain_count，不应再独立 ×2"是一条未经真实数据
        验证的假设。全量语料 round_ended 真实 detail/fan 字段核对（205 个
        真实样本，零反例）显示"杠开"是独立的 ×2 番值来源，与 chain_count
        是两套互不相关的机制，不存在双重计费问题——已改为无条件 ×2。
        """
        hand = ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w", "东", "东", "东", "发", "发"]
        without_gang_open = evaluate(to_counts(hand[:-1]), TILE_INDEX["发"], chain_count=1)
        with_gang_open = evaluate(to_counts(hand[:-1]), TILE_INDEX["发"], chain_count=1, gang_open=True)
        self.assertEqual(with_gang_open["fan"], without_gang_open["fan"] * 2)
        self.assertIn("杠开", with_gang_open["detail"])
        self.assertNotIn("杠开", without_gang_open["detail"])

    def test_gang_open_matches_real_observed_fan_values(self):
        """直接核对 tools/fan_breakdown.py::sanity_check 里两个真实观测到的
        detail 组合：单独"杠开"（平胡分支，无链）真实 fan=2；"杠开"+"爆头"
        真实 fan=4。用最简单的平胡+杠子手牌构造，不依赖链/白板/七对。"""
        hand = ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w",
               "东", "东", "东", "南", "南"]
        result = evaluate(to_counts(hand[:-1]), TILE_INDEX["南"], chain_count=0,
                          meld_groups=0, gang_open=True)
        self.assertIsNotNone(result)
        self.assertEqual(result["detail"], ["平胡", "杠开"])
        self.assertEqual(result["fan"], 2)


class CrossSuitMeldBugTests(unittest.TestCase):
    """2026-10-01 真实 bug 修复（a_db9e594544b6_r1_b4_t0 第 6 局，服务端
    "平胡"fan=1，引擎错判"平胡·爆头"fan=2）：``_melds`` 里"财神补顺子头/
    中缺"那个分支只检查了 span 起点跟锚点 ``i`` 同花色，没检查 span 终点
    ``start+2`` 也在同一花色——花色每 9 张一段连续排布，锚点落在花色最后
    两张（比如 9w）时，span 会悄悄跨到下一花色开头（1b），被当成"顺子"
    误判。见 ``mj/rules.py::_melds`` 里对应的注释。"""

    def test_cross_suit_pair_cannot_form_meld_even_with_joker(self):
        """8w、9w、1b 分属 w/b 两个花色，1 张财神顶多补一张同花色的缺口，
        不能跨花色拼成顺子——这是真实 bug 复现样本本身（见
        mj/mc/rollout.py 里引用的回放记录）。"""
        real = [0] * 34
        real[TILE_INDEX["9w"]] = 1
        real[TILE_INDEX["1b"]] = 1
        self.assertFalse(_melds(tuple(real), 1, 1))

    def test_isolated_singles_across_suits_with_joker_is_not_baotou(self):
        """真实回放里的完整手牌：引擎曾经错误地判定"摸任何牌都胡"（爆头），
        真实服务端判定只是普通平胡（没有爆头加成）。"""
        pre13 = ["1b", "1t", "2t", "3b", "3t", "4b", "5b", "9w", "白", "白"]
        self.assertFalse(baotou(to_counts(pre13), meld_groups=1))

    def test_same_suit_joker_filled_straight_still_works(self):
        """同花色的补顺子必须继续成立，不能被上面的修复误伤——7w/9w 加 1
        张财神补 8w，三张在同一花色里，是合法顺子。"""
        real = [0] * 34
        real[TILE_INDEX["7w"]] = 1
        real[TILE_INDEX["9w"]] = 1
        self.assertTrue(_melds(tuple(real), 1, 1))

    def test_same_suit_joker_filled_straight_from_low_end_still_works(self):
        """起点在花色最前端（1w）时同样的"补头"分支也要继续成立：1w 单张
        + 2 张财神补 2w3w，是合法顺子（花色内，不越界）。"""
        real = [0] * 34
        real[TILE_INDEX["1w"]] = 1
        self.assertTrue(_melds(tuple(real), 1, 2))


if __name__ == "__main__":
    unittest.main()
