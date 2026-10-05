"""DecisionState 契约测试：覆盖评审报告已确认的字段错读与门控缺陷。

这些测试在阶段1修复前会失败（如果直接对着旧 `bot.py` 顶层 chain_count 读取跑），
用于证明 bug 存在，并在修复后作为回归锁定。
"""
import unittest

from mj.state import (
    canonical_chain_count,
    canonical_piao,
    meld_count,
    melds_for_seat,
    normalize,
)


class ChainCountFieldTests(unittest.TestCase):
    def test_prefers_god_chain_count_over_top_level(self):
        # 官网规范字段是 god.chain_count；顶层 chain_count 历史上从不由服务端写入。
        # 若两者都存在且不同，必须以 god.chain_count 为准。
        snapshot = {"god": {"chain_count": 3}, "chain_count": 0}
        self.assertEqual(canonical_chain_count(snapshot), 3)

    def test_falls_back_to_top_level_when_god_missing_key(self):
        # 兼容旧测试夹具：god 存在但没有 chain_count 键时，允许回退顶层字段。
        snapshot = {"god": {"catch_play": False}, "chain_count": 2}
        self.assertEqual(canonical_chain_count(snapshot), 2)

    def test_zero_when_neither_present(self):
        self.assertEqual(canonical_chain_count({}), 0)

    def test_piao_prefers_god_piao(self):
        snapshot = {"god": {"piao": 2}, "piao": 0}
        self.assertEqual(canonical_piao(snapshot), 2)


class MeldHelpersTests(unittest.TestCase):
    def test_melds_for_seat_list_form(self):
        melds = [[], [{"kind": "peng"}], [], []]
        self.assertEqual(meld_count({"melds": melds}, 1), 1)

    def test_melds_for_seat_dict_form(self):
        melds = {"1": [{"kind": "chi"}, {"kind": "chi"}]}
        self.assertEqual(meld_count({"melds": melds}, 1), 2)

    def test_melds_for_seat_missing_returns_empty(self):
        self.assertEqual(melds_for_seat({"melds": []}, 0), [])


class NormalizeTests(unittest.TestCase):
    def test_normalize_reads_canonical_chain_and_piao(self):
        snapshot = {
            "seat": 0, "phase": "draw", "turn": 0,
            "my_hand": ["1w", "2w"], "drawn_tile": "3w",
            "dealer": 0, "wall_remaining": 40,
            "god": {"chain_count": 2, "piao": 1, "catch_play": True, "god_discarder_seat": 2},
            "melds": [[], [], [], []],
        }
        state = normalize(snapshot)
        self.assertEqual(state.chain_count, 2)
        self.assertEqual(state.piao, 1)
        self.assertTrue(state.catch_play)
        self.assertTrue(state.is_dealer())
        # god_discarder_seat=2 != seat=0 → 抓打圈限制生效
        self.assertTrue(state.catch_restricted())

    def test_normalize_empty_snapshot_returns_none(self):
        self.assertIsNone(normalize({}))
        self.assertIsNone(normalize(None))

    def test_wall_tail_threshold_is_20_for_all_gang_paths(self):
        # 20 张墙尾（含）以内应统一视为墙尾：<=20 时 wall_tail 为真
        state = normalize({"seat": 0, "wall_remaining": 20, "melds": [[], [], [], []]})
        self.assertTrue(state.wall_tail())
        state21 = normalize({"seat": 0, "wall_remaining": 21, "melds": [[], [], [], []]})
        self.assertFalse(state21.wall_tail())


if __name__ == "__main__":
    unittest.main()
