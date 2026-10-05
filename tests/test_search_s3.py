import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
for sub in ("tools", os.path.join("tools", "train")):
    sys.path.insert(0, os.path.join(ROOT, sub))
os.environ.setdefault("MJ_WEIGHTS_NO_FILE", "1")

import search_s3 as S  # noqa: E402
from mj.mc.determinize import determinize_batch  # noqa: E402
from mj.mc.slim import DEFAULT_PARAMS  # noqa: E402
from mj.sim.engine import RoundEngine, build_wall  # noqa: E402
from mj.sim.snapshot import build_snapshot  # noqa: E402


def _state(seed):
    e = RoundEngine(build_wall(seed), seed % 4, 1)
    e.step_draw()
    return build_snapshot(e, e.turn)


class SearchTests(unittest.TestCase):
    def setUp(self):
        self._saved = dict(S._W)
        S._W["p"] = dict(DEFAULT_PARAMS)

    def tearDown(self):
        S._W.clear()
        S._W.update(self._saved)

    def test_rollout_terminates_deterministic_and_finite(self):
        for seed in range(6):
            snap = _state(seed)
            deal = determinize_batch(snap, 1, seed)[0]
            tile = snap["drawn_tile"]
            a = S.rollout(snap, {}, deal, snap["seat"], tile, seed)
            b = S.rollout(snap, {}, deal, snap["seat"], tile, seed)
            self.assertEqual(a, b)
            self.assertTrue(-300 < a < 300)

    def test_first_discard_changes_outcome_possible(self):
        snap = _state(2)
        deal = determinize_batch(snap, 1, 2)[0]
        vals = {S.rollout(snap, {}, deal, snap["seat"], t, 2) for t in set(snap["my_hand"])}
        self.assertGreaterEqual(len(vals), 1)

    def test_classify(self):
        snap = _state(3)
        self.assertIn(S.classify(snap), ("joker", "menqing", "other", None))
        self.assertIsNone(S.classify(dict(snap, phase="response_peng")))
        self.assertIsNone(S.classify(dict(snap, god={"chain_count": 0, "catch_play": True,
                                                     "god_discarder_seat": (snap["seat"] + 1) % 4})))


if __name__ == "__main__":
    unittest.main()
