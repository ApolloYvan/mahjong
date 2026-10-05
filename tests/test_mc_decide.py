import os
import random
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("MJ_WEIGHTS_NO_FILE", "1")

from mj.fit import weights_overlay  # noqa: E402
from mj.mc import decide  # noqa: E402
from mj.sim.engine import RoundEngine, build_wall  # noqa: E402
from mj.sim.snapshot import build_snapshot  # noqa: E402

ALL_ON = {"mc_hu_enabled": 1, "mc_discard_enabled": 1, "mc_menqing_enabled": 1}


def _sample_snapshot(phase="draw"):
    return {
        "seat": 0, "turn": 0, "phase": phase, "dealer": 0, "round_no": 1,
        "wall_remaining": 60, "scores": [0, 0, 0, 0],
        "my_hand": ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w",
                    "1b", "2b", "3b", "东", "南"],
        "discards": [[], [], [], []], "melds": [[], [], [], []],
        "drawn_tile": "南", "window_tile": None, "responding_seats": [],
        "god": {"baotou": False, "chain_count": 0, "catch_play": False, "god_discarder_seat": -1},
    }


def _engine_snapshot(seed, want_joker=False):
    """从真实牌墙发的一局里取庄家第一次摸牌后的快照（庄家手里有/没有财神按 want_joker 挑种子）。"""
    for s in range(seed, seed + 200):
        e = RoundEngine(build_wall(s), 0, 1)
        e.step_draw()
        snap = build_snapshot(e, 0)
        if ("白" in snap["my_hand"]) == want_joker:
            return snap
    raise AssertionError("找不到合适的种子")


class _Calibrated:
    """给 decide 灌一份"校准参数"（默认策略、无补足表），测完还原。"""

    def setUp(self):
        self._saved_opp = dict(decide._OPP)
        self._saved_cfg = decide.get_config()
        decide._OPP.update(loaded=True, params={}, ok=False)

    def tearDown(self):
        decide._OPP.clear()
        decide._OPP.update(self._saved_opp)
        decide._CONFIG.clear()
        decide._CONFIG.update(self._saved_cfg)


class TestDisabledIsByteIdentical(unittest.TestCase):
    """开关全 0（默认）时 ``mc_override`` 不做任何事，直接返回 None。"""

    def test_defaults_are_off(self):
        self.assertEqual(decide._switches(), (False, False, False))

    def test_draw_discard_disabled(self):
        self.assertIsNone(decide.mc_override(_sample_snapshot("draw"), {"action": "discard", "tile": "南"}))

    def test_draw_hu_disabled(self):
        self.assertIsNone(decide.mc_override(_sample_snapshot("draw"), {"action": "hu"}))

    def test_no_snapshot_reads_happen_when_disabled(self):
        class Guarded(dict):
            def get(self, key, default=None):
                raise AssertionError("disabled 路径不该读快照 %r" % key)

            def __getitem__(self, key):
                raise AssertionError("disabled 路径不该读快照 %r" % key)

        self.assertIsNone(decide.mc_override(Guarded(_sample_snapshot("draw")), {"action": "discard", "tile": "南"}))

    def test_ctx_untouched_when_disabled(self):
        ctx = {"t0": 1.0}
        decide.mc_override(_sample_snapshot("draw"), {"action": "discard", "tile": "南"}, None, ctx)
        self.assertEqual(ctx, {"t0": 1.0})


class TestGating(_Calibrated, unittest.TestCase):
    def test_only_hu_switch_ignores_plain_discard(self):
        snap = _engine_snapshot(0, want_joker=True)
        with weights_overlay({"mc_hu_enabled": 1}):
            ctx = {"fixed_n": 8}
            self.assertIsNone(decide.mc_override(snap, {"action": "discard", "tile": snap["drawn_tile"]}, None, ctx))
        self.assertNotIn("mc_log", ctx)

    def test_no_calibration_falls_back(self):
        decide._OPP.update(loaded=True, params=None, ok=False)
        snap = _engine_snapshot(0, want_joker=True)
        ctx = {"fixed_n": 8}
        with weights_overlay(ALL_ON):
            self.assertIsNone(decide.mc_override(snap, {"action": "discard", "tile": snap["drawn_tile"]}, None, ctx))
        self.assertEqual(ctx["mc_log"]["fallback_reason"], "no_calibration")

    def test_catch_restricted_seat_is_skipped(self):
        snap = _engine_snapshot(0, want_joker=True)
        snap["god"] = {"baotou": False, "chain_count": 0, "catch_play": True, "god_discarder_seat": 2}
        ctx = {"fixed_n": 8}
        with weights_overlay(ALL_ON):
            self.assertIsNone(decide.mc_override(snap, {"action": "discard", "tile": snap["drawn_tile"]}, None, ctx))
        self.assertNotIn("mc_log", ctx)

    def test_gang_and_response_not_handled(self):
        snap = _engine_snapshot(0, want_joker=True)
        with weights_overlay(ALL_ON):
            self.assertIsNone(decide.mc_override(snap, {"action": "gang", "tile": "1w"}, None, {}))
            snap2 = dict(snap, phase="response_peng")
            self.assertIsNone(decide.mc_override(snap2, {"action": "pass", "tile": ""}, None, {}))


