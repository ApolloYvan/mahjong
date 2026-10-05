"""bot 接入实时 MC：开关全 0 时行为不变、开关打开时的覆盖/回落、抓打圈受限方能胡的修复、ChainTracker。"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MJ_WEIGHTS_NO_FILE", "1")

import mj.bot as bot  # noqa: E402
from mj.fit import weights_overlay  # noqa: E402
from mj.mc import decide  # noqa: E402
from mj.mc.chain_tracker import ChainTracker  # noqa: E402
from mj.sim.engine import RoundEngine, build_wall  # noqa: E402
from mj.sim.snapshot import build_snapshot  # noqa: E402

ALL_ON = {"mc_hu_enabled": 1, "mc_discard_enabled": 1, "mc_menqing_enabled": 1}


def _snapshots(n_rounds=4, per_round=12):
    """用生产 choose_action（开关全 0）自己打几局，沿途收集摸牌/响应快照。"""
    out = []
    for seed in range(n_rounds):
        e = RoundEngine(build_wall(seed), seed % 4, 1)
        steps = 0
        while not e.finished and steps < 400:
            steps += 1
            if e.phase == "draw":
                seat = e.turn
                if e.needs_draw():
                    e.step_draw()
                    if e.finished:
                        break
                snap = build_snapshot(e, seat)
                act = bot._choose_action_production(snap)
                if steps % 2 == 0 and sum(1 for x in out if x['phase'] == 'draw') < n_rounds * per_round:
                    out.append(snap)
                try:
                    if act["action"] == "hu":
                        e.apply_hu(seat)
                    elif act["action"] == "gang":
                        e.apply_discard(seat, snap["drawn_tile"])
                    else:
                        e.apply_discard(seat, act["tile"])
                except Exception:
                    e.apply_discard(seat, snap["drawn_tile"] if snap["drawn_tile"] in e.hands[seat] else e.hands[seat][-1])
            elif e.phase == "response":
                for seat in list(e.responding_seats):
                    if e.phase != "response" or seat not in e.responding_seats:
                        continue
                    snap = build_snapshot(e, seat)
                    if sum(1 for x in out if x['phase'] != 'draw') < n_rounds * 4:
                        out.append(snap)
                    e.apply_pass(seat)
            else:
                break
    return out


class TestSwitchesOffIdentical(unittest.TestCase):
    def test_choose_action_equals_production_and_never_touches_mc(self):
        snaps = _snapshots()
        self.assertGreater(len(snaps), 20)
        with patch.object(decide, "mc_override", side_effect=AssertionError("开关全 0 不该调用 mc_override")):
            for snap in snaps:
                self.assertEqual(bot.choose_action(snap), bot._choose_action_production(snap))
                self.assertEqual(bot.choose_action(snap, mc_ctx={"t0": 0.0}), bot._choose_action_production(snap))


class TestOverrideWiring(unittest.TestCase):
    def setUp(self):
        self.snap = next(s for s in _snapshots() if s["phase"] == "draw")
        self.prod = bot._choose_action_production(self.snap)

    def test_override_used(self):
        alt = {"action": "discard", "tile": self.snap["my_hand"][0]}
        with weights_overlay({"mc_discard_enabled": 1}), patch.object(decide, "mc_override", return_value=alt) as m:
            ctx = {"t0": 1.0}
            self.assertEqual(bot.choose_action(self.snap, mc_ctx=ctx), alt)
        self.assertEqual(m.call_args[0][1], self.prod)
        self.assertIs(m.call_args[0][3], ctx)

    def test_none_keeps_production(self):
        with weights_overlay(ALL_ON), patch.object(decide, "mc_override", return_value=None):
            self.assertEqual(bot.choose_action(self.snap), self.prod)

    def test_exception_keeps_production(self):
        with weights_overlay(ALL_ON), patch.object(decide, "mc_override", side_effect=RuntimeError("boom")):
            self.assertEqual(bot.choose_action(self.snap), self.prod)

    def test_real_mc_returns_legal_action_and_logs(self):
        saved = (dict(decide._OPP), decide.get_config())
        decide._OPP.update(loaded=True, params={}, ok=False)
        decide.set_config(mc_min_n=4)
        try:
            ctx = {"fixed_n": 16, "seed": 3}
            with weights_overlay(ALL_ON):
                act = bot.choose_action(self.snap, mc_ctx=ctx)
            self.assertIn(act["action"], ("discard", "hu", "gang"))
            if act["action"] == "discard":
                self.assertIn(act["tile"], self.snap["my_hand"])
        finally:
            decide._OPP.clear()
            decide._OPP.update(saved[0])
            decide._CONFIG.clear()
            decide._CONFIG.update(saved[1])


def _catch_snapshot(restricted):
    return {
        "seat": 0, "turn": 0, "phase": "draw", "dealer": 1, "round_no": 2,
        "wall_remaining": 60, "scores": [0, 0, 0, 0],
        "my_hand": ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w", "1b", "2b", "3b", "东", "东"],
        "discards": [[], [], [], []], "melds": [[], [], [], []], "drawn_tile": "东",
        "window_tile": None, "responding_seats": [],
        "god": {"baotou": False, "chain_count": 0, "catch_play": True, "god_discarder_seat": 2 if restricted else 0},
    }


class TestCatchPlayHuFix(unittest.TestCase):
    def test_restricted_declining_to_other_tile_becomes_hu(self):
        snap = _catch_snapshot(True)
        self.assertTrue(bot.can_hu(snap))
        with patch.object(bot, "choose_hu_or_piao", return_value={"action": "discard", "tile": "1w"}):
            self.assertEqual(bot.choose_action(snap), {"action": "hu", "tile": "东"})

    def test_restricted_discarding_drawn_tile_unchanged(self):
        snap = _catch_snapshot(True)
        with patch.object(bot, "choose_hu_or_piao", return_value={"action": "discard", "tile": "东"}):
            self.assertEqual(bot.choose_action(snap), {"action": "discard", "tile": "东"})

    def test_exempt_seat_unchanged(self):
        snap = _catch_snapshot(False)
        with patch.object(bot, "choose_hu_or_piao", return_value={"action": "discard", "tile": "1w"}):
            self.assertEqual(bot.choose_action(snap), {"action": "discard", "tile": "1w"})

    def test_restricted_hu_unchanged(self):
        snap = _catch_snapshot(True)
        with patch.object(bot, "choose_hu_or_piao", return_value={"action": "hu", "tile": "东"}):
            self.assertEqual(bot.choose_action(snap), {"action": "hu", "tile": "东"})


class TestChainTracker(unittest.TestCase):
    def _snap(self, chain=0, catch=False, gds=-1, round_no=1, phase="draw"):
        return {"seat": 0, "round_no": round_no, "phase": phase,
                "god": {"chain_count": chain, "catch_play": catch, "god_discarder_seat": gds}}

    def test_free_piao_counts_and_normal_discard_resets(self):
        t = ChainTracker()
        t.observe(self._snap(), {"action": "discard", "tile": "白"})
        self.assertEqual(t.ctx(self._snap(chain=1, catch=True, gds=0)), {"piao_count": 1, "chain_has_gang": False})
        t.observe(self._snap(chain=1, catch=True, gds=0), {"action": "discard", "tile": "白"})
        self.assertEqual(t.piao, 2)
        t.observe(self._snap(chain=2), {"action": "discard", "tile": "1w"})
        self.assertEqual(t.piao, 0)

    def test_forced_joker_discard_is_not_piao(self):
        t = ChainTracker()
        t.observe(self._snap(catch=True, gds=2), {"action": "discard", "tile": "白"})
        self.assertEqual(t.piao, 0)

    def test_gang_sets_flag_and_chain_gone_resets(self):
        t = ChainTracker()
        t.observe(self._snap(), {"action": "gang", "tile": "1w"})
        self.assertTrue(t.has_gang)
        self.assertTrue(t.ctx(self._snap(chain=1))["chain_has_gang"])
        self.assertFalse(t.ctx(self._snap(chain=0))["chain_has_gang"])

    def test_new_round_resets(self):
        t = ChainTracker()
        t.observe(self._snap(), {"action": "discard", "tile": "白"})
        self.assertEqual(t.ctx(self._snap(chain=1, round_no=2))["piao_count"], 0)


if __name__ == "__main__":
    unittest.main()
