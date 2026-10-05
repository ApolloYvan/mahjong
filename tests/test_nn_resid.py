import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MJ_WEIGHTS_NO_FILE", "1")

import mj.bot as bot  # noqa: E402
from mj.fit import weights_overlay  # noqa: E402
from mj.nn import cands as C  # noqa: E402
from mj.nn import resid  # noqa: E402
from mj.sim.engine import RoundEngine, build_wall  # noqa: E402
from mj.sim.snapshot import build_snapshot  # noqa: E402

H1, H2 = 4, 3


def _snap(seed):
    e = RoundEngine(build_wall(seed), seed % 4, 1)
    e.step_draw()
    return build_snapshot(e, e.turn)


def _net(rank1_bonus=0.0):
    """mean=0,std=1；g(x)=rank1_bonus * x[rank1]：只给"排第二的候选"加分。"""
    D = C.FEATURE_DIM

    def z(*shape):
        n = 1
        for s in shape:
            n *= s
        return [0.0] * n

    w1 = z(D, H1)
    w1[C.FEATURE_NAMES.index("rank1") * H1 + 0] = 1.0
    w2 = z(H1, H2)
    w2[0] = 1.0
    w3 = z(H2, 1)
    w3[0] = rank1_bonus
    t = {"w1": ([D, H1], w1), "b1": ([H1], z(H1)), "w2": ([H1, H2], w2), "b2": ([H2], z(H2)),
         "w3": ([H2, 1], w3), "b3": ([1], [0.0])}
    return resid.ResidNet(resid.pack_resid([H1, H2], t, [0.0] * D, [1.0] * D))


class CandidateTests(unittest.TestCase):
    def test_first_candidate_is_production_choice(self):
        for seed in range(25):
            snap = _snap(seed)
            prod = bot.choose_discard(snap)["tile"]
            cs = C.production_candidates(snap, prod)
            self.assertEqual(cs[0]["tile"], prod)
            self.assertLessEqual(len(cs), C.K_DEFAULT)
            self.assertEqual(len({c["tile"] for c in cs}), len(cs))
            self.assertTrue(all(c["tile"] in snap["my_hand"] for c in cs))
            self.assertEqual([c["rank"] for c in cs], list(range(len(cs))))
            # 不传 prod_tile 时自己调用生产，结果一致
            self.assertEqual(C.production_candidates(snap)[0]["tile"], prod)

    def test_feature_vectors_fixed_dim_and_finite(self):
        import math
        for seed in range(10):
            snap = _snap(seed)
            cs = C.production_candidates(snap)
            vecs = C.candidate_features(snap, cs)
            self.assertEqual(len(vecs), len(cs))
            for v in vecs:
                self.assertEqual(len(v), C.FEATURE_DIM)
                self.assertTrue(all(math.isfinite(x) for x in v))
            self.assertEqual(vecs[0][C.FEATURE_NAMES.index("is_prod")], 1.0)


class ResidTests(unittest.TestCase):
    def test_zero_correction_equals_production(self):
        net = _net(0.0)
        for seed in range(15):
            snap = _snap(seed)
            prod = bot._choose_action_production(snap)
            if prod["action"] != "discard":
                continue
            new, log = resid.rescore(snap, prod, None, net=net)
            self.assertIsNone(new)

    def test_correction_can_override_and_is_a_legal_candidate(self):
        net = _net(10.0)
        hits = 0
        for seed in range(15):
            snap = _snap(seed)
            prod = bot._choose_action_production(snap)
            if prod["action"] != "discard":
                continue
            new, log = resid.rescore(snap, prod, None, net=net)
            if new is not None:
                hits += 1
                self.assertIn(new["tile"], snap["my_hand"])
                self.assertNotEqual(new["tile"], prod["tile"])
                self.assertEqual(new["tile"], log["cands"][1])
        self.assertGreater(hits, 5)

    def test_threshold_tau_blocks_small_predicted_advantage(self):
        snap = _snap(5)
        prod = bot._choose_action_production(snap)
        if prod["action"] != "discard":
            self.skipTest("这局不是出牌")
        small = _net(0.5)     # tau 默认 1.0：预测优势 0.5 不够
        self.assertEqual(small.tau, 1.0)
        self.assertIsNone(resid.rescore(snap, prod, None, net=small)[0])
        new, log = resid.rescore(snap, prod, None, net=_net(10.0))
        self.assertIsNotNone(new)
        self.assertEqual(log["adv"][0], 0.0)

    def test_feature_budget_exhausted_falls_back(self):
        snap = _snap(6)
        prod = bot._choose_action_production(snap)
        with patch.object(resid, "FEATURE_BUDGET_S", -1.0):
            new, log = resid.rescore(snap, prod, None, net=_net(10.0))
        self.assertIsNone(new)
        self.assertEqual(log["fallback"], "features_timeout")

    def test_non_discard_and_restricted_untouched(self):
        net = _net(10.0)
        snap = _snap(1)
        self.assertIsNone(resid.rescore(snap, {"action": "hu", "tile": "1w"}, None, net=net)[0])
        snap = dict(snap, god={"chain_count": 0, "catch_play": True, "god_discarder_seat": (snap["seat"] + 1) % 4})
        self.assertIsNone(resid.rescore(snap, {"action": "discard", "tile": snap["drawn_tile"]}, None, net=net)[0])

    def test_missing_model_falls_back(self):
        with patch.dict(os.environ, {"MJ_NN_RESID_PATH": "/nonexistent/r.json"}):
            snap = _snap(2)
            new, log = resid.rescore(snap, bot._choose_action_production(snap), None)
        self.assertIsNone(new)
        self.assertEqual(log["fallback"], "no_model")

    def test_dim_mismatch_rejected(self):
        obj = resid.pack_resid([H1, H2], {"w1": ([C.FEATURE_DIM, H1], [0.0] * C.FEATURE_DIM * H1)}, [0.0] * C.FEATURE_DIM,
                               [1.0] * C.FEATURE_DIM)
        obj["feature_dim"] = C.FEATURE_DIM + 1
        with self.assertRaises(ValueError):
            resid.ResidNet(obj)


class BotTests(unittest.TestCase):
    def test_off_never_touches_resid(self):
        snap = _snap(3)
        with patch.object(resid, "rescore", side_effect=AssertionError("开关为 0")):
            self.assertEqual(bot.choose_action(snap), bot._choose_action_production(snap))

    def test_on_applies_override_and_exception_falls_back(self):
        snap = _snap(4)
        prod = bot._choose_action_production(snap)
        alt = {"action": "discard", "tile": next(t for t in snap["my_hand"] if t != prod.get("tile"))}
        with weights_overlay({"nn_resid_enabled": 1}):
            with patch.object(resid, "rescore", return_value=(alt, {"cands": ["a"], "pick": 1})):
                ctx = {}
                self.assertEqual(bot.choose_action(snap, mc_ctx=ctx), alt)
                self.assertEqual(ctx["resid_log"]["pick"], 1)
            with patch.object(resid, "rescore", side_effect=RuntimeError("x")):
                self.assertEqual(bot.choose_action(snap), prod)


if __name__ == "__main__":
    unittest.main()
