import json
import os
import unittest

from mj.bot import choose_action
from mj.responses import choose_chi, choose_gang, choose_peng, claim_assessment

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GANG_FIXTURE_PATH = os.path.join(
    REPO_ROOT, "data", "fixtures", "protocol", "gang_hand_count_semantics.json")


def _load_gang_fixture():
    with open(_GANG_FIXTURE_PATH, encoding="utf-8") as handle:
        return json.load(handle)


class ResponseTests(unittest.TestCase):
    def test_chi_limit_two(self):
        snapshot = {"window_tile": "3w", "my_hand": ["1w", "2w"], "seat": 0,
                    "melds": [[{"kind": "chi"}, {"kind": "chi"}], [], [], []]}
        self.assertIsNone(choose_chi(snapshot))

        snapshot = {"window_tile": "白", "my_hand": ["白", "白", "1w"]}
        self.assertIsNone(choose_peng(snapshot))
        self.assertIsNone(choose_chi(snapshot))

    def test_gang_forbidden_near_wall(self):
        snapshot = {"drawn_tile": "5w", "my_hand": ["5w", "5w", "5w", "5w"], "wall_remaining": 20}
        self.assertIsNone(choose_gang(snapshot))

        # 2026-09-22 弃牌目标函数修复（§3.4）后，claim_assessment 的门禁判据
        # 改用 pair_route_allowed（>=5 个真实对子且严格更快）。旧的 3 张单薄
        # fixture（["5w","5w","1w"]）能被拒绝，只是旧公式
        # "pair_shanten(before_counts) <= standard_before" 在极小手牌上近乎
        # 恒真的副作用，不是真的在保护七对。换成真实 13 张门清手，
        # claim_assessment 给出的 route_shanten_worsens 与七对门禁无关（该
        # 手牌本身也不满足新门槛）。见 docs/refactor/VALIDATION.md。
        snapshot = {"window_tile": "5w",
                    "my_hand": ["1b", "1b", "1w", "1w", "5w", "5w", "6b", "6b",
                                "8t", "8w", "9t", "9t", "9w"]}
        self.assertIsNone(choose_peng(snapshot))

    def test_incomplete_hand_never_bypasses_successor_simulation(self):
        # 同上：换成真实 13 张手牌，避免旧公式在极小手牌上的副作用掩盖了
        # 本测试真正要验证的性质（后继模拟止损，与七对门禁无关）。
        snapshot = {"window_tile": "3w",
                    "my_hand": ["1t", "1t", "1w", "2w", "3b", "3b", "3t", "3t",
                                "5w", "7w", "7w", "8w", "8w"]}
        self.assertIsNone(choose_chi(snapshot))

    def test_gang_from_four_tiles(self):
        snapshot = {"drawn_tile": "5w", "my_hand": ["5w", "5w", "5w", "5w"]}
        self.assertEqual(choose_gang(snapshot)["action"], "gang")

        snapshot = {"window_tile": "东", "my_hand": ["东", "东"]}
        self.assertIsNone(choose_chi(snapshot))

    def test_pair_route_hand_first_claim_rejected(self):
        snapshot = {"window_tile": "9w", "my_hand": ["1w", "1w", "3w", "3w", "5w", "5w",
                                                    "7w", "7w", "9w", "9w", "2t", "4t", "6t"],
                    "melds": [[], [], [], []]}
        self.assertIsNone(choose_peng(snapshot))

    def test_non_improving_first_claim_rejected(self):
        hand = ["1w", "1w", "3w", "3w", "5w", "5w", "7w", "7w", "9w", "9w", "2t", "3t", "4t"]
        rivers = [["2t", "2t", "2t", "3t", "3t", "3t"], [], [], []]
        snapshot = {"window_tile": "1t", "my_hand": hand, "discards": rivers,
                    "melds": [[], [], [], []]}
        self.assertIsNone(choose_chi(snapshot))

    def test_dealer_does_not_change_claim_gate(self):
        hand = ["1w", "1w", "2w", "3w", "5w", "6w", "7w", "2t", "3t", "4t", "6t", "7t", "8t"]
        base = {"window_tile": "5w", "my_hand": hand, "melds": [[], [], [], []],
                "discards": [[], [], [], []]}
        self.assertEqual(choose_chi(dict(base, dealer=2)), choose_chi(dict(base, dealer=0)))


