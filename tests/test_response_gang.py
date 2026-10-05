"""离线协议对齐热修需求三：response_peng 窗口支持合法明杠——最小响应
明杠门禁（response_gang_take），明杠优先于原有 peng/pass，且不满足门禁
条件时原 peng/pass 行为不变。"""
import unittest

from mj.bot import choose_action
from mj.responses import response_gang_take


# 2026-09-22 弃牌目标函数修复（§3.4）后，claim_assessment 的门禁判据改用
# pair_route_allowed（需要 >=5 个真实对子且严格更快），不再是旧的
# "pair_shanten(before_counts) <= standard_before"。旧的 3/4 张单薄 fixture
# 手牌下，任何一手带对子的手在旧公式里都近似恒真（pair_shanten 起点是 6，
# 比 shanten 的起点 8 低），使这些用例"意外地"总是落在 route_shanten_worsens
# 分支——不是因为真的在保护七对，只是公式在极小手牌上的副作用。新门禁下这个
# 副作用消失，所以这里换成真实 13 张的门清手，claim_assessment 对其给出的
# route_shanten_worsens 判定与七对门禁完全无关（可用 mj.shanten.pair_route_allowed
# 核验：这些手牌本身也不满足新门槛）。verified via tools 脚本，见
# docs/refactor/VALIDATION.md。
_BASE_HAND = ["1t", "1t", "3t", "3t", "5b", "5b", "5w", "5w", "7w", "9b", "9b", "9b", "中"]
_TWO_9B_HAND = ["4t", "5b", "5t", "5t", "6b", "6b", "6t", "7w", "7w", "9b", "9b", "9t", "9t"]
_TWO_5W_HAND = ["1b", "1b", "1w", "1w", "5w", "5w", "6b", "6b", "8t", "8w", "9t", "9t", "9w"]


def _base_snapshot(**overrides):
    snapshot = {
        "phase": "response_peng", "seat": 0, "responding_seats": [0],
        "window_tile": "9b", "my_hand": _BASE_HAND,
        "melds": [[], [], [], []], "wall_remaining": 50,
        "god": {"catch_play": False},
    }
    snapshot.update(overrides)
    return snapshot


