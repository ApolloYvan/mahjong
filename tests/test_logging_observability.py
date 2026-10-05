"""DecisionLog 可复现观测字段测试（阶段2）：schema_version、decision_id、
config_hash、state_hash 必须写入日志，且 decision_id 可跨条目复用。
"""
import json
import os
import tempfile
import unittest

from mj.logging import DecisionLog
from mj.observability import config_hash, new_decision_id


class DecisionLogObservabilityTests(unittest.TestCase):
    def _read_lines(self, directory):
        path = os.path.join(directory, os.listdir(directory)[0])
        with open(path, encoding="utf-8") as source:
            return [json.loads(line) for line in source if line.strip()]

    def test_action_writes_schema_and_hash_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            log = DecisionLog(directory=directory)
            snapshot = {
                "seat": 0, "phase": "draw", "turn": 0, "my_hand": ["1w", "2w"],
                "drawn_tile": "3w", "dealer": 0, "wall_remaining": 40,
                "god": {"chain_count": 1}, "melds": [[], [], [], []],
            }
            decision_id = log.action("g1", snapshot, {"action": "discard", "tile": "1w"})
            records = self._read_lines(directory)
            payload = records[0]["payload"]
            self.assertEqual(payload["schema_version"], 2)
            self.assertEqual(payload["policy_version"], "legacy")
            self.assertEqual(payload["decision_id"], decision_id)
            self.assertIn("config_hash", payload)
            self.assertIn("state_hash", payload)

    def test_action_accepts_explicit_decision_id_for_cross_entry_linking(self):
        with tempfile.TemporaryDirectory() as directory:
            log = DecisionLog(directory=directory)
            snapshot = {"seat": 0, "phase": "draw", "my_hand": [], "melds": [[], [], [], []]}
            fixed_id = new_decision_id()
            returned = log.action("g1", snapshot, {"action": "pass"}, decision_id=fixed_id)
            self.assertEqual(returned, fixed_id)
            records = self._read_lines(directory)
            self.assertEqual(records[0]["payload"]["decision_id"], fixed_id)

    def test_config_hash_reflects_passed_rules_not_snapshot_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            log = DecisionLog(directory=directory)
            snapshot = {"seat": 0, "phase": "draw", "my_hand": [], "melds": [[], [], [], []],
                       "rules": {"stale": True}}
            log.action("g1", snapshot, {"action": "pass"}, rules={"fresh": True})
            records = self._read_lines(directory)
            self.assertEqual(records[0]["payload"]["config_hash"], config_hash({"fresh": True}))


if __name__ == "__main__":
    unittest.main()
