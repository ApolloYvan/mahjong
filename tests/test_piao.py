import json
import os
import tempfile
import unittest

from mj.piao import estimate


class PiaoTests(unittest.TestCase):
    def test_piao_attempt_success_requires_later_hu(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "x.jsonl")
            records = [
                {"kind": "piao_attempt", "payload": {"game_id": "g", "seat": 1, "dealer": True, "god": {"chain_count": 1}, "chain_count": 1}},
                {"kind": "decision", "payload": {"game_id": "g", "seat": 1, "decision": {"action": "hu", "tile": "白"}}},
                {"kind": "result", "payload": {"game_id": "g", "state": {"snapshot": {"phase": "finished"}}}},
            ]
            with open(path, "w", encoding="utf-8") as output:
                for record in records:
                    json.dump(record, output)
                    output.write("\n")
            result = estimate(path, os.path.join(directory, "rate.json"))
            self.assertEqual(result["attempts"], 1)
            self.assertEqual(result["successes"], 1)
            self.assertIn("dealer|1|1", result["groups"])

    def test_attempt_without_hu_is_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "x.jsonl")
            records = [
                {"kind": "piao_attempt", "payload": {"game_id": "g", "seat": 1, "dealer": False, "god": {}, "chain_count": 0}},
                {"kind": "result", "payload": {"game_id": "g", "state": {"snapshot": {"phase": "finished"}}}},
            ]
            with open(path, "w", encoding="utf-8") as output:
                for record in records:
                    json.dump(record, output)
                    output.write("\n")
            result = estimate(path, os.path.join(directory, "rate.json"))
            self.assertEqual(result["attempts"], 1)
            self.assertEqual(result["successes"], 0)

    def test_unresolved_game_neither_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "x.jsonl")
            with open(path, "w", encoding="utf-8") as output:
                json.dump({"kind": "piao_attempt", "payload": {"game_id": "g", "seat": 0, "god": {}, "chain_count": 0}}, output)
                output.write("\n")
            result = estimate(path, os.path.join(directory, "rate.json"))
            self.assertEqual(result["attempts"], 1)
            self.assertEqual(result["successes"], 0)