class ResponseGangGateTests(unittest.TestCase):
    def test_legal_open_gang_takes_priority_over_peng(self):
        """合法明杠：满足全部门禁条件时，choose_action 提交 gang，且优先
        于 peng（同一 window_tile 本也满足 peng 条件，即 gang 优先级更高）。"""
        snapshot = _base_snapshot()
        self.assertEqual(response_gang_take(snapshot), {"action": "gang", "tile": "9b"})
        self.assertEqual(choose_action(snapshot), {"action": "gang", "tile": "9b"})

    def test_joker_forbidden(self):
        """财神禁止：window_tile 是财神时，无论手牌数量，一律不提交
        gang（也不提交 peng，因为財神不能被碰——回到 pass）。"""
        snapshot = _base_snapshot(window_tile="白", my_hand=["白", "白", "白", "1w"])
        self.assertIsNone(response_gang_take(snapshot))
        self.assertEqual(choose_action(snapshot), {"action": "pass", "tile": ""})

    def test_wall_tail_forbidden_does_not_bypass_peng_gate(self):
        snapshot = _base_snapshot(wall_remaining=20)
        self.assertIsNone(response_gang_take(snapshot))
        self.assertEqual(choose_action(snapshot), {"action": "pass", "tile": ""})

        # wall_remaining=21（刚好>20）时明杠应放行，作为边界对照。
        snapshot_ok = _base_snapshot(wall_remaining=21)
        self.assertEqual(response_gang_take(snapshot_ok), {"action": "gang", "tile": "9b"})

    def test_catch_restricted_responder_forbidden_falls_back_to_pass(self):
        """抓打圈禁止：响应者自身是受限方（god.catch_play 且
        god_discarder_seat != 己方座位）时不提交 gang；且 choose_action()
        在 response_peng 窗口必须直接返回 pass（不得继续调用
        response_gang_take/choose_peng 落到 peng——受限方在抓打圈内没有
        任何合法响应权，即使手持合法搭子）。"""
        snapshot = _base_snapshot(god={"catch_play": True, "god_discarder_seat": 1})
        self.assertIsNone(response_gang_take(snapshot))
        self.assertEqual(choose_action(snapshot), {"action": "pass", "tile": ""})

        # 出牌者本人打财神触发抓打圈时，god_discarder_seat==己方座位，
        # 说明自己就是打财神者——不受抓打圈限制，仍可正常判定明杠。
        snapshot_exempt = _base_snapshot(god={"catch_play": True, "god_discarder_seat": 0})
        self.assertEqual(response_gang_take(snapshot_exempt), {"action": "gang", "tile": "9b"})
        self.assertEqual(choose_action(snapshot_exempt), {"action": "gang", "tile": "9b"})

    def test_insufficient_tile_count_respects_peng_gate(self):
        snapshot = _base_snapshot(my_hand=_TWO_9B_HAND)
        self.assertIsNone(response_gang_take(snapshot))
        self.assertEqual(choose_action(snapshot), {"action": "pass", "tile": ""})

    def test_unmet_gang_gate_respects_peng_or_pass_behavior(self):
        snapshot = _base_snapshot(window_tile="5w", my_hand=_TWO_5W_HAND)
        self.assertIsNone(response_gang_take(snapshot))
        self.assertEqual(choose_action(snapshot), {"action": "pass", "tile": ""})

        # 完全无法声明的场景：既不能 gang 也不能 peng，回落 pass，行为
        # 与修复前一致。
        no_claim_snapshot = _base_snapshot(window_tile="5w", my_hand=["1w"])
        self.assertIsNone(response_gang_take(no_claim_snapshot))
        self.assertEqual(choose_action(no_claim_snapshot), {"action": "pass", "tile": ""})

    def test_wrong_phase_or_not_responding_seat_never_gangs(self):
        """非 response_peng 阶段，或己方座位不在 responding_seats 中时，
        response_gang_take 一律返回 None。"""
        wrong_phase = _base_snapshot(phase="response_chi")
        self.assertIsNone(response_gang_take(wrong_phase))

        not_responding = _base_snapshot(responding_seats=[1, 2])
        self.assertIsNone(response_gang_take(not_responding))

    def test_catch_restricted_response_chi_forbidden_falls_back_to_pass(self):
        """抓打圈受限方 response_chi：即使持有合法搭子，choose_action() 也
        必须直接返回 pass，不得继续调用 choose_chi。"""
        snapshot = {
            "phase": "response_chi", "seat": 0, "responding_seats": [0],
            "window_tile": "5w", "my_hand": ["4w", "6w"], "melds": [[], [], [], []],
            "wall_remaining": 50, "god": {"catch_play": True, "god_discarder_seat": 1},
        }
        self.assertEqual(choose_action(snapshot), {"action": "pass", "tile": ""})

    def test_no_catch_play_still_applies_claim_safety_gate(self):
        peng_snapshot = _base_snapshot(god={"catch_play": False})
        self.assertEqual(choose_action(peng_snapshot), {"action": "gang", "tile": "9b"})

        peng_only_snapshot = _base_snapshot(my_hand=_TWO_9B_HAND, god={"catch_play": False})
        self.assertEqual(choose_action(peng_only_snapshot), {"action": "pass", "tile": ""})

        chi_snapshot = {
            "phase": "response_chi", "seat": 0, "responding_seats": [0],
            "window_tile": "5w", "my_hand": ["4w", "6w"], "melds": [[], [], [], []],
            "wall_remaining": 50, "god": {"catch_play": False},
        }
        self.assertEqual(choose_action(chi_snapshot), {"action": "pass", "tile": ""})


if __name__ == "__main__":
    unittest.main()
