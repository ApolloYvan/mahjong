import unittest

from mj.ev import choose_route_discard, hand_profile, route_value


class EvTests(unittest.TestCase):
    def test_profile_counts_joker(self):
        profile = hand_profile(["1w", "2w", "3w", "东", "东", "白"])
        self.assertEqual(profile["joker"], 1)

    def test_profile_disables_seven_pairs_after_meld(self):
        profile = hand_profile(["1w", "1w", "2w", "2w"], meld_groups=1)
        self.assertIsNone(profile["pairs"])

    def test_profile_tracks_seven_pairs_without_meld(self):
        profile = hand_profile(["1w", "1w", "2w", "2w"], meld_groups=0)
        self.assertEqual(profile["pairs"], None)

        hand = ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w", "东", "东", "南", "白", "发"]
        self.assertIsNotNone(choose_route_discard(hand, chain_count=1, piao=1, rules={"YouCaiBiKao": True}))


class MenqingBaotouWiringTests(unittest.TestCase):
    """S7 rule_menqing_baotou_enabled 接入 route_value：夹具与
    tests/test_joker_ev.py::MenqingBaotouBonusTests 用同一手牌，弃'中'
    向听==1/to_baotou==2，弃'7w'向听==1/to_baotou==3——开关打开后前者的
    value 应该比后者高出恰好 (3-2)*menqing_baotou_weight。"""

    HAND = ["白", "北", "北", "7w", "4w", "中", "3w", "8w", "6w", "8w",
           "5b", "3b", "北", "1w"]

    def test_enabled_adds_exact_bonus_to_value(self):
        from mj.fit import DEFAULT_WEIGHTS
        weights_off = dict(DEFAULT_WEIGHTS)
        weights_on = dict(DEFAULT_WEIGHTS, rule_menqing_baotou_enabled=1)
        v_better_off = route_value(self.HAND, "中", 0, weights=weights_off)
        v_better_on = route_value(self.HAND, "中", 0, weights=weights_on)
        v_worse_off = route_value(self.HAND, "7w", 0, weights=weights_off)
        v_worse_on = route_value(self.HAND, "7w", 0, weights=weights_on)
        # 打开开关后 value 恰好多加了 -distance*weight 这一项（to_baotou=2/3）。
        self.assertEqual(v_better_on - v_better_off, -2 * 200)
        self.assertEqual(v_worse_on - v_worse_off, -3 * 200)

    def test_enabled_widens_gap_in_favor_of_smaller_distance(self):
        from mj.fit import DEFAULT_WEIGHTS
        weights_off = dict(DEFAULT_WEIGHTS)
        weights_on = dict(DEFAULT_WEIGHTS, rule_menqing_baotou_enabled=1)
        gap_off = route_value(self.HAND, "中", 0, weights=weights_off) - \
            route_value(self.HAND, "7w", 0, weights=weights_off)
        gap_on = route_value(self.HAND, "中", 0, weights=weights_on) - \
            route_value(self.HAND, "7w", 0, weights=weights_on)
        # 开关打开后，'中'（to_baotou 更小）相对'7w'的优势应该扩大 200
        # （(-2*200) - (-3*200) = 200）。
        self.assertEqual(gap_on - gap_off, 200)
