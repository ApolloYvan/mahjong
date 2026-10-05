import tempfile
import unittest

from mj.ab import run


class AbTests(unittest.TestCase):
    def test_reproducible(self):
        with tempfile.TemporaryDirectory() as directory:
            first = run(seed=7, rounds=10, output=directory + "/a.json")
            second = run(seed=7, rounds=10, output=directory + "/b.json")
            self.assertEqual(first["stats"], second["stats"])
            self.assertIn("route_shanten_better", first["stats"])
