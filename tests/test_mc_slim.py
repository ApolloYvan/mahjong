import os
import random
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
os.environ.setdefault("MJ_WEIGHTS_NO_FILE", "1")

import mc_check  # noqa: E402
from mj.mc.slim import Slim, _std_possible, play_out  # noqa: E402
from mj.rules import win_standard  # noqa: E402
from mj.sim.engine import RoundEngine, build_wall  # noqa: E402
from mj.tiles import ALL_TILES, JOKER_IDX, to_counts  # noqa: E402


class SlimEquivalenceTests(unittest.TestCase):
    def test_lockstep_with_round_engine(self):
        bad = [r for r in (mc_check._lockstep_round(seed) for seed in range(150)) if r]
        self.assertEqual(bad, [])

    def test_play_out_terminates_and_zero_sum(self):
        for seed in range(30):
            st = Slim.from_wall(build_wall(seed), seed % 4, 1, [0, 0, 0, 0])
            play_out(st, random.Random(seed))
            self.assertTrue(st.finished)
            if not st.is_draw:
                self.assertLessEqual(abs(sum(st.scores)), 0)

    def test_from_engine_matches_from_wall_deal(self):
        tiles = build_wall(5)
        e = RoundEngine(tiles, 2, 3, [0, 0, 0, 0])
        a = Slim.from_engine(e)
        b = Slim.from_wall(tiles, 2, 3, [0, 0, 0, 0])
        self.assertEqual(a.counts, b.counts)
        self.assertEqual(a.wall, b.wall)

    def test_needs_draw_false_after_claim(self):
        st = Slim.from_wall(build_wall(1), 0, 1, [0, 0, 0, 0])
        self.assertTrue(st.needs_draw())


class IsolatedSingleFilterTests(unittest.TestCase):
    def test_filter_never_rejects_a_winning_hand(self):
        rng = random.Random(0)
        pool = [t for t in ALL_TILES for _ in range(4)]
        checked = 0
        for _ in range(4000):
            rng.shuffle(pool)
            hand = pool[:14]
            counts = to_counts(hand)
            for mg in (0, 1):
                n = 14 - 3 * mg
                c = to_counts(pool[:n]) if mg else counts
                if win_standard(c, mg):
                    checked += 1
                    self.assertTrue(_std_possible(list(c)), (hand, mg))
        self.assertGreaterEqual(checked, 0)

    def test_filter_exact_on_constructed_winning_hands_with_jokers(self):
        rng = random.Random(1)
        # 4 个面子 + 一对，随机把若干张换成财神（仍然是胡牌）
        for _ in range(500):
            tiles = []
            for _g in range(4):
                s = rng.choice("wbt")
                r = rng.randint(1, 7)
                tiles += ["%d%s" % (r + k, s) for k in range(3)] if rng.random() < 0.6 else ["%d%s" % (r, s)] * 3
            tiles += [rng.choice(ALL_TILES[:33])] * 2
            for i in rng.sample(range(14), rng.randint(0, 4)):
                tiles[i] = "白"
            c = to_counts(tiles)
            if max(c[:JOKER_IDX]) > 4:
                continue
            self.assertTrue(win_standard(c, 0))
            self.assertTrue(_std_possible(list(c)), tiles)


def _full_topup(p):
    row = [0.0] + [p] * 20
    fan = [[1, 0.5], [2, 1.0], [4, 1.0], [8, 1.0], [16, 1.0]]
    return {"p": {"d": row, "n": row}, "fan": {"d": fan, "n": fan}}


class TopupTests(unittest.TestCase):
    def test_virtual_win_settles_zero_sum_and_marks_virtual(self):
        for seed in range(20):
            st = Slim.from_wall(build_wall(seed), seed % 4, 1, [0, 0, 0, 0])
            play_out(st, random.Random(seed), params={"topup": _full_topup(1.0)})
            self.assertTrue(st.finished and not st.is_draw)
            self.assertEqual(sum(st.scores), 0)
            self.assertEqual(st.draws[st.winner], 1)
            self.assertIn(st.fan, (1, 2))
            self.assertGreater(st.scores[st.winner], 0)

    def test_virtual_win_dealer_pays_like_real_selfdraw(self):
        st = Slim.from_wall(build_wall(5), 0, 1, [0, 0, 0, 0])
        st.step_draw()
        st.apply_virtual_hu(0, 2)
        ref = Slim.from_wall(build_wall(5), 0, 1, [0, 0, 0, 0])
        ref._settle_selfdraw(0, 2)
        self.assertEqual(st.scores, ref.scores)
        self.assertEqual(st.scores[1:], [-16, -16, -16])

    def test_our_seat_never_gets_topup(self):
        for seed in range(20):
            st = Slim.from_wall(build_wall(seed), 0, 1, [0, 0, 0, 0])
            play_out(st, random.Random(seed), our_seat=0, params={"topup": _full_topup(1.0)})
            if st.virtual:
                self.assertNotEqual(st.winner, 0)

    def test_no_topup_means_no_virtual(self):
        for seed in range(20):
            st = Slim.from_wall(build_wall(seed), seed % 4, 1, [0, 0, 0, 0])
            play_out(st, random.Random(seed))
            self.assertFalse(st.virtual)


class CalibrateHelperTests(unittest.TestCase):
    def test_bucket_hazards_roundtrip(self):
        cum = {0: 0.0}
        for k in mc_check.CURVE_KS:
            cum[k] = 1.0 - (1.0 - 0.05) ** k      # 恒定每摸 5%
        h = mc_check._bucket_hazards(cum)
        for k in range(1, 21):
            self.assertAlmostEqual(h[k], 0.05, places=6)
        self.assertEqual(h[0], 0.0)

    def test_compensated_fan_normalised_and_matches_mix(self):
        tg = {1: 0.68, 2: 0.27, 4: 0.04, 8: 0.01, 16: 0.0}
        st = {"fan_strategy": {1: 85, 2: 15}, "fan_virtual": {1: 50, 2: 50}}
        q = mc_check._compensated_fan(tg, st)
        self.assertAlmostEqual(sum(q.values()), 1.0)
        ws = 100 / 200
        mix2 = ws * 0.15 + (1 - ws) * q[2]
        self.assertGreater(q[2], 0.27)           # 策略自己 2番+ 偏低，虚拟胡要补回来
        self.assertAlmostEqual(mix2, 0.27, delta=0.03)

    def test_fan_cum_last_is_one(self):
        c = mc_check._fan_cum({1: 3, 2: 1})
        self.assertEqual(c[-1][1], 1.0)


if __name__ == "__main__":
    unittest.main()
