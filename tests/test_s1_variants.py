"""S1 放宽变体 E1/E2/E3：默认关闭时行为不变（对照冻结的旧实现），打开时各自生效。"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MJ_WEIGHTS_NO_FILE", "1")

from mj import hu_strategy as H  # noqa: E402
from mj import s1_variants as V  # noqa: E402
from mj.fit import weights_overlay  # noqa: E402
from mj.hu_strategy import _decline_discard_choice, choose_hu_or_piao  # noqa: E402
from mj.rules import evaluate  # noqa: E402
from mj.tiles import JOKER, TILE_INDEX, to_counts  # noqa: E402

E_OFF = {"s1_wall_relax_enabled": 0, "s1_one_step_enabled": 0, "s1_ev_enabled": 0}


def _old_decline(snapshot, hu_result, weights):
    """改动前的 ``_decline_small_hu_tile``（冻结副本，只含 S1 触发逻辑；墙尾门槛是字面量 8）。"""
    if not weights.get("rule_decline_joker_hold_enabled", 0):
        return None
    if not weights.get("s1_dealer_enabled", 1) and snapshot.get("dealer") == snapshot.get("seat"):
        return None
    if hu_result.get("baotou"):
        return None
    counts = hu_result.get("counts")
    if not counts:
        return None
    effective_jokers = counts[TILE_INDEX[JOKER]]
    meld_groups = H._meld_groups(snapshot)
    single_all = weights.get("rule_decline_single_joker_all_melds_enabled", 0)
    one_meld = weights.get("rule_decline_single_joker_one_meld_enabled", 1)
    if effective_jokers == 1 and meld_groups == 1:
        triggers = bool(one_meld)
    else:
        triggers = effective_jokers >= 2 or (effective_jokers == 1 and single_all)
    if not triggers:
        return None
    if snapshot.get("wall_remaining", 40) - 20 < 8:
        return None
    return H._decline_discard_choice(snapshot, hu_result)


def _case(hand, drawn, melds=0, wall=60, dealer=1, real=False):
    """real=False：沿用 test_hu_strategy 的做法手工给"非爆头、财神数 n"的 hu_result（这手牌去掉摸到的牌本来就是爆头形，
    evaluate 会判爆头）；real=True：用 evaluate 的真实结果（E2 的随机找到的手牌是真的非爆头胡）。"""
    rest = list(hand)
    rest.remove(drawn)
    if real:
        res = evaluate(to_counts(rest), TILE_INDEX[drawn], chain_count=0, piao=0, meld_groups=melds)
    else:
        res = {"hu": True, "baotou": False, "fan": 1, "counts": tuple([0] * 33 + [hand.count(JOKER)])}
    snap = {"drawn_tile": drawn, "wall_remaining": wall, "my_hand": hand, "seat": 0, "dealer": dealer,
            "melds": [[{"kind": "peng", "tiles": ["9t"] * 3}] * melds, [], [], []], "discards": [[], [], [], []]}
    return snap, res


H2J0 = ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w", "1b", "1b", "白", "白", "东"]       # 直接转爆头，2 财神 0 副露
H1J0 = ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w", "1b", "1b", "1b", "白", "东"]       # 直接转爆头，1 财神 0 副露
ONE_STEP = [(["2t", "3w", "2b", "1w", "9b", "4b", "1w", "4w", "3t", "8b", "3b", "1t", "白", "白"], "8b", "1w"),
            (["3b", "4b", "7b", "7b", "北", "9b", "2b", "7b", "8b", "2b", "3b", "北", "白", "白"], "北", "7b")]


class DefaultsUnchanged(unittest.TestCase):
    def test_grid_equals_frozen_old_implementation(self):
        cases = [(H2J0, "东", 0, False), (H1J0, "东", 0, False)] + [(h, d, 0, True) for h, d, _ in ONE_STEP]
        n_decline = 0
        for hand, drawn, melds, real in cases:
            for wall in (20, 24, 27, 28, 29, 31, 40, 60, 100):
                for dealer in (0, 1):
                    snap, res = _case(hand, drawn, melds, wall, dealer, real)
                    for base in ({"rule_decline_joker_hold_enabled": 1},
                                 {"rule_decline_joker_hold_enabled": 1, "rule_decline_single_joker_all_melds_enabled": 1},
                                 {"rule_decline_joker_hold_enabled": 0},
                                 {"rule_decline_joker_hold_enabled": 1, "s1_dealer_enabled": 0}):
                        want = _old_decline(snap, res, base)
                        n_decline += want is not None
                        self.assertEqual(H._decline_small_hu_tile(snap, res, dict(base)), want)
                        self.assertEqual(H._decline_small_hu_tile(snap, res, {**base, **E_OFF}), want)
        self.assertGreater(n_decline, 10)

    def test_choose_hu_or_piao_same_with_explicit_off(self):
        for hand, drawn, melds in ((H2J0, "东", 0), (H1J0, "东", 0)):
            for wall in (24, 27, 28, 40, 60):
                snap, res = _case(hand, drawn, melds, wall, 1)
                with weights_overlay({"rule_decline_joker_hold_enabled": 1}):
                    a = choose_hu_or_piao(snap, res)
                with weights_overlay({"rule_decline_joker_hold_enabled": 1, **E_OFF}):
                    self.assertEqual(choose_hu_or_piao(snap, res), a)


class E1Wall(unittest.TestCase):
    def test_wall_relax(self):
        snap, res = _case(H2J0, "东", 0, wall=26)          # 墙剩余-20 = 6：现行 <8 不弃
        base = {"rule_decline_joker_hold_enabled": 1}
        self.assertIsNone(H._decline_small_hu_tile(snap, res, base))
        self.assertEqual(H._decline_small_hu_tile(snap, res, {**base, "s1_wall_relax_enabled": 1}), "东")
        snap3, res3 = _case(H2J0, "东", 0, wall=23)         # 3 < 4：放宽后也不弃
        self.assertIsNone(H._decline_small_hu_tile(snap3, res3, {**base, "s1_wall_relax_enabled": 1}))
        self.assertIsNone(H._decline_small_hu_tile(snap, res, {**base, "s1_wall_relax_enabled": 1, "s1_wall_relax_min": 8}))
        self.assertEqual(V.wall_min({}), 8)
        self.assertEqual(V.wall_min({"s1_wall_relax_enabled": 1}), 4)

    def test_integration(self):
        snap, res = _case(H2J0, "东", 0, wall=26)
        with weights_overlay({"rule_decline_joker_hold_enabled": 1}):
            self.assertEqual(choose_hu_or_piao(snap, res), {"action": "hu", "tile": "东"})
        with weights_overlay({"rule_decline_joker_hold_enabled": 1, "s1_wall_relax_enabled": 1}):
            self.assertEqual(choose_hu_or_piao(snap, res), {"action": "discard", "tile": "东"})


class E2OneStep(unittest.TestCase):
    BASE = {"rule_decline_joker_hold_enabled": 1}

    def test_one_step_triggers_when_no_direct_conversion(self):
        for hand, drawn, tile in ONE_STEP:
            snap, res = _case(hand, drawn, 0, 60, 1, real=True)
            self.assertIsNone(_decline_discard_choice(snap, res))
            self.assertIsNone(H._decline_small_hu_tile(snap, res, dict(self.BASE)))
            got = H._decline_small_hu_tile(snap, res, {**self.BASE, "s1_one_step_enabled": 1})
            self.assertEqual(got, tile)
            self.assertIsNone(H._decline_small_hu_tile(snap, res, {**self.BASE, "s1_one_step_enabled": 1,
                                                                  "s1_one_step_min_ukeire": 999}))

    def test_chosen_tile_is_really_one_step_away(self):
        for hand, drawn, tile in ONE_STEP:
            snap, res = _case(hand, drawn, 0, 60, 1, real=True)
            left = list(hand)
            left.remove(tile)
            c = to_counts(left)
            vis = V._unseen(snap)
            live = V.one_step_ukeire(c, 0, vis)
            self.assertIsNotNone(live)
            self.assertGreaterEqual(live, 8)

    def test_does_not_override_direct_conversion_or_single_joker(self):
        snap, res = _case(H2J0, "东", 0, 60, 1)
        w = {**self.BASE, "s1_one_step_enabled": 1}
        self.assertEqual(H._decline_small_hu_tile(snap, res, w), H._decline_small_hu_tile(snap, res, dict(self.BASE)))
        hand, drawn, _ = ONE_STEP[0]
        snap1, res1 = _case(hand, drawn, 0, 60, 1, real=True)
        res1 = dict(res1, counts=tuple([0] * 33 + [1]))           # 假装只有 1 张有效财神
        self.assertIsNone(V.one_step_choice(snap1, res1, w, 0))


def _model_file(g_wins, g_n, L_dealer=10.0, L_non=5.0, ratio=2.0):
    obj = {"version": 1, "shrink_k": 10.0, "levels": {"g": [g_wins, g_n]}, "L": {"dealer": L_dealer, "non": L_non},
           "ratio": ratio}
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(obj, f)
    f.close()
    return f.name


class E3Expected(unittest.TestCase):
    BASE = {"rule_decline_joker_hold_enabled": 1, "s1_ev_enabled": 1}

    def _run(self, path, snap, res, **extra):
        with patch.dict(os.environ, {"MJ_S1_PHAT_PATH": path}):
            return H._decline_small_hu_tile(snap, res, {**self.BASE, **extra})

    def test_high_phat_declines_low_does_not_and_covers_table_excluded_cells(self):
        high, low = _model_file(95, 100), _model_file(30, 100)
        self.addCleanup(os.unlink, high)
        self.addCleanup(os.unlink, low)
        snap, res = _case(H1J0, "东", 0, 60, 0)      # 1 财神 0 副露：现行格子表排除（single_all 关）
        self.assertIsNone(_old_decline(snap, res, {"rule_decline_joker_hold_enabled": 1}))
        self.assertEqual(self._run(high, snap, res), "东")
        self.assertIsNone(self._run(low, snap, res))

    def test_margin_and_dealer_specific_pstar(self):
        # p̂≈0.7：闲家 p*=(10+5)/(20+5)=0.6 -> 0.7>0.7? 取 margin 0.05 弃；庄家 L=40 -> p*=(24+40)/(48+40)=0.727 -> 不弃
        path = _model_file(70, 100, L_dealer=40.0, L_non=5.0)
        self.addCleanup(os.unlink, path)
        non, res_n = _case(H2J0, "东", 0, 60, 1)
        dlr, res_d = _case(H2J0, "东", 0, 60, 0)
        self.assertEqual(self._run(path, non, res_n, s1_ev_margin=0.05), "东")
        self.assertIsNone(self._run(path, dlr, res_d, s1_ev_margin=0.05))
        self.assertIsNone(self._run(path, non, res_n, s1_ev_margin=0.2))

    def test_missing_or_broken_model_and_master_switch(self):
        snap, res = _case(H2J0, "东", 0, 60, 1)
        self.assertIsNone(self._run("/nonexistent/s1.json", snap, res))
        bad = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        bad.write("{oops")
        bad.close()
        self.addCleanup(os.unlink, bad.name)
        self.assertIsNone(self._run(bad.name, snap, res))
        ok = _model_file(95, 100)
        self.addCleanup(os.unlink, ok)
        with patch.dict(os.environ, {"MJ_S1_PHAT_PATH": ok}):
            self.assertIsNone(H._decline_small_hu_tile(snap, res, {"s1_ev_enabled": 1}))   # S1 总开关没开

    def test_wall_gate_and_no_direct_conversion(self):
        path = _model_file(95, 100)
        self.addCleanup(os.unlink, path)
        snap, res = _case(H2J0, "东", 0, 23, 1)
        self.assertIsNone(self._run(path, snap, res))          # 墙剩余-20=3：没有再摸一次的机会
        hand, drawn, _ = ONE_STEP[0]
        snap2, res2 = _case(hand, drawn, 0, 60, 1, real=True)
        self.assertIsNone(self._run(path, snap2, res2))        # 候选必须能直接转爆头

    def test_phat_model_shrinkage(self):
        m = V.PhatModel({"levels": {"g": [80, 100], "j2": [9, 10], "j2m0": [9, 10], "j2m0w5": [0, 1]}, "L": {"dealer": 1, "non": 1}})
        p_big = m.p_hat(2, 0, 5)
        self.assertTrue(0.7 < p_big < 0.9)        # 单个失败的小格子几乎不动
        self.assertEqual(V.wall_bin(100), 5)
        self.assertEqual(V.wall_bin(20), 0)
        self.assertEqual(V.wall_bin(36), 2)


if __name__ == "__main__":
    unittest.main()
