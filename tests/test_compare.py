import json
import os
import tempfile
import unittest

from mj.compare import compare


class CompareTests(unittest.TestCase):
    def test_compare_empty_without_hand(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "g.json")
            with open(path, "w", encoding="utf-8") as output:
                json.dump({"blocks": [{"events": [{"type": "tile_discarded", "tile": "1w"}]}]}, output)
            report = compare(os.path.join(directory, "*.json"), os.path.join(directory, "r.json"))
            self.assertEqual(report["samples"], 0)
