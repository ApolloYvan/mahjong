import os
import random
import unittest

os.environ.setdefault("MJ_WEIGHTS_NO_FILE", "1")

from mj.discard_features import FD_FEATURES, features  # noqa: E402
from mj.strategy import choose_discard  # noqa: E402
from mj.tiles import ALL_TILES  # noqa: E402

ON = {"_weights": {"rule_fitted_discard_enabled": 1}}


def random_hands(n, size, seed):
    rng = random.Random(seed)
    wall = [t for t in ALL_TILES if t != "白"] * 4
    for _ in range(n):
        rng.shuffle(wall)
        yield wall[:size], [rng.randint(0, 3) for _ in range(34)]


class FittedDiscardDefaultsTests(unittest.TestCase):
    def test_defaults_reproduce_current_choice_closed(self):
        for hand, vis in random_hands(300, 14, 1):
            visible = [min(4, v + hand.count(t)) for v, t in zip(vis, ALL_TILES)]
            for v in (None, visible):
                self.assertEqual(choose_discard(hand, 0, 0, 0, {}, v), choose_discard(hand, 0, 0, 0, ON, v), hand)

    def test_defaults_reproduce_current_choice_one_meld(self):
        for hand, _ in random_hands(200, 11, 2):
            self.assertEqual(choose_discard(hand, 1, 0, 0, {}, None), choose_discard(hand, 1, 0, 0, ON, None), hand)

    def test_defaults_follow_std_pair_switch(self):
        off = {"_weights": {"rule_std_pair_enabled": 1}}
        on = {"_weights": {"rule_std_pair_enabled": 1, "rule_fitted_discard_enabled": 1}}
        for hand, _ in random_hands(200, 14, 3):
            self.assertEqual(choose_discard(hand, 0, 0, 0, off, None), choose_discard(hand, 0, 0, 0, on, None), hand)

    def test_joker_hand_uses_old_path(self):
        hand = ["白", "1w", "2w", "3w", "5b", "6b", "9t", "9t", "东", "南", "西", "4t", "7t", "8t"]
        self.assertEqual(choose_discard(hand, 0, 0, 0, {}, None), choose_discard(hand, 0, 0, 0, ON, None))

    def test_feature_values(self):
        hand = ["1w", "1w", "2b", "3b", "5t", "9t", "东", "东", "东", "南", "4w", "5w", "6w", "7b"]
        f = features(hand, "南", 0, None, {"b_pair": 25, "b_progress": 180})
        self.assertEqual(f["hon_iso"], 1)
        self.assertEqual(f["isolated"], 3)      # 5t、9t、7b 各自孤立
        f = features(hand, "东", 0, None, {"b_pair": 25, "b_progress": 180})
        self.assertEqual(f["hon_trip"], 1)
        self.assertEqual(set(f), set(FD_FEATURES))


class DealerD0SwitchTests(unittest.TestCase):
    def test_dealer_d0_switch(self):
        from mj.ev import choose_route_discard
        on = {"rule_fitted_discard_enabled": 1, "fd_uke": 100, "fd_hon_iso": -5000}   # 拟合打分：死也不打孤字
        diff = 0
        for hand, _ in random_hands(300, 14, 4):
            fitted = choose_route_discard(hand, 0, 0, 0, {"dealer_hint": True, "_weights": on}, None)
            old = choose_route_discard(hand, 0, 0, 0, {"dealer_hint": True, "_weights": dict(
                on, rule_fitted_discard_dealer_d0_enabled=0)}, None)
            base = choose_route_discard(hand, 0, 0, 0, {"dealer_hint": True}, None)
            self.assertEqual(old, base, hand)          # 开关=0 → 与不开拟合时完全一致
            diff += fitted != old
            # 闲家路径（无 dealer_hint）不受开关影响
            self.assertEqual(choose_route_discard(hand, 0, 0, 0, {"_weights": dict(
                on, rule_fitted_discard_dealer_d0_enabled=0)}, None),
                choose_route_discard(hand, 0, 0, 0, {"_weights": on}, None))
        self.assertGreater(diff, 0)


if __name__ == "__main__":
    unittest.main()
