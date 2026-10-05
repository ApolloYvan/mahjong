"""消融开关：默认值 = 现行为（逐字节不变）；打开/关闭时各自的作用。"""
import os
import random
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MJ_WEIGHTS_NO_FILE", "1")

import mj.bot as bot  # noqa: E402
from mj.fit import weights_overlay  # noqa: E402
from mj.hu_strategy import _decline_small_hu_tile  # noqa: E402
from mj.joker_ev import joker_plan_value  # noqa: E402
from mj.shanten import shanten  # noqa: E402
from mj.sim.engine import RoundEngine, build_wall  # noqa: E402
from mj.sim.snapshot import build_snapshot  # noqa: E402
from mj.tiles import TILE_INDEX, to_counts  # noqa: E402

DEFAULTS = {"dealer_route_enabled": 1, "dealer_slow1_enabled": 1, "s1_dealer_enabled": 1,
            "gang_strict_enabled": 0, "joker_nonbaotou_hu_disabled": 0}


def _snaps(n=40):
    out = []
    for seed in range(n):
        e = RoundEngine(build_wall(seed), seed % 4, 1 + seed % 8)
        e.step_draw()
        out.append(build_snapshot(e, e.turn))     # 每局庄家/闲家都有（seed%4 决定庄，turn=庄）
        out.append(build_snapshot(e, (e.turn + 1) % 4) | {"drawn_tile": "", "phase": "draw"} if False else build_snapshot(e, e.turn))
    return out


class DefaultsUnchanged(unittest.TestCase):
    def test_choose_action_same_with_explicit_defaults(self):
        for snap in _snaps():
            base = bot.choose_action(snap)
            with weights_overlay(DEFAULTS):
                self.assertEqual(bot.choose_action(snap), base)

    def test_non_dealer_unaffected_by_dealer_switches(self):
        tested = 0
        for seed in range(30):
            e = RoundEngine(build_wall(seed), 0, 1)
            e.turn = 1 + seed % 3          # 非庄家的回合（庄家 0）
            e.step_draw()
            snap = build_snapshot(e, e.turn)
            assert snap["dealer"] != snap["seat"]
            base = bot.choose_action(snap)
            tested += 1
            with weights_overlay({"dealer_route_enabled": 0, "dealer_slow1_enabled": 0, "s1_dealer_enabled": 0}):
                self.assertEqual(bot.choose_action(snap), base)
        self.assertGreater(tested, 10)


class DealerRoute(unittest.TestCase):
    def test_inputs(self):
        e = RoundEngine(build_wall(1), 1, 1)
        e.step_draw()
        snap = build_snapshot(e, 1)
        self.assertEqual(snap["dealer"], snap["seat"])
        on = bot._discard_inputs(snap)
        self.assertTrue(on[6])
        self.assertTrue(on[4].get("dealer_hint"))
        with weights_overlay({"dealer_route_enabled": 0}):
            off = bot._discard_inputs(snap)
        self.assertFalse(off[6])
        self.assertNotIn("dealer_hint", off[4])
        with weights_overlay({"dealer_route_enabled": 1}):
            self.assertEqual(bot._discard_inputs(snap), on)


class DealerSlow1(unittest.TestCase):
    def test_default_equal_and_switch_changes_some_dealer_plans(self):
        rng = random.Random(3)
        tiles = [t for t in list(TILE_INDEX) if t != "白"]
        diff = 0
        for _ in range(250):
            hand = rng.sample(tiles * 4, 12) + ["白", "白"][:rng.choice([1, 1, 2])]
            hand = hand[:13]
            c = to_counts(hand)
            std = shanten(c, 0)
            w = {"b_shanten": 10000}
            a = joker_plan_value(c, 0, std, 10000, {"dealer_hint": True}, w)
            b = joker_plan_value(c, 0, std, 10000, {"dealer_hint": True}, {**w, "dealer_slow1_enabled": 1})
            self.assertEqual(a, b)
            z = joker_plan_value(c, 0, std, 10000, {"dealer_hint": True}, {**w, "dealer_slow1_enabled": 0})
            ns = joker_plan_value(c, 0, std, 10000, {}, {**w, "dealer_slow1_enabled": 0})
            self.assertEqual(z, ns)        # 关掉之后庄家和闲家同分
            diff += a != z
        self.assertGreater(diff, 0)


