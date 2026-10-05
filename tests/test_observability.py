"""决策可观测性基础设施测试。"""
import unittest

from mj.observability import (
    config_hash,
    local_estimate_marker,
    new_decision_id,
    server_truth_marker,
    should_log_full,
    state_hash,
)
from mj.state import normalize


class HashDeterminismTests(unittest.TestCase):
    def test_config_hash_is_deterministic(self):
        rules = {"YouCaiBiKao": True, "M": 10}
        self.assertEqual(config_hash(rules), config_hash(dict(rules)))

    def test_config_hash_differs_for_different_config(self):
        self.assertNotEqual(config_hash({"a": 1}), config_hash({"a": 2}))

    def test_config_hash_key_order_independent(self):
        self.assertEqual(config_hash({"a": 1, "b": 2}), config_hash({"b": 2, "a": 1}))

    def test_state_hash_deterministic_for_same_logical_state(self):
        snapshot = {
            "seat": 0, "phase": "draw", "turn": 0,
            "my_hand": ["1w", "2w"], "drawn_tile": "3w",
            "dealer": 0, "wall_remaining": 40,
            "god": {"chain_count": 1, "piao": 0},
            "melds": [[], [], [], []],
        }
        state_a = normalize(dict(snapshot))
        state_b = normalize(dict(snapshot))
        self.assertEqual(state_hash(state_a), state_hash(state_b))

    def test_state_hash_differs_when_hand_changes(self):
        base = {
            "seat": 0, "phase": "draw", "turn": 0,
            "drawn_tile": "3w", "dealer": 0, "wall_remaining": 40,
            "god": {}, "melds": [[], [], [], []],
        }
        state_a = normalize({**base, "my_hand": ["1w", "2w"]})
        state_b = normalize({**base, "my_hand": ["1w", "9w"]})
        self.assertNotEqual(state_hash(state_a), state_hash(state_b))


class DecisionIdTests(unittest.TestCase):
    def test_decision_ids_are_unique(self):
        ids = {new_decision_id() for _ in range(1000)}
        self.assertEqual(len(ids), 1000)


class SamplingTests(unittest.TestCase):
    def test_abnormal_always_logs_full(self):
        for _ in range(20):
            self.assertTrue(should_log_full(is_abnormal=True, sample_rate=0.0))

    def test_normal_respects_sample_rate_zero(self):
        for _ in range(20):
            self.assertFalse(should_log_full(is_abnormal=False, sample_rate=0.0))

    def test_normal_respects_sample_rate_one(self):
        for _ in range(20):
            self.assertTrue(should_log_full(is_abnormal=False, sample_rate=1.0))


class SourceMarkerTests(unittest.TestCase):
    def test_local_estimate_and_server_truth_are_distinguishable(self):
        self.assertNotEqual(local_estimate_marker(), server_truth_marker())
        self.assertEqual(local_estimate_marker()["source"], "local_estimate")
        self.assertEqual(server_truth_marker()["source"], "server_truth")


if __name__ == "__main__":
    unittest.main()
