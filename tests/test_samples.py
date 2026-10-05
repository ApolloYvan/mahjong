import json
import os
import tempfile
import unittest

from mj.samples import build_samples


class SamplesTests(unittest.TestCase):
    def test_dict_melds_count_uses_current_seat(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "x.jsonl")
            with open(path, "w", encoding="utf-8") as output:
                json.dump({"kind": "decision", "payload": {
                    "seat": 1,
                    "hand": ["1w", "2w", "3w", "4w"],
                    "melds": {"0": [{"kind": "chi"}], "1": []},
                    "decision": {"action": "discard", "tile": "4w"},
                }}, output)
                output.write("\n")
            samples = build_samples(path, os.path.join(directory, "out.json"))
            self.assertEqual(len(samples), 1)
