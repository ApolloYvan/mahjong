import json
import os
import tempfile
import unittest

from mj.dataset import extract_file
from mj.report import build_report


class DataTests(unittest.TestCase):
    def test_extract_and_report(self):
        with tempfile.TemporaryDirectory() as directory:
            events = os.path.join(directory, "g.json")
            with open(events, "w", encoding="utf-8") as output:
                json.dump({"game_id": "g1", "blocks": [{"events": [{"type": "tile_discarded", "tile": "1w"}]}], "rounds": [{"action": "hu", "score": 8, "seat": 0}]}, output)
            dataset = os.path.join(directory, "d.json")
            self.assertEqual(extract_file(events, dataset), 1)
            report = build_report(events, os.path.join(directory, "report.json"))
            self.assertEqual(report["games"], 1)
            self.assertEqual(report["events"]["tile_discarded"], 1)
