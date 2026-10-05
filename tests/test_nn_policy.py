import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("MJ_WEIGHTS_NO_FILE", "1")

import mj.bot as bot  # noqa: E402
from mj.fit import weights_overlay  # noqa: E402
from mj.nn import encode as E  # noqa: E402
from mj.nn import infer, policy  # noqa: E402
from mj.sim.engine import RoundEngine, build_wall  # noqa: E402
from mj.sim.snapshot import build_snapshot  # noqa: E402

H1, H2 = 6, 5


def _tiny_model(bias_d=None, bias_r=None, bias_h=None):
    """全零权重 + 指定头偏置：输出就是偏置本身，用来钉死"选动作/合法掩码/回落"的行为。"""
    def zeros(*shape):
        n = 1
        for s in shape:
            n *= s
        return (list(shape), [0.0] * n)

    t = {"w1": zeros(E.FEATURE_DIM, H1), "b1": zeros(H1), "w2": zeros(H1, H2), "b2": zeros(H2)}
    for k, n in (("d", 35), ("r", 6), ("h", 3), ("v", 1)):
        t["w" + k] = zeros(H2, n)
        b = {"d": bias_d, "r": bias_r, "h": bias_h}.get(k)
        t["b" + k] = ([n], list(b) if b else [0.0] * n)
    return infer.pack(E.FEATURE_DIM, [H1, H2], t)


def _snap(seed=0, seat=0):
    e = RoundEngine(build_wall(seed), seat, 1)
    e.step_draw()
    return build_snapshot(e, seat)


class InferTests(unittest.TestCase):
    def test_pack_load_roundtrip_and_forward_matches_dense(self):
        import random
        rng = random.Random(1)
        shapes = {"w1": (E.FEATURE_DIM, H1), "b1": (H1,), "w2": (H1, H2), "b2": (H2,)}
        for k, n in (("d", 35), ("r", 6), ("h", 3), ("v", 1)):
            shapes["w" + k] = (H2, n)
            shapes["b" + k] = (n,)
        raw = {}
        for name, shp in shapes.items():
            size = 1
            for x in shp:
                size *= x
            raw[name] = (list(shp), [rng.uniform(-1, 1) for _ in range(size)])
        net = infer.Net(infer.pack(E.FEATURE_DIM, [H1, H2], raw))
        snap = _snap(3)
        idx, val = E.encode_sparse(snap)
        out = net.forward(idx, val)
        # 稠密参考（用 float32 舍入后的权重）
        import array
        r32 = {k: list(array.array("f", v[1])) for k, v in raw.items()}
        x = E.encode(snap)
        h = [max(0.0, r32["b1"][j] + sum(x[i] * r32["w1"][i * H1 + j] for i in range(E.FEATURE_DIM))) for j in range(H1)]
        h = [max(0.0, r32["b2"][j] + sum(h[i] * r32["w2"][i * H2 + j] for i in range(H1))) for j in range(H2)]
        for k, n in (("d", 35), ("r", 6), ("h", 3)):
            ref = [r32["b" + k][j] + sum(h[i] * r32["w" + k][i * n + j] for i in range(H2)) for j in range(n)]
            self.assertLess(max(abs(a - b) for a, b in zip(out[k], ref)), 1e-9)

    def test_bad_version_rejected(self):
        obj = _tiny_model()
        obj["version"] = 99
        with self.assertRaises(ValueError):
            infer.Net(obj)


