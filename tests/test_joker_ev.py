import unittest

from mj.joker_ev import joker_plan_value, menqing_baotou_bonus, to_baotou_distance
from mj.shanten import route_shanten, shanten
from mj.tiles import TILE_INDEX, to_counts


def _active(counts, removed_jokers):
    values = list(counts)
    values[TILE_INDEX["白"]] -= removed_jokers
    return tuple(values)


def _premium(s_active, std_current, jokers, rules):
    if s_active == 0:
        return 3000
    if s_active == std_current:
        return 1500
    if s_active == std_current + 1:
        return 9000 if ((rules or {}).get("dealer_hint") or jokers >= 2) else 3000
    return 0


class JokerPlanTests(unittest.TestCase):
    def test_no_joker_returns_none(self):
        counts = to_counts(["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w", "1b", "2b", "3b", "4b"])
        self.assertIsNone(joker_plan_value(counts, 0, 0, 10000))

    def test_chain_active_returns_none(self):
        counts = to_counts(["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w", "1b", "2b", "3b", "白"])
        self.assertIsNone(joker_plan_value(counts, 0, 0, 10000, {"chain_active": True}))

    def test_four_melds_plus_joker_is_all_wait(self):
        # 四组面子 + 白 = 摸任何牌即胡（爆头全听）
        tiles = ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w", "1b", "2b", "3b", "白"]
        counts = to_counts(tiles)
        std = shanten(counts, 0)
        self.assertEqual(std, 0)
        plan = joker_plan_value(counts, 0, std, 10000)
        active = _active(counts, 1)
        self.assertEqual(shanten(active, 0), 0)
        self.assertEqual(plan, 3000)

    def test_slow1_tier_single_joker(self):
        tiles = ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "东", "东", "东", "南", "白"]
        counts = to_counts(tiles)
        std = shanten(counts, 0)
        self.assertEqual(std, 0)
        plan = joker_plan_value(counts, 0, std, 10000)
        active = _active(counts, 1)
        expected = -shanten(active, 0) * 10000 + 3000
        self.assertEqual(plan, expected)
        dealer = joker_plan_value(counts, 0, std, 10000, {"dealer_hint": True})
        self.assertEqual(dealer, expected + 6000)

    def test_two_jokers_enumerates_splits(self):
        # 白白 + 三组成型: d+白 恒为对 → 全听成立, all_wait 溢价
        tiles = ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w", "1b", "2b", "白", "白"]
        counts = to_counts(tiles)
        std = shanten(counts, 0)
        plan = joker_plan_value(counts, 0, std, 10000)
        self.assertEqual(plan, 3000)

    def test_non_all_wait_tenpai_gets_slow1(self):
        # 三搭子成型听牌(摸东不胡): 非全听 → slow1 档, 庄家 hints 9000 而非 all_wait 3000
        tiles = ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w", "2t", "3t", "5t", "白"]
        counts = to_counts(tiles)
        std = shanten(counts, 0)
        plan = joker_plan_value(counts, 0, std, 10000)
        self.assertEqual(plan, -7000)
        dealer = joker_plan_value(counts, 0, std, 10000, {"dealer_hint": True})
        self.assertEqual(dealer, -1000)

    def test_melded_hand_still_plans(self):
        # 有副露时白仍按留将/熔搭枚举（10 张主手 + 1 白）
        tiles = ["1w", "2w", "3w", "4w", "5w", "6w", "东", "东", "南", "白"]
        counts = to_counts(tiles)
        std = shanten(counts, 1)
        plan = joker_plan_value(counts, 1, std, 10000)
        self.assertIsNotNone(plan)


