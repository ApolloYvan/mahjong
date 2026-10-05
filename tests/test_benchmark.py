import unittest

from mj.benchmark import compare, wilson


class BenchmarkTests(unittest.TestCase):
    def test_wilson_range(self):
        low, high = wilson(50, 100)
        self.assertLess(low, 0.5)
        self.assertGreater(high, 0.5)

    def test_compare(self):
        import json
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", encoding="utf-8", delete=False) as output:
            json.dump({"rounds": 10, "stats": {"baseline_wins": 7, "baseline_draw_total": 20, "baseline_fan_total": 10, "route_wins": 8, "route_draw_total": 18, "route_fan_total": 12}}, output)
            path = output.name
        result = compare(path)
        self.assertEqual(result["baseline"]["wins"], 7)
        self.assertEqual(result["route"]["wins"], 8)
