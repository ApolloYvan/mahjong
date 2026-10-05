import unittest

from mj.ev import route_value
from mj.fit import load_weights
from mj.strategy import _discard_score


def w(ticket):
    return {**load_weights(), "baohua_ticket": ticket}


# 2026-09-22 弃牌目标函数修复（§3.4）后，baohua_ticket 罚分被收在
# "meld_groups == 0 and pair_route_allowed(...)" 门里（需要 >=5 个真实对子
# 且 pair_shanten 严格快于 shanten）。原 PAIR_HAND 只有 13 张（缺一张，实际
# 少算了一张牌），且都是数牌——拆开看两两相邻的三组数牌（1w/2w/3w 等）能被
# 标准路线直接吃成顺子，标准向听反而和七对打平（都是 0），不满足"严格更快"，
# 门禁下豪华门票不再触发。换成 5 对字牌（无法组成顺子，堵死标准路线的顺子
# 优化空间）+ 一组三张字牌（豪华门票保护对象）+ 一张孤张，凑够真实 14 张
# 弃前手牌，讨论 pair_shanten(0) 严格快于 shanten(7)。见 docs/refactor/VALIDATION.md。
PAIR_HAND = ["东", "东", "南", "南", "西", "西", "北", "北", "中", "中", "发", "发", "发", "1w"]
# 弱对形: 3 对 + 三张 4t + 散张 (progress 8 < 10, 不走七对门票)
WEAK_HAND = ["1w", "1w", "2w", "2w", "3w", "3w", "4t", "4t", "4t", "5t", "6t", "7t", "8b"]


class BaohuaTicketTests(unittest.TestCase):
    def test_strategy_path_penalizes_breaking_triplet(self):
        base = _discard_score(PAIR_HAND, "发", 0, weights=w(0))
        with_ticket = _discard_score(PAIR_HAND, "发", 0, weights=w(1000))
        self.assertAlmostEqual(with_ticket, base - 1000, places=6)

    def test_strategy_path_no_penalty_for_pair_break(self):
        # 拆对子(1w)不触发豪华门票罚分（票还在：三张 发 未动）
        base = _discard_score(PAIR_HAND, "1w", 0, weights=w(0))
        with_ticket = _discard_score(PAIR_HAND, "1w", 0, weights=w(1000))
        self.assertAlmostEqual(with_ticket, base, places=6)

    def test_strategy_path_no_ticket_below_five_pairs(self):
        # <5 对不走七对: 三张同种无门票价值, 罚分不触发
        base = _discard_score(WEAK_HAND, "4t", 0, weights=w(0))
        with_ticket = _discard_score(WEAK_HAND, "4t", 0, weights=w(1000))
        self.assertAlmostEqual(with_ticket, base, places=6)

    def test_route_path_penalizes_breaking_triplet(self):
        base = route_value(PAIR_HAND, "发", 0, weights=w(0))
        with_ticket = route_value(PAIR_HAND, "发", 0, weights=w(1000))
        self.assertAlmostEqual(with_ticket, base - 1000, places=6)

    def test_route_path_no_ticket_below_five_pairs(self):
        base = route_value(WEAK_HAND, "4t", 0, weights=w(0))
        with_ticket = route_value(WEAK_HAND, "4t", 0, weights=w(1000))
        self.assertAlmostEqual(with_ticket, base, places=6)


if __name__ == "__main__":
    unittest.main()
