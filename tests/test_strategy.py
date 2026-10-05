import unittest

from mj.bot import choose_action
from mj.joker_ev import joker_plan_value
from mj.shanten import pair_shanten, shanten
from mj.strategy import _discard_score, _wait_live_score, choose_discard
from mj.tiles import TILE_INDEX, to_counts, visible_counts


class WaitLiveScoreTests(unittest.TestCase):
    def test_wait_live_score_levels(self):
        waits = [TILE_INDEX["5w"], TILE_INDEX["2t"]]
        clean = [0] * 34
        semi = [0] * 34
        semi[TILE_INDEX["5w"]] = 3  # 剩 1 张
        dead = [0] * 34
        dead[TILE_INDEX["5w"]] = 4  # 亮光死听
        self.assertEqual(_wait_live_score(waits, None), 2.0)
        self.assertEqual(_wait_live_score(waits, clean), 2.0)
        self.assertEqual(_wait_live_score(waits, semi), 1.5)
        self.assertEqual(_wait_live_score(waits, dead), 1.0)

    def test_dead_wait_scores_lower(self):
        # 同一弃牌(5t牌 东 的对倒被亮光 4 张 → 死听得分低于全活
        hand = ["1w", "2w", "3w", "5w", "6w", "7w", "2t", "3t", "4t", "7t", "8t", "9t", "5t", "东"]
        clean = [0] * 34
        dead = [0] * 34
        dead[TILE_INDEX["东"]] = 4
        score_clean = _discard_score(hand, "5t", visible=clean)
        score_dead = _discard_score(hand, "5t", visible=dead)
        self.assertLess(score_dead, score_clean)

    def test_choose_discard_prefers_live_wait(self):
        # 双听牌选项: 留 5t 则听死 5t, 留 东 则听活 东 → 弃 5t 保活听
        hand = ["1w", "2w", "3w", "5w", "6w", "7w", "2t", "3t", "4t", "7t", "8t", "9t", "5t", "东"]
        rivers = [["5t", "5t", "5t"], [], [], []]
        visible = visible_counts(hand, rivers, [[], [], [], []])
        self.assertEqual(choose_discard(hand, visible=visible), "东")


class StrategyTests(unittest.TestCase):
    def test_joker_plan_tiers_after_revert(self):
        hand = ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "东", "东", "东", "南", "白"]
        counts = to_counts(hand)
        std = shanten(counts, 0)
        self.assertEqual(std, 0)
        normal = joker_plan_value(counts, 0, std, 10000, {})
        chained = joker_plan_value(counts, 0, std, 10000, {"opp_chain": True})
        dealer = joker_plan_value(counts, 0, std, 10000, {"dealer_hint": True})
        active = list(counts)
        active[TILE_INDEX["白"]] -= 1
        expected = -shanten(tuple(active), 0) * 10000 + 3000
        self.assertEqual(normal, expected)
        self.assertEqual(chained, expected)
        self.assertEqual(dealer, expected + 6000)

    def test_preserves_seven_pairs_progress(self):
        hand = ["1w", "1w", "2w", "2w", "3w", "3w", "4w", "4w", "5w", "5w", "6w", "白", "白", "南"]
        choice = choose_discard(hand)
        self.assertNotEqual(choice, "白")
        remaining = list(hand)
        remaining.remove(choice)
        self.assertLessEqual(pair_shanten(to_counts(remaining)), 1)

        snapshot = {"seat": 1, "phase": "response_peng", "responding_seats": [2]}
        self.assertIsNone(choose_action(snapshot))

    def test_choose_hu_for_standard_draw(self):
        hand = ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w", "东", "东", "东", "发"]
        snapshot = {"seat": 0, "phase": "draw", "turn": 0, "my_hand": hand, "drawn_tile": "发"}
        self.assertEqual(choose_action(snapshot)["action"], "hu")

    def test_chain_preserves_joker(self):
        hand = ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w", "东", "东", "南", "白", "发"]
        self.assertNotEqual(choose_discard(hand, chain_count=2), "白")

        hand = ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w", "东", "东", "南", "白", "发"]
        self.assertNotEqual(choose_discard(hand), "白")

    def test_discards_isolated_honor(self):
        hand = ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w", "东", "东", "东", "南", "白"]
        self.assertEqual(choose_discard(hand), "南")


class MenqingBaotouWiringTests(unittest.TestCase):
    """S7 rule_menqing_baotou_enabled 接入 _discard_score——同一夹具见
    tests/test_ev.py::MenqingBaotouWiringTests（两份平行评分体都要覆盖，
    不是"改一处漏一处"）。"""

    HAND = ["白", "北", "北", "7w", "4w", "中", "3w", "8w", "6w", "8w",
           "5b", "3b", "北", "1w"]

    def test_enabled_adds_exact_bonus_to_score(self):
        from mj.fit import DEFAULT_WEIGHTS
        weights_off = dict(DEFAULT_WEIGHTS)
        weights_on = dict(DEFAULT_WEIGHTS, rule_menqing_baotou_enabled=1)
        s_better_off = _discard_score(self.HAND, "中", 0, weights=weights_off)
        s_better_on = _discard_score(self.HAND, "中", 0, weights=weights_on)
        s_worse_off = _discard_score(self.HAND, "7w", 0, weights=weights_off)
        s_worse_on = _discard_score(self.HAND, "7w", 0, weights=weights_on)
        self.assertEqual(s_better_on - s_better_off, -2 * 200)
        self.assertEqual(s_worse_on - s_worse_off, -3 * 200)