class GangHandCountFixtureTests(unittest.TestCase):
    """P0 修复回归：暗杠牌数语义（my_hand 含 drawn_tile 本身，需要
    hand.count(drawn)>=4 才允许暗杠，不是 >=3）。fixture 来自真实拒绝
    案例脱敏抽取 + 合成正反例，见
    data/fixtures/protocol/gang_hand_count_semantics.json。"""

    def test_all_choose_gang_cases(self):
        fixture = _load_gang_fixture()
        for case in fixture["cases"]:
            if "expected_choose_gang" not in case:
                continue
            with self.subTest(case=case["id"]):
                result = choose_gang(case["snapshot"])
                self.assertEqual(result, case["expected_choose_gang"], msg=case["description"])

    def test_catch_restricted_choose_action_cases(self):
        fixture = _load_gang_fixture()
        for case_id in ("synthetic_catch_restricted_count3_forbids_gang_via_choose_action",
                        "synthetic_catch_restricted_count4_allows_gang_via_choose_action"):
            with self.subTest(case=case_id):
                case = next(c for c in fixture["cases"] if c["id"] == case_id)
                result = choose_action(case["snapshot"])
                self.assertEqual(result, case["expected_choose_action"], msg=case["description"])

    def test_real_reject_case_count3_no_melds_rejects(self):
        fixture = _load_gang_fixture()
        case = next(c for c in fixture["cases"] if c["id"] == "real_reject_count3_no_melds")
        snapshot = case["snapshot"]
        hand = snapshot["my_hand"]
        drawn = snapshot["drawn_tile"]
        self.assertEqual(hand.count(drawn), 3)
        self.assertIsNone(choose_gang(snapshot))

    def test_synthetic_count4_case_allows_gang(self):
        fixture = _load_gang_fixture()
        case = next(c for c in fixture["cases"] if c["id"] == "synthetic_count4_allows_gang")
        snapshot = case["snapshot"]
        hand = snapshot["my_hand"]
        drawn = snapshot["drawn_tile"]
        self.assertEqual(hand.count(drawn), 4)
        self.assertEqual(choose_gang(snapshot), {"action": "gang", "tile": drawn})


class TenpaiPengGangRelaxSwitchTests(unittest.TestCase):
    """rule_tenpai_peng_gang_relax_enabled（默认关闭）：听牌态碰/杠响应放宽
    门槛（不再要求严格加宽听口，只要求不变窄）。来源、Δ、CI、覆盖率见
    mj/fit.py::DEFAULT_WEIGHTS 与 mj/responses.py::claim_assessment 里本键
    旁的注释，完整数字见 docs/experiments/OFFLINE_REPORT.md「五问结论」S2。

    夹具：三组顺子(1-9w) + 1b1b1b 暗刻 + 浮张 3t，听 3t 单钓。碰一张外部
    1b（把暗刻当成对子来碰，只为在单测里构造"中性碰"——不代表真实合法
    副露形态，只用于钉住 claim_assessment 的听口边界判据）：听口碰前碰后
    都是 2 张（3t），中性、不加宽。"""

    HAND = ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w", "1b", "1b", "1b", "3t"]
    SNAPSHOT = {"my_hand": HAND, "melds": [[], [], [], []], "seat": 0}
    TAKE = ("1b", "1b")

    def _assessment(self, weights=None):
        return claim_assessment(dict(self.SNAPSHOT), self.TAKE, weights=weights)

    def test_default_off_rejects_neutral_tenpai_peng(self):
        result = self._assessment(weights={"rule_tenpai_peng_gang_relax_enabled": 0})
        self.assertEqual(result["before_ukeire"], result["after_ukeire"])
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason"], "tenpai_claim_no_gain")

    def test_enabled_allows_neutral_tenpai_peng(self):
        result = self._assessment(weights={"rule_tenpai_peng_gang_relax_enabled": 1})
        self.assertTrue(result["allowed"])
        self.assertEqual(result["reason"], "tenpai_claim_peng_gang_relaxed")

    def test_enabled_does_not_relax_chi(self):
        """同一开关打开时，chi（take 是两张不同牌）的听牌态门槛必须原样
        保留——本次 S2 发现的缺口只在 peng/gang，不在 chi。"""
        hand = ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w",
               "1b", "2b", "4b", "3t"]
        snapshot = {"my_hand": hand, "melds": [[], [], [], []], "seat": 0}
        take = ("1b", "2b")   # 吃 3b 组成 1b2b3b，两张不同牌
        default_result = claim_assessment(dict(snapshot), take)
        relaxed_result = claim_assessment(dict(snapshot), take,
                                          weights={"rule_tenpai_peng_gang_relax_enabled": 1})
        self.assertEqual(default_result["allowed"], relaxed_result["allowed"])
        self.assertEqual(default_result["reason"], relaxed_result["reason"])

    def test_default_weight_is_zero(self):
        from mj.fit import DEFAULT_WEIGHTS
        self.assertEqual(DEFAULT_WEIGHTS["rule_tenpai_peng_gang_relax_enabled"], 0)


