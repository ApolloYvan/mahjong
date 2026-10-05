import json
import os
import tempfile
import unittest

from mj.replay_analysis import replay


class ReplayAnalysisTests(unittest.TestCase):
    def test_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "g.json")
            with open(source, "w", encoding="utf-8") as output:
                json.dump({"blocks": [{"events": [{"ts": 1, "type": "tile_discarded"}, {"ts": 3, "type": "timeout", "data": {"kind": "response"}}]}]}, output)
            report = replay(os.path.join(directory, "*.json"), os.path.join(directory, "r.json"))
            self.assertEqual(report["gap_seconds"]["max"], 2)
            self.assertEqual(report["timeouts"]["response"], 1)
