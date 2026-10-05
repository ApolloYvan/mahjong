import json
import os
import tempfile
import unittest

from mj.fit import fit, load_weights


class FitTests(unittest.TestCase):
    def test_fit_prefers_baseline_when_route_loses(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "s.json")
            with open(source, "w", encoding="utf-8") as output:
                json.dump([{"baseline_match": True, "route_match": False}], output)
            report = fit(source, os.path.join(directory, "weights.json"))
            self.assertEqual(report["weights"]["pair"], 20)
            self.assertEqual(report["samples"], 1)

    def test_load_weights_missing_uses_defaults(self):
        weights = load_weights("missing-weights.json")
        self.assertEqual(weights["shanten"], 10000)
