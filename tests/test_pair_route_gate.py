"""七对路线门禁回归测试（2026-09-22 弃牌目标函数修复）。

覆盖 mj.shanten.pair_route_allowed 及其在 route_shanten / combined_route /
mj.strategy._discard_score / mj.ev.route_value / mj.ev.pair_route_bonus 里的
接线。门槛：meld_groups == 0 且 手上 >=5 个真实对子（财神不算）且
pair_shanten 严格小于 shanten，才允许七对路线参与门清弃牌分层与评分。
"""
import unittest

from mj.ev import pair_route_bonus, route_value
from mj.fit import DEFAULT_WEIGHTS
from mj.shanten import combined_route, pair_route_allowed, pair_shanten, route_shanten, shanten, ukeire
from mj.strategy import _discard_score
from mj.tiles import to_counts

# 5 个真实对子 + 3 单张：pair_shanten(1) 严格快于 shanten(2)。
FIVE_PAIRS_FASTER = ["1w", "1w", "2w", "2w", "3w", "3w", "5b", "5b", "6b", "6b", "9t", "东", "南"]

# 4 个真实对子，散张，pair_shanten(2) 严格快于 shanten(7)——即便更快也应被挡。
FOUR_PAIRS_FASTER = ["1w", "1w", "5w", "5w", "2b", "2b", "7b", "7b", "东", "南", "西", "北", "中"]

# 5 个真实对子，但 pair_shanten == shanten == 1（持平）。
FIVE_PAIRS_TIE = ["1w", "1w", "2w", "2w", "3w", "3w", "5b", "5b", "6b", "6b", "7b", "8b", "9b"]

# 2 张财神 + 4 个真实对子 + 3 单张：real_pairs 必须算 4，不能把财神刷成 5。
TWO_JOKER_FOUR_PAIRS = ["白", "白", "1w", "1w", "2w", "2w", "3w", "3w", "5b", "5b", "9t", "东", "南"]

# shanten<=2 时仍是 4 个真实对子（用于验证 combined_route 不泄漏 pair_ukeire）。
FOUR_PAIRS_LOW_SHANTEN = ["1w", "1w", "2w", "2w", "3w", "3w", "5b", "5b", "6b", "7b", "8b", "9t", "9t"]


class TestPairRouteAllowed(unittest.TestCase):
    def test_five_real_pairs_and_strictly_faster_allows_pair_route(self):
        counts = to_counts(FIVE_PAIRS_FASTER)
        self.assertLess(pair_shanten(counts), shanten(counts, 0))
        self.assertTrue(pair_route_allowed(counts, 0))
        self.assertEqual(route_shanten(counts, 0), pair_shanten(counts))

    def test_four_pairs_blocks_pair_route_even_if_faster(self):
        counts = to_counts(FOUR_PAIRS_FASTER)
        self.assertLess(pair_shanten(counts), shanten(counts, 0))
        self.assertFalse(pair_route_allowed(counts, 0))
        self.assertEqual(route_shanten(counts, 0), shanten(counts, 0))

    def test_tie_blocks_pair_route(self):
        counts = to_counts(FIVE_PAIRS_TIE)
        self.assertEqual(pair_shanten(counts), shanten(counts, 0))
        self.assertFalse(pair_route_allowed(counts, 0))
        self.assertEqual(route_shanten(counts, 0), shanten(counts, 0))

    def test_joker_not_counted_as_real_pair(self):
        from mj.tiles import JOKER_IDX

        counts = to_counts(TWO_JOKER_FOUR_PAIRS)
        # 直接核对 real_pairs 的定义：财神槽位不计入，只有 4 组真实对子。
        real_pairs = sum(1 for i, n in enumerate(counts) if n >= 2 and i != JOKER_IDX)
        self.assertEqual(real_pairs, 4)
        self.assertFalse(pair_route_allowed(counts, 0))

    def test_open_hand_never_allows_pair_route(self):
        counts = to_counts(FIVE_PAIRS_FASTER)
        for meld_groups in (1, 2, 3, 4):
            self.assertFalse(pair_route_allowed(counts, meld_groups))

    def test_combined_route_does_not_leak_pair_ukeire_when_blocked(self):
        counts = to_counts(FOUR_PAIRS_LOW_SHANTEN)
        self.assertFalse(pair_route_allowed(counts, 0))
        current, waits = combined_route(counts, 0)
        self.assertLessEqual(current, 2)
        self.assertEqual(waits, ukeire(counts, 0))

    def test_discard_score_pair_terms_inactive_when_blocked(self):
        tiles14 = FOUR_PAIRS_LOW_SHANTEN + ["4b"]
        weights_a = dict(DEFAULT_WEIGHTS)
        weights_b = dict(DEFAULT_WEIGHTS)
        weights_b.update(b_pair=99999, b_progress=99999, baohua_ticket=0, pair_route=99999)
        score_a = _discard_score(tiles14, "4b", 0, 0, 0, None, weights_a, None)
        score_b = _discard_score(tiles14, "4b", 0, 0, 0, None, weights_b, None)
        self.assertEqual(score_a, score_b)

    def test_strategy_and_ev_agree_on_gate(self):
        blocked14 = FOUR_PAIRS_LOW_SHANTEN + ["4b"]
        allowed14 = FIVE_PAIRS_FASTER + ["4b"]
        for tiles14, expect_gated_off in ((blocked14, True), (allowed14, False)):
            weights_strategy_a = dict(DEFAULT_WEIGHTS)
            weights_strategy_b = dict(DEFAULT_WEIGHTS)
            weights_strategy_b.update(b_pair=99999, b_progress=99999, baohua_ticket=0, pair_route=99999)
            strategy_delta = (
                _discard_score(tiles14, "4b", 0, 0, 0, None, weights_strategy_b, None)
                - _discard_score(tiles14, "4b", 0, 0, 0, None, weights_strategy_a, None)
            )

            weights_ev_a = dict(DEFAULT_WEIGHTS)
            weights_ev_b = dict(DEFAULT_WEIGHTS)
            weights_ev_b.update(pair=99999, seven_pairs_route=99999, pair_route=99999, baohua_ticket=0)
            ev_delta = (
                route_value(tiles14, "4b", 0, 0, 0, None, weights_ev_b, None)
                - route_value(tiles14, "4b", 0, 0, 0, None, weights_ev_a, None)
            )

            self.assertEqual(strategy_delta == 0, expect_gated_off)
            self.assertEqual(ev_delta == 0, expect_gated_off)
            self.assertEqual(strategy_delta == 0, ev_delta == 0)

    def test_pair_route_bonus_gated_directly(self):
        blocked_counts = to_counts(FOUR_PAIRS_LOW_SHANTEN)
        allowed_counts = to_counts(FIVE_PAIRS_FASTER)
        self.assertEqual(pair_route_bonus(blocked_counts, 0, DEFAULT_WEIGHTS), 0)
        self.assertGreater(pair_route_bonus(allowed_counts, 0, DEFAULT_WEIGHTS), 0)
        self.assertEqual(pair_route_bonus(allowed_counts, 1, DEFAULT_WEIGHTS), 0)


if __name__ == "__main__":
    unittest.main()