class TestEvaluate(_Calibrated, unittest.TestCase):
    def setUp(self):
        super().setUp()
        decide.set_config(mc_min_n=4)

    def _run(self, snap, ctx):
        with weights_overlay(ALL_ON):
            res = decide.mc_override(snap, {"action": "discard", "tile": snap["drawn_tile"]}, None, ctx)
        return res, ctx.get("mc_log")

    def test_fixed_n_is_deterministic_and_logged(self):
        snap = _engine_snapshot(3, want_joker=True)
        r1, l1 = self._run(snap, {"fixed_n": 24, "seed": 7})
        r2, l2 = self._run(snap, {"fixed_n": 24, "seed": 7})
        self.assertEqual(r1, r2)
        for k in ("cls", "cands", "prod", "alt", "n", "diff", "se", "fallback_reason", "overridden"):
            self.assertEqual(l1.get(k), l2.get(k), k)
        self.assertEqual(l1["cls"], "joker_discard")
        self.assertEqual(l1["n"], 24 - 6)           # pilot = n//4
        self.assertIn(l1["prod"], l1["cands"])

    def test_override_action_is_legal_discard_from_hand(self):
        for seed in range(6):
            snap = _engine_snapshot(seed * 11, want_joker=True)
            res, log = self._run(snap, {"fixed_n": 16, "seed": seed})
            if res is not None:
                self.assertEqual(res["action"], "discard")
                self.assertIn(res["tile"], snap["my_hand"])
                self.assertTrue(log["overridden"])

    def test_timeout_falls_back(self):
        snap = _engine_snapshot(3, want_joker=True)
        res, log = self._run(snap, {"t0": time.monotonic() - 10.0})   # 预算早就用完了
        self.assertIsNone(res)
        self.assertEqual(log["fallback_reason"], "timeout")

    def test_insufficient_n_falls_back(self):
        decide.set_config(mc_min_n=10 ** 6)
        snap = _engine_snapshot(3, want_joker=True)
        res, log = self._run(snap, {"fixed_n": 16, "seed": 1})
        self.assertIsNone(res)
        self.assertEqual(log["fallback_reason"], "insufficient_n")

    def test_exception_falls_back(self):
        snap = _engine_snapshot(3, want_joker=True)
        snap["my_hand"] = snap["my_hand"] + ["not-a-tile"]
        res, log = self._run(snap, {"fixed_n": 8})
        self.assertIsNone(res)
        self.assertTrue(log["fallback_reason"].startswith("exception"))

    def test_time_mode_respects_cap(self):
        decide.set_config(cap_discard_hu_s=0.3, mc_min_n=1)
        snap = _engine_snapshot(3, want_joker=True)
        t = time.monotonic()
        self._run(snap, {"t0": t})
        self.assertLess(time.monotonic() - t, 0.3 + 1.0)   # 截止后最多再多跑一次推演+收尾，不会拖到秒级以上

    def test_slim_from_snapshot_uses_ctx_chain_info(self):
        snap = _engine_snapshot(3, want_joker=True)
        snap["god"] = {"baotou": False, "chain_count": 2, "catch_play": True, "god_discarder_seat": 0}
        deal = decide.determinize_batch(snap, 1, 5)[0]
        st = decide.slim_from_snapshot(snap, deal, 0, {"piao_count": 2, "chain_has_gang": True})
        self.assertEqual((st.piao_count[0], st.chain_has_gang, st.chain_owner), (2, True, 0))
        st2 = decide.slim_from_snapshot(snap, deal, 0, {})
        self.assertEqual((st2.piao_count[0], st2.chain_has_gang), (0, False))


class TestCandidates(unittest.TestCase):
    def test_production_first_and_hu_candidates(self):
        snap = _sample_snapshot("draw")
        cands = decide._candidates(snap, {"action": "discard", "tile": "南"}, True, 2)
        self.assertEqual(cands[0], ("discard", "南"))
        self.assertIn(("hu", None), cands)

    def test_joker_discard_candidate_when_holding_joker(self):
        snap = _engine_snapshot(0, want_joker=True)
        cands = decide._candidates(snap, {"action": "discard", "tile": snap["drawn_tile"]}, False, 2)
        self.assertIn(("discard", "白"), cands)

    def test_dealer_value(self):
        self.assertEqual(decide._dealer_value(8), 0.0)
        self.assertGreater(decide._dealer_value(1), 0.0)


if __name__ == "__main__":
    unittest.main()