class PolicyTests(unittest.TestCase):
    def test_draw_picks_max_legal_discard(self):
        snap = _snap(2)
        want = E.TILE_INDEX[snap["my_hand"][3]]
        bias = [0.0] * 35
        bias[want] = 5.0
        bias[33] = 9.0 if "白" not in snap["my_hand"] else 0.0   # 不在手里的牌即使分高也不能选
        net = infer.Net(_tiny_model(bias_d=bias))
        act, log = policy.nn_action(snap, None, net=net)
        self.assertEqual(act, {"action": "discard", "tile": E.INDEX_TILE[want]})
        self.assertNotIn("fallback", log)

    def test_hu_head_three_way(self):
        snap = {"seat": 0, "turn": 0, "phase": "draw", "dealer": 0, "round_no": 1, "wall_remaining": 60,
                "scores": [0, 0, 0, 0],
                "my_hand": ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w", "1b", "2b", "3b", "东", "东"],
                "drawn_tile": "东", "melds": [[], [], [], []], "discards": [[], [], [], []], "responding_seats": [],
                "god": {"chain_count": 0, "catch_play": False, "god_discarder_seat": -1}}
        self.assertIn(E.A_HU, E.legal_actions(snap))
        for bias_h, expect in (([5, 0, 0], "hu"), ([0, 5, 0], "discard"), ([0, 0, 5], "discard")):
            net = infer.Net(_tiny_model(bias_h=bias_h))
            act, _ = policy.nn_action(snap, None, net=net)
            self.assertEqual(act["action"], expect)
        net = infer.Net(_tiny_model(bias_h=[0, 5, 0]))   # 飘但手里没财神 -> 当弃胡处理，不会打出不存在的牌
        act, _ = policy.nn_action(snap, None, net=net)
        self.assertIn(act["tile"], snap["my_hand"])

    def test_response_mask_and_pass(self):
        snap = {"seat": 1, "phase": "response_peng", "turn": 0, "dealer": 0, "round_no": 1, "wall_remaining": 50,
                "scores": [0, 0, 0, 0], "my_hand": ["2w", "3w", "5w", "6w", "9b"], "last_discard": "9b",
                "responding_seats": [1], "melds": [[], [], [], []], "discards": [[], [], [], []],
                "god": {"chain_count": 0, "catch_play": False, "god_discarder_seat": -1}}
        net = infer.Net(_tiny_model(bias_r=[0, 5, 0, 5, 5, 5]))   # 碰/吃分高，但手里只有 1 张 9b：碰不合法、吃窗口不对
        act, _ = policy.nn_action(snap, None, net=net)
        self.assertEqual(act, {"action": "pass", "tile": ""})

    def test_missing_model_and_slow_fall_back(self):
        snap = _snap(1)
        with patch.dict(os.environ, {"MJ_NN_POLICY_PATH": "/nonexistent/x.json"}):
            act, log = policy.nn_action(snap, None)
        self.assertIsNone(act)
        self.assertEqual(log["fallback"], "no_model")
        act, log = policy.nn_action(snap, None, net=infer.Net(_tiny_model()), budget_s=-1.0)
        self.assertIsNone(act)
        self.assertEqual(log["fallback"], "timeout")

    def test_corrupt_model_falls_back(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write("{not json")
        try:
            with patch.dict(os.environ, {"MJ_NN_POLICY_PATH": f.name}):
                act, log = policy.nn_action(_snap(1), None)
            self.assertIsNone(act)
            self.assertTrue(log["fallback"].startswith("load_error"))
        finally:
            os.unlink(f.name)


class BotIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.snap = _snap(4)
        self.prod = bot._choose_action_production(self.snap)

    def test_off_by_default_never_touches_nn(self):
        with patch.object(policy, "nn_action", side_effect=AssertionError("开关为 0 不该调用网络")):
            self.assertEqual(bot.choose_action(self.snap), self.prod)
            self.assertEqual(bot.choose_action(self.snap, mc_ctx={}), self.prod)

    def _enabled_with(self, net_obj):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(net_obj, f)
        self.addCleanup(os.unlink, f.name)
        patcher = patch.dict(os.environ, {"MJ_NN_POLICY_PATH": f.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_enabled_uses_network_action_and_logs(self):
        want = self.snap["my_hand"][5]
        bias = [0.0] * 35
        bias[E.TILE_INDEX[want]] = 5.0
        self._enabled_with(_tiny_model(bias_d=bias))
        ctx = {}
        with weights_overlay({"nn_policy_enabled": 1}):
            act = bot.choose_action(self.snap, mc_ctx=ctx)
        self.assertEqual(act, {"action": "discard", "tile": want})
        self.assertIn("ms", ctx["nn_log"])

    def test_missing_model_falls_back_to_production(self):
        with patch.dict(os.environ, {"MJ_NN_POLICY_PATH": "/nonexistent/x.json"}), \
                weights_overlay({"nn_policy_enabled": 1}):
            ctx = {}
            self.assertEqual(bot.choose_action(self.snap, mc_ctx=ctx), self.prod)
        self.assertEqual(ctx["nn_log"]["fallback"], "no_model")

    def test_exception_falls_back(self):
        with weights_overlay({"nn_policy_enabled": 1}), patch.object(policy, "nn_action", side_effect=RuntimeError("x")):
            self.assertEqual(bot.choose_action(self.snap), self.prod)

    def test_production_check_vetoes_illegal_hu(self):
        """网络选"胡"但生产判定不能胡 -> 回落生产。"""
        self._enabled_with(_tiny_model(bias_h=[9, 0, 0]))
        with weights_overlay({"nn_policy_enabled": 1}), \
                patch.object(policy, "nn_action", return_value=({"action": "hu", "tile": "1w"}, {})):
            ctx = {}
            self.assertEqual(bot.choose_action(self.snap, mc_ctx=ctx), self.prod)
        self.assertEqual(ctx["nn_log"]["fallback"], "invalid_by_production_check")

    def test_catch_restricted_discard_must_be_drawn_tile(self):
        snap = dict(self.snap, god={"chain_count": 0, "catch_play": True, "god_discarder_seat": 2})
        other = next(t for t in snap["my_hand"] if t != snap["drawn_tile"])
        with weights_overlay({"nn_policy_enabled": 1}), \
                patch.object(policy, "nn_action", return_value=({"action": "discard", "tile": other}, {})):
            self.assertEqual(bot.choose_action(snap), bot._choose_action_production(snap))


if __name__ == "__main__":
    unittest.main()
