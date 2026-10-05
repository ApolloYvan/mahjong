import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools", "train"))
os.environ.setdefault("MJ_WEIGHTS_NO_FILE", "1")

from mj.nn import encode as E  # noqa: E402
from mj.sim.engine import RoundEngine, build_wall  # noqa: E402
from mj.sim.snapshot import build_snapshot  # noqa: E402


def _draw_snapshot(seed=0, seat=0):
    e = RoundEngine(build_wall(seed), seat, 1)
    e.step_draw()
    return e, build_snapshot(e, seat)


class EncodeTests(unittest.TestCase):
    def test_dim_and_sparse_dense_agree(self):
        _, snap = _draw_snapshot()
        idx, val = E.encode_sparse(snap, {"piao_count": 1, "chain_has_gang": True})
        dense = E.encode(snap, {"piao_count": 1, "chain_has_gang": True})
        self.assertEqual(len(dense), E.FEATURE_DIM)
        self.assertEqual(idx, [i for i, x in enumerate(dense) if x])
        self.assertTrue(all(v != 0 for v in val))

    def test_matches_independent_reference_and_decodes(self):
        import check_encoder as C
        for seed in range(12):
            _, snap = _draw_snapshot(seed, seed % 4)
            ctx = {"piao_count": seed % 3, "chain_has_gang": bool(seed % 2)}
            dense = E.encode(snap, ctx)
            self.assertEqual(dense, C.ref_encode(snap, ctx))
            self.assertEqual(C.check_decode(snap, ctx, dense), [])

    def test_relative_seats_are_rotation_invariant(self):
        """同一局面换个座位看（座位号整体平移）-> 特征完全一样。"""
        e, snap0 = _draw_snapshot(3, 0)
        shifted = dict(snap0)
        for key in ("melds", "discards", "scores"):
            shifted[key] = [snap0[key][(s - 1) % 4] for s in range(4)]
        shifted["seat"] = 1
        shifted["dealer"] = (snap0["dealer"] + 1) % 4
        shifted["turn"] = (snap0["turn"] + 1) % 4
        self.assertEqual(E.encode(snap0), E.encode(shifted))

    def test_actions_roundtrip_and_legal(self):
        _, snap = _draw_snapshot(2)
        legal = E.legal_actions(snap)
        for t in set(snap["my_hand"]):
            a = {"action": "discard", "tile": t}
            idx = E.action_to_index(snap, a)
            self.assertIn(idx, legal)
            self.assertEqual(E.index_to_action(snap, idx), a)
        self.assertEqual(E.action_to_index(snap, {"action": "hu"}), E.A_HU)

    def test_chi_slots_and_response_legal(self):
        snap = {"seat": 1, "phase": "response_chi", "turn": 0, "dealer": 0, "round_no": 1, "wall_remaining": 50,
                "scores": [0, 0, 0, 0], "my_hand": ["2w", "3w", "5w", "6w", "9b"], "window_tile": "4w",
                "responding_seats": [1], "melds": [[], [], [], []], "discards": [[], [], [], []],
                "god": {"chain_count": 0, "catch_play": False, "god_discarder_seat": -1}}
        self.assertEqual(E.chi_slot("4w", ["2w", "3w"]), E.A_CHI_LOW)
        self.assertEqual(E.chi_slot("4w", ["3w", "5w"]), E.A_CHI_MID)
        self.assertEqual(E.chi_slot("4w", ["5w", "6w"]), E.A_CHI_HIGH)
        self.assertEqual(E.legal_actions(snap), [E.A_PASS, E.A_CHI_LOW, E.A_CHI_MID, E.A_CHI_HIGH])
        a = E.index_to_action(snap, E.A_CHI_MID)
        self.assertEqual(a["tiles"], ["3w", "5w"])
        self.assertEqual(E.action_to_index(snap, a), E.A_CHI_MID)

    def test_catch_restricted_only_drawn_tile(self):
        _, snap = _draw_snapshot(1)
        snap["god"] = {"chain_count": 0, "catch_play": True, "god_discarder_seat": 2}
        legal = [i for i in E.legal_actions(snap) if i < 34]
        self.assertEqual(legal, [E.TILE_INDEX[snap["drawn_tile"]]])

    def test_mj_never_imports_training_code(self):
        bad = []
        for dirpath, _, files in os.walk(os.path.join(ROOT, "mj")):
            for fn in files:
                if fn.endswith(".py"):
                    with open(os.path.join(dirpath, fn), encoding="utf-8") as f:
                        for line in f:
                            if re.match(r"\s*(from|import)\s+(tools|train|torch|numpy)\b", line):
                                bad.append((fn, line.strip()))
        self.assertEqual(bad, [])

    def test_encode_speed_budget(self):
        import time
        _, snap = _draw_snapshot(5)
        t = time.perf_counter()
        for _ in range(200):
            E.encode_sparse(snap)
        per_ms = (time.perf_counter() - t) / 200 * 1000
        self.assertLess(per_ms, 5.0)   # 单次编码 <5ms（性能核；能效核约 4 倍，仍在 50ms 推理预算内）


if __name__ == "__main__":
    unittest.main()
