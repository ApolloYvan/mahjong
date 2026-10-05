import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
os.environ.setdefault("MJ_WEIGHTS_NO_FILE", "1")

import arena2  # noqa: E402
from mj.mc import decide  # noqa: E402

ALL_ON = {"mc_hu_enabled": 1, "mc_discard_enabled": 1, "mc_menqing_enabled": 1, "mc_fixed_n": 16}


class Arena2McTests(unittest.TestCase):
    def setUp(self):
        self._saved = (dict(decide._OPP), decide.get_config())
        decide._OPP.update(loaded=True, params={}, ok=False)
        decide.set_config(mc_min_n=4)

    def tearDown(self):
        decide._OPP.clear()
        decide._OPP.update(self._saved[0])
        decide._CONFIG.clear()
        decide._CONFIG.update(self._saved[1])

    def test_mc_seed_deterministic_and_distinct(self):
        self.assertEqual(arena2.mc_seed(3, 10), arena2.mc_seed(3, 10))
        self.assertNotEqual(arena2.mc_seed(3, 10), arena2.mc_seed(3, 11))
        self.assertNotEqual(arena2.mc_seed(3, 10), arena2.mc_seed(4, 10))

    def test_match_with_mc_seat_reproducible_and_legal(self):
        runs = []
        for _ in range(2):
            m = arena2.Arena2Match(1, seat_weights={0: ALL_ON}, rounds=1, collect_illegal=True).play()
            runs.append(m)
        a, b = runs
        self.assertEqual(a.match.scores, b.match.scores)
        self.assertEqual(dict(a.stats), dict(b.stats))
        self.assertEqual(a.illegal_log, [])
        self.assertGreater(a.stats.get("mc_calls", 0), 0)
        self.assertEqual(len(a.mc_ms), a.stats["mc_calls"])

    def test_non_mc_seats_unaffected(self):
        m = arena2.Arena2Match(1, seat_weights={0: {"rev_speed_enabled": 0}}, rounds=1).play()
        self.assertEqual(m.stats.get("mc_calls", 0), 0)

    def test_max_minutes_stops_and_uses_finished(self):
        for jobs in (1, 2):
            res = arena2._run_ab("t_maxmin_%d" % jobs, list(range(50)), None, None, "1v3", 1, {}, jobs, True,
                                 max_minutes=1e-6)
            done = res[7]
            self.assertLess(len(done), 50)


if __name__ == "__main__":
    unittest.main()