class ToBaotouDistanceTests(unittest.TestCase):
    """mj.joker_ev.to_baotou_distance 是 tools/mining_common.py::to_baotou
    的移植——用同一组随机手牌核对两边逐一相等（不是重新发明算法）。"""

    def test_matches_offline_tool_implementation(self):
        import random
        import sys
        sys.path.insert(0, ".")
        from tools.mining_common import to_baotou as offline_to_baotou

        rng = random.Random(20260925)
        suits = ["w", "b", "t"]
        honors = ["东", "南", "西", "北", "中", "发", "白"]
        for _ in range(200):
            pool = []
            for s in suits:
                for n in range(1, 10):
                    pool.extend(["%d%s" % (n, s)] * 4)
            pool.extend(h for h in honors for _ in range(4))
            rng.shuffle(pool)
            hand = ["白"]
            counts = {"白": 1}
            for t in pool:
                if counts.get(t, 0) >= 4:
                    continue
                hand.append(t)
                counts[t] = counts.get(t, 0) + 1
                if len(hand) == 13:
                    break
            c = to_counts(hand)
            meld_groups = rng.choice([0, 1, 2])
            self.assertEqual(to_baotou_distance(c, meld_groups), offline_to_baotou(c, meld_groups))

    def test_no_joker_returns_none(self):
        hand = ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w", "1b", "2b", "3b", "4b"]
        self.assertIsNone(to_baotou_distance(to_counts(hand), 0))


class MenqingBaotouBonusTests(unittest.TestCase):
    """rule_menqing_baotou_enabled（默认关闭）：S7，只在"候选弃牌之后
    向听恰好==1 且财神数恰好==1"这一窄格生效。来源、Δ、CI、覆盖率见
    mj/fit.py::DEFAULT_WEIGHTS 与本文件同名开关旁的注释，完整数字见
    docs/experiments/OFFLINE_REPORT.md「五问结论」S7。

    夹具：14 张手牌 ['白','北','北','7w','4w','中','3w','8w','6w','8w',
    '5b','3b','北','1w']，用脚本核实过：弃'中'与弃'7w'向听都是 1（同一个
    tier），但 to_baotou_distance 分别是 2 和 3——用于验证"同向听时，
    to_baotou 更小的候选分数更高"。
    """

    HAND = ["白", "北", "北", "7w", "4w", "中", "3w", "8w", "6w", "8w",
           "5b", "3b", "北", "1w"]

    def _counts_after(self, discard):
        left = list(self.HAND)
        left.remove(discard)
        return to_counts(left)

    def test_default_off_returns_zero(self):
        counts = self._counts_after("中")
        current = route_shanten(counts, 0)
        self.assertEqual(current, 1)
        self.assertEqual(menqing_baotou_bonus(counts, 0, current, weights={}), 0)

    def test_wrong_shanten_returns_zero_even_when_enabled(self):
        # current 强行传成 0（不是这一格）：即使开关打开也不应该生效。
        counts = self._counts_after("中")
        weights = {"rule_menqing_baotou_enabled": 1, "menqing_baotou_weight": 200}
        self.assertEqual(menqing_baotou_bonus(counts, 0, 0, weights=weights), 0)

    def test_wrong_joker_count_returns_zero_even_when_enabled(self):
        # 手动清零财神：即使 current==1、开关打开，财神数不是 1 也不生效。
        counts = list(self._counts_after("中"))
        counts[TILE_INDEX["白"]] = 0
        weights = {"rule_menqing_baotou_enabled": 1, "menqing_baotou_weight": 200}
        self.assertEqual(menqing_baotou_bonus(tuple(counts), 0, 1, weights=weights), 0)

    def test_enabled_scores_smaller_distance_higher(self):
        weights = {"rule_menqing_baotou_enabled": 1, "menqing_baotou_weight": 200}
        counts_better = self._counts_after("中")   # to_baotou=2
        counts_worse = self._counts_after("7w")    # to_baotou=3
        current_better = route_shanten(counts_better, 0)
        current_worse = route_shanten(counts_worse, 0)
        self.assertEqual(current_better, 1)
        self.assertEqual(current_worse, 1)
        self.assertEqual(to_baotou_distance(counts_better, 0), 2)
        self.assertEqual(to_baotou_distance(counts_worse, 0), 3)
        bonus_better = menqing_baotou_bonus(counts_better, 0, current_better, weights=weights)
        bonus_worse = menqing_baotou_bonus(counts_worse, 0, current_worse, weights=weights)
        self.assertEqual(bonus_better, -400)   # -2*200
        self.assertEqual(bonus_worse, -600)    # -3*200
        self.assertGreater(bonus_better, bonus_worse)

    def test_default_weight_is_zero(self):
        from mj.fit import DEFAULT_WEIGHTS
        self.assertEqual(DEFAULT_WEIGHTS["rule_menqing_baotou_enabled"], 0)


if __name__ == "__main__":
    unittest.main()
