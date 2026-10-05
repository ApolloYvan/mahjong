import json
import os
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MJ_WEIGHTS_NO_FILE", "1")

import mj.bot as bot  # noqa: E402
from mj.fit import DEFAULT_WEIGHTS, load_weights, thread_weights_overlay, weights_overlay  # noqa: E402


class AbPick(unittest.TestCase):
    def test_deterministic_and_balanced(self):
        picks = [bot._ab_pick(7, "room%d" % i) for i in range(2000)]
        self.assertEqual(picks, [bot._ab_pick(7, "room%d" % i) for i in range(2000)])
        self.assertTrue(900 < sum(picks) < 1100)
        self.assertNotEqual(picks, [bot._ab_pick(8, "room%d" % i) for i in range(2000)])

    def test_load_ab(self):
        with tempfile.TemporaryDirectory() as d:
            a = os.path.join(d, "armA.json")
            with open(a, "w") as f:
                json.dump({"dealer_route_enabled": 0}, f)
            ab = bot._load_ab("%s,current" % a, seed=5)
        self.assertEqual(ab["names"], ("armA", "current"))
        self.assertEqual(ab["overlays"], ({"dealer_route_enabled": 0}, {}))
        self.assertEqual(ab["seed"], 5)
        with self.assertRaises(ValueError):
            bot._load_ab("only_one.json")

    def test_assign_logs_label_seed_overlay(self):
        ab = {"labels": ("A", "B"), "names": ("x", "y"), "overlays": ({"k": 1}, {}), "seed": 3}
        log = MagicMock()
        label, overlay = bot._ab_assign(ab, "r1", log)
        kind, payload = log.append.call_args[0]
        self.assertEqual(kind, "ab_assign")
        self.assertEqual((payload["room_id"], payload["label"], payload["seed"]), ("r1", label, 3))
        self.assertEqual(overlay, ab["overlays"][0 if label == "A" else 1])


class ThreadOverlay(unittest.TestCase):
    def test_unused_is_byte_identical(self):
        self.assertEqual(load_weights(), dict(DEFAULT_WEIGHTS))

    def test_threads_isolated_and_global_untouched(self):
        seen = {}
        barrier = threading.Barrier(2)

        def worker(name, val):
            with thread_weights_overlay({"probe": val}):
                barrier.wait()
                seen[name] = load_weights().get("probe")
                barrier.wait()
        t1 = threading.Thread(target=worker, args=("a", 1))
        t2 = threading.Thread(target=worker, args=("b", 2))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(seen, {"a": 1, "b": 2})
        self.assertNotIn("probe", load_weights())

    def test_thread_overrides_global_overlay(self):
        with weights_overlay({"probe": 1, "other": 5}):
            with thread_weights_overlay({"probe": 2}):
                w = load_weights()
                self.assertEqual((w["probe"], w["other"]), (2, 5))
            self.assertEqual(load_weights()["probe"], 1)

    def test_play_game_applies_overlay_in_its_thread(self):
        got = {}

        def fake_loop(api, gid, log, rules, observer):
            got["v"] = load_weights().get("probe")
        api = MagicMock()
        api.rules.return_value = {}
        with patch.object(bot, "_play_game_loop", side_effect=fake_loop):
            bot.play_game(api, "g1", MagicMock(), {}, {"probe": 9})
            self.assertEqual(got["v"], 9)
            bot.play_game(api, "g2", MagicMock(), {})
            self.assertIsNone(got["v"])


if __name__ == "__main__":
    unittest.main()
