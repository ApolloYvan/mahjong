"""第二次自由对战的脱敏吃碰止损审计。只读取本地日志，不联网。"""
import json
import os
import unittest

from mj.bot import RECOVERY_WATCHDOG_INTERVAL, WATCHDOG_INTERVAL, _SeqTracker
from mj.responses import _meld_groups, claim_assessment, choose_chi, choose_peng
from mj.shanten import pair_route_allowed, shanten
from mj.tiles import to_counts
from tools.postmortem import build_postmortem


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOM = "a_b478b2cbc5db"


def _accepted_claims():
    with open(os.path.join(ROOT, "logs", "2026-09-22.jsonl"), encoding="utf-8") as source:
        for line in source:
            item = json.loads(line)
            if item.get("kind") != "decision":
                continue
            payload = item.get("payload") or {}
            decision = payload.get("decision") or {}
            if not payload.get("game_id", "").startswith(ROOM) or decision.get("action") not in ("chi", "peng"):
                continue
            snapshot = {**payload, "my_hand": payload.get("hand") or [], "window_tile": decision.get("tile")}
            take = decision.get("tiles") if decision["action"] == "chi" else [decision["tile"]] * 2
            yield snapshot, decision, claim_assessment(snapshot, tuple(take))


class ClaimSafetyAuditTests(unittest.TestCase):
    def test_historical_worsening_claims_are_rejected(self):
        worsening = [(snapshot, decision, assessment) for snapshot, decision, assessment in _accepted_claims()
                     if assessment["after_shanten"] > assessment["before_shanten"]]
        self.assertEqual(len(worsening), 2)
        self.assertTrue(all(not assessment["allowed"] for _, _, assessment in worsening))
        for snapshot, decision, _ in worsening:
            selected = choose_chi(snapshot) if decision["action"] == "chi" else choose_peng(snapshot)
            self.assertIsNone(selected)

    def test_claim_comparison_uses_the_same_route_on_both_sides(self):
        """副露永久关闭七对，所以 after 必然走标准路线；before 只有在
        pair_route_allowed 为真时才允许走七对路线。两侧口径不一致会把
        "严格改善标准向听的碰"误判成"变差"——2026-09-22 审计在本房 128 次
        历史声明里抓到 6 次这样的误拒，其中 5 次实际是 first_meld_improves，
        最严重一次标准向听 5→3 被拒。本测试锁死这个口径，任何人退回
        无条件 min(std, pair) 都会立刻红。"""
        for snapshot, _decision, assessment in _accepted_claims():
            counts = to_counts(snapshot["my_hand"])
            mg = _meld_groups(snapshot)
            if not pair_route_allowed(counts, mg):
                self.assertEqual(assessment["before_shanten"], shanten(counts, mg))

    def test_improving_and_second_open_hand_claims_remain_available(self):
        claims = list(_accepted_claims())
        improving = next((snapshot, decision) for snapshot, decision, assessment in claims
                         if assessment["allowed"] and assessment["after_shanten"] < assessment["before_shanten"])
        snapshot, decision = improving
        selected = choose_chi(snapshot) if decision["action"] == "chi" else choose_peng(snapshot)
        self.assertIsNotNone(selected)

        second = next((snapshot, decision) for snapshot, decision, assessment in claims
                      if assessment["allowed"] and len(snapshot["melds"][snapshot["seat"]]) >= 1)
        snapshot, decision = second
        selected = choose_chi(snapshot) if decision["action"] == "chi" else choose_peng(snapshot)
        self.assertIsNotNone(selected)

    def test_seven_pairs_potential_cannot_be_broken_by_neutral_first_peng(self):
        snapshot = {
            "window_tile": "9w",
            "my_hand": ["1w", "1w", "3w", "3w", "5w", "5w", "7w", "7w", "9w", "9w", "2t", "4t", "6t"],
            "melds": [[], [], [], []], "seat": 0,
        }
        self.assertIsNone(choose_peng(snapshot))

    def test_postmortem_reproduces_claim_observations(self):
        report = build_postmortem(ROOM, os.path.join(ROOT, "logs", "*.jsonl"),
                                  os.path.join(ROOT, "models", "events", "*.json"))
        effectiveness = report["claim_effectiveness"]
        self.assertEqual(effectiveness["claim_count_by_round"]["1"],
                         {"rounds": 20, "wins": 0, "net_score": -168})
        self.assertEqual(effectiveness["claim_count_by_round"]["2+"],
                         {"rounds": 45, "wins": 13, "net_score": 60})
        self.assertEqual(effectiveness["route_shanten"],
                         {"improve": 70, "same": 56, "worsen": 2})
        self.assertIn("观察性相关", effectiveness["observation_note"])


class WatchdogSchedulingTests(unittest.TestCase):
    def test_ten_room_healthy_sse_simulation_cuts_watchdog_requests(self):
        trackers = [_SeqTracker() for _ in range(10)]
        for tracker in trackers:
            tracker.set_sse_health(True)
        simulated_seconds = 60
        healthy_requests = sum(simulated_seconds // tracker.watchdog_interval() for tracker in trackers)
        old_requests = 10 * (simulated_seconds // 2)
        self.assertEqual(healthy_requests, 120)
        self.assertLessEqual(healthy_requests, old_requests * 0.5)

    def test_disconnect_recovers_to_one_second_and_notify_wakes_immediately(self):
        tracker = _SeqTracker()
        self.assertEqual(tracker.watchdog_interval(), RECOVERY_WATCHDOG_INTERVAL)
        tracker.set_sse_health(True)
        self.assertEqual(tracker.watchdog_interval(), WATCHDOG_INTERVAL)
        tracker.set_sse_health(False)
        self.assertEqual(tracker.watchdog_interval(), RECOVERY_WATCHDOG_INTERVAL)
        tracker.observe_notify(99)
        self.assertEqual(tracker.next_request(), (0, "recovery"))
        tracker.observe_response(99, trigger="recovery")
        self.assertEqual(tracker.next_request(), (0, "notify"))