class S1Dealer(unittest.TestCase):
    HAND = ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w", "1b", "2b", "3b", "白", "白"]

    def _call(self, dealer, **w):
        snap = {"drawn_tile": "东", "wall_remaining": 60, "my_hand": self.HAND[:12] + ["东", "白"],
                "melds": [[], [], [], []], "seat": 0, "dealer": dealer}
        res = {"hu": True, "baotou": False, "fan": 1, "counts": tuple([0] * 33 + [2])}
        weights = {"rule_decline_joker_hold_enabled": 1, **w}
        return _decline_small_hu_tile(snap, res, weights)

    def test_switch(self):
        self.assertEqual(self._call(0), "东")                          # 庄家，默认触发
        self.assertEqual(self._call(0, s1_dealer_enabled=1), "东")
        self.assertIsNone(self._call(0, s1_dealer_enabled=0))          # 庄家关掉
        self.assertEqual(self._call(1, s1_dealer_enabled=0), "东")     # 闲家不受影响


class V2OldBug(unittest.TestCase):
    SNAP = {"seat": 0, "turn": 0, "phase": "draw", "dealer": 1, "round_no": 1, "wall_remaining": 60,
            "scores": [0] * 4, "drawn_tile": "白", "melds": [[], [], [], []], "discards": [[], [], [], []],
            "my_hand": ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w", "1b", "2b", "3b", "东", "白"],
            "god": {"chain_count": 0, "catch_play": False, "god_discarder_seat": -1}}

    def test_switch(self):
        res = bot.hu_result(self.SNAP)
        self.assertTrue(res and not res.get("baotou"))
        with weights_overlay({"joker_nonbaotou_hu_disabled": 0}):
            self.assertTrue(bot.can_hu(self.SNAP))
        with weights_overlay({"joker_nonbaotou_hu_disabled": 1}):
            self.assertFalse(bot.can_hu(self.SNAP))
            self.assertNotEqual(bot.choose_action(self.SNAP), {"action": "hu", "tile": "白"})


class GangStrict(unittest.TestCase):
    def _snap(self, hand, melds=None, tile=None):
        return {"seat": 0, "turn": 0, "phase": "draw", "dealer": 1, "round_no": 1, "wall_remaining": 60,
                "scores": [0] * 4, "drawn_tile": hand[-1], "my_hand": hand, "discards": [[], [], [], []],
                "melds": [melds or [], [], [], []], "god": {"chain_count": 0, "catch_play": False, "god_discarder_seat": -1}}

    def test_an_gang_ok_when_not_hurting(self):
        hand = ["7w", "7w", "7w", "7w", "1w", "2w", "3w", "4b", "5b", "6b", "1t", "2t", "8t", "东"]
        self.assertTrue(bot._gang_strict_ok(self._snap(hand), {"action": "gang", "tile": "7w"}))

    def test_an_gang_blocked_when_it_kills_seven_pairs(self):
        hand = ["2w", "2w", "2w", "2w", "3b", "3b", "5t", "5t", "7w", "7w", "东", "东", "9b", "9b"]
        self.assertFalse(bot._gang_strict_ok(self._snap(hand), {"action": "gang", "tile": "2w"}))

    def test_dispatch_reruns_production_without_gang(self):
        snap = self._snap(["7w"] * 4 + ["1w", "2w", "3w", "4b", "5b", "6b", "1t", "2t", "8t", "东"])
        calls = []

        def fake(snapshot, rules=None, gang_open=False, no_gang=False):
            calls.append(no_gang)
            return {"action": "discard", "tile": "东"} if no_gang else {"action": "gang", "tile": "7w"}
        with weights_overlay({"gang_strict_enabled": 1}), patch.object(bot, "_choose_action_production", side_effect=fake), \
                patch.object(bot, "_gang_strict_ok", return_value=False):
            self.assertEqual(bot.choose_action(snap), {"action": "discard", "tile": "东"})
        self.assertEqual(calls, [False, True])
        calls.clear()
        with weights_overlay({"gang_strict_enabled": 1}), patch.object(bot, "_choose_action_production", side_effect=fake), \
                patch.object(bot, "_gang_strict_ok", return_value=True):
            self.assertEqual(bot.choose_action(snap), {"action": "gang", "tile": "7w"})
        calls.clear()
        with patch.object(bot, "_choose_action_production", side_effect=fake):      # 开关默认关：不检查
            self.assertEqual(bot.choose_action(snap), {"action": "gang", "tile": "7w"})
        self.assertEqual(calls, [False])


if __name__ == "__main__":
    unittest.main()