class RejectNeutralChiSwitchTests(unittest.TestCase):
    """rule_reject_neutral_chi_enabled（默认关闭）：非听牌态、向听不变的
    chi 一律 pass（碰/杠不受影响）。来源、Δ、CI、覆盖率见
    mj/fit.py::DEFAULT_WEIGHTS 与 mj/responses.py::claim_assessment 里本键
    旁的注释，完整数字见 docs/experiments/OFFLINE_REPORT.md「五问结论」S6。

    四个夹具均已用脚本核实过 claim_assessment 的真实返回值（不是手推）：
    - 首副露(meld_groups=0)的中性吃：4 张手牌 4w6w 吃成 4w5w6w，向听 4→4
      不变，且没有有效七对路线（pair_route_allowed=False），旧判据走
      first_meld_neutral_no_pair_route 分支放行。
    - 已有副露(meld_groups=1)的中性吃：向听 2→2 不变，旧判据走
      non_worsening_open_hand 分支放行。
    - 中性碰（同样向听不变、无七对路线）：开关只应该管 chi，不应该动它。
    - 能改善向听的吃（向听 4→3）：开关不应该拒绝真正有用的吃。
    """

    NEUTRAL_CHI_FIRST_MELD = {
        "hand": ["南", "4b", "2w", "8t", "北", "8b", "5b", "1t", "北", "8w", "西", "4w", "6w"],
        "melds": [[], [], [], []], "take": ("4w", "6w"),
    }
    NEUTRAL_CHI_HAS_MELD = {
        "hand": ["9w", "1t", "5w", "1w", "5t", "9w", "4b", "白", "4w", "6w"],
        "melds": [[{"kind": "chi", "tiles": ["1w", "2w", "3w"]}], [], [], []],
        "take": ("4w", "6w"),
    }
    NEUTRAL_PENG = {
        "hand": ["3t", "白", "4w", "1t", "5t", "7t", "3b", "7w", "3w", "南", "2b", "9w", "9w"],
        "melds": [[], [], [], []], "take": ("9w", "9w"),
    }
    IMPROVING_CHI = {
        "hand": ["4w", "6w", "1b", "3b", "5b", "7b", "1t", "3t", "5t", "7t", "东", "南", "西"],
        "melds": [[], [], [], []], "take": ("4w", "6w"),
    }

    def _assess(self, fixture, weights=None):
        snapshot = {"my_hand": fixture["hand"], "melds": fixture["melds"], "seat": 0}
        return claim_assessment(dict(snapshot), fixture["take"], weights=weights)

    def test_default_off_allows_neutral_chi_first_meld(self):
        result = self._assess(self.NEUTRAL_CHI_FIRST_MELD, weights={"rule_reject_neutral_chi_enabled": 0})
        self.assertEqual(result["before_shanten"], result["after_shanten"])
        self.assertTrue(result["allowed"])
        self.assertEqual(result["reason"], "first_meld_neutral_no_pair_route")

    def test_default_off_allows_neutral_chi_has_meld(self):
        result = self._assess(self.NEUTRAL_CHI_HAS_MELD, weights={"rule_reject_neutral_chi_enabled": 0})
        self.assertEqual(result["before_shanten"], result["after_shanten"])
        self.assertTrue(result["allowed"])
        self.assertEqual(result["reason"], "non_worsening_open_hand")

    def test_enabled_rejects_neutral_chi_first_meld(self):
        result = self._assess(self.NEUTRAL_CHI_FIRST_MELD, weights={"rule_reject_neutral_chi_enabled": 1})
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason"], "neutral_chi_rejected")

    def test_enabled_rejects_neutral_chi_has_meld(self):
        result = self._assess(self.NEUTRAL_CHI_HAS_MELD, weights={"rule_reject_neutral_chi_enabled": 1})
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason"], "neutral_chi_rejected")

    def test_enabled_does_not_touch_neutral_peng(self):
        """碰不动：take 是两张同种牌，无论开关状态结果必须完全一样。"""
        off = self._assess(self.NEUTRAL_PENG, weights={"rule_reject_neutral_chi_enabled": 0})
        on = self._assess(self.NEUTRAL_PENG, weights={"rule_reject_neutral_chi_enabled": 1})
        self.assertEqual(off, on)
        self.assertTrue(on["allowed"])

    def test_enabled_still_allows_improving_chi(self):
        """开关只挡"向听不变"的吃，能真正改善向听的吃必须继续放行。"""
        off = self._assess(self.IMPROVING_CHI, weights={"rule_reject_neutral_chi_enabled": 0})
        on = self._assess(self.IMPROVING_CHI, weights={"rule_reject_neutral_chi_enabled": 1})
        self.assertEqual(off, on)
        self.assertTrue(on["allowed"])
        self.assertEqual(on["reason"], "first_meld_improves")

    def test_default_weight_is_zero(self):
        from mj.fit import DEFAULT_WEIGHTS
        self.assertEqual(DEFAULT_WEIGHTS["rule_reject_neutral_chi_enabled"], 0)
