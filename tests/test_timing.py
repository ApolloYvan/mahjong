import json
import os
import tempfile
import unittest

from mj.timing import analyze


def _write_jsonl(path, records):
    with open(path, "w", encoding="utf-8") as output:
        for record in records:
            output.write(json.dumps(record) + "\n")


class TimingTests(unittest.TestCase):
    def test_legacy_state_and_notify_matching_still_supported(self):
        """向后兼容：早于 state_request_metric 引入的历史日志（kind="state"/
        "notify"）仍可分析，落在 legacy_* 字段下。"""
        with tempfile.TemporaryDirectory() as directory:
            logs = os.path.join(directory, "x.jsonl")
            records = [
                {"kind": "state", "payload": {"game_id": "room_a_g", "requested_seq": 4,
                                               "pending": False, "gap": False, "state_request_ms": 12}},
                {"kind": "notify", "payload": {"game_id": "room_a_g", "seq": 4}},
            ]
            _write_jsonl(logs, records)
            report = analyze(os.path.join(directory, "*.jsonl"), os.path.join(directory, "*.json"),
                              os.path.join(directory, "r.json"), room_id="room_a")
            self.assertEqual(report["state_timing"]["legacy_requests"], 1)
            self.assertEqual(report["notify_timing"]["notify_to_state_matches"], 1)

    def test_room_filter_scopes_decisions_and_own_timeouts(self):
        with tempfile.TemporaryDirectory() as directory:
            logs = os.path.join(directory, "x.jsonl")
            with open(logs, "w", encoding="utf-8") as output:
                for room in ("room_a", "room_b"):
                    output.write(json.dumps({
                        "kind": "decision",
                        "payload": {"game_id": room + "_g", "seat": 1, "decision": {"action": "pass"}},
                    }) + "\n")
            events = os.path.join(directory, "room_a_0.json")
            with open(events, "w", encoding="utf-8") as output:
                json.dump({
                    "game_id": "room_a_g",
                    "blocks": [{"events": [
                        {"type": "timeout", "seat": 1, "data": {"kind": "response"}},
                        {"type": "timeout", "seat": 2, "data": {"kind": "response"}},
                    ]}],
                }, output)
            report = analyze(os.path.join(directory, "*.jsonl"), os.path.join(directory, "*.json"),
                              os.path.join(directory, "r.json"), room_id="room_a")
            self.assertEqual(report["accepted_actions"], 1)
            # 独立复核 P1 要求：own vs all-player timeout 分离——本 AI 座位
            # 是 seat=1（从 decision 记录动态识别），seat=2 是对手，不计入
            # own_response_timeouts。
            self.assertEqual(report["own_response_timeouts"], 1)
            self.assertEqual(report["all_player_response_timeouts"], 2)
            self.assertEqual(report["my_seat_by_game"]["room_a_g"], 1)

    def test_dynamic_seat_identification_not_hardcoded(self):
        """规则：不能假设固定 seat——同一 game_id 下出现频率最高的 seat
        被识别为"我方座位"，不同 game_id 可以有不同的座位号。"""
        with tempfile.TemporaryDirectory() as directory:
            logs = os.path.join(directory, "x.jsonl")
            records = [
                {"kind": "decision", "payload": {"game_id": "g1", "seat": 3, "decision": {"action": "discard"}}},
                {"kind": "decision", "payload": {"game_id": "g2", "seat": 0, "decision": {"action": "discard"}}},
            ]
            _write_jsonl(logs, records)
            report = analyze(os.path.join(directory, "*.jsonl"), os.path.join(directory, "*.json"),
                              os.path.join(directory, "r.json"))
            self.assertEqual(report["my_seat_by_game"]["g1"], 3)
            self.assertEqual(report["my_seat_by_game"]["g2"], 0)

    def test_409_count_rate_and_reason_breakdown(self):
        with tempfile.TemporaryDirectory() as directory:
            logs = os.path.join(directory, "x.jsonl")
            records = [
                {"kind": "decision_attempt", "payload": {"game_id": "g1", "decision_id": "d1", "seat": 0}},
                {"kind": "decision_attempt", "payload": {"game_id": "g1", "decision_id": "d2", "seat": 0}},
                {"kind": "decision_attempt", "payload": {"game_id": "g1", "decision_id": "d3", "seat": 0}},
                {"kind": "decision", "payload": {"game_id": "g1", "seat": 0, "decision": {"action": "discard"}}},
                {"kind": "action_rejected", "payload": {
                    "game_id": "g1", "error": '{"code":"INVALID_ACTION","message":"not your response turn"}'}},
                {"kind": "action_rejected", "payload": {
                    "game_id": "g1", "error": '{"code":"INVALID_ACTION","message":"cannot gang 4b"}'}},
                {"kind": "action_rejected", "payload": {
                    "game_id": "g1", "error": '{"code":"INVALID_ACTION","message":"some unknown rule"}'}},
            ]
            _write_jsonl(logs, records)
            report = analyze(os.path.join(directory, "*.jsonl"), os.path.join(directory, "*.json"),
                              os.path.join(directory, "r.json"))
            self.assertEqual(report["action_attempts"], 3)
            self.assertEqual(report["accepted_actions"], 1)
            self.assertEqual(report["rejected_409_count"], 3)
            self.assertAlmostEqual(report["rejected_409_rate"], 1.0)
            self.assertEqual(report["rejected_409_reason_breakdown"]["not_your_response_turn"], 1)
            self.assertEqual(report["rejected_409_reason_breakdown"]["illegal_gang"], 1)
            self.assertEqual(report["rejected_409_reason_breakdown"]["other"], 1)

    def test_no_misleading_timeout_ratio_field(self):
        """独立复核 P1 明确要求：删除误导性的旧 timeout_ratio 字段。"""
        with tempfile.TemporaryDirectory() as directory:
            logs = os.path.join(directory, "x.jsonl")
            _write_jsonl(logs, [])
            report = analyze(os.path.join(directory, "*.jsonl"), os.path.join(directory, "*.json"),
                              os.path.join(directory, "r.json"))
            self.assertNotIn("timeout_ratio", report)
            self.assertNotIn("server_timeout_by_kind_and_seat", report)

    def test_action_and_state_percentiles_include_p99_and_max(self):
        with tempfile.TemporaryDirectory() as directory:
            logs = os.path.join(directory, "x.jsonl")
            records = [
                {"kind": "decision", "payload": {"game_id": "g1", "seat": 0,
                                                  "decision": {"action": "discard"},
                                                  "action_request_ms": ms, "client_prepare_ms": 1}}
                for ms in [100, 200, 300, 400, 500, 600, 700, 800, 900, 2000]
            ]
            _write_jsonl(logs, records)
            report = analyze(os.path.join(directory, "*.jsonl"), os.path.join(directory, "*.json"),
                              os.path.join(directory, "r.json"))
            timing = report["client_timing"]["action_request_ms"]
            self.assertIn("p50", timing)
            self.assertIn("p95", timing)
            self.assertIn("p99", timing)
            self.assertIn("max", timing)
            self.assertEqual(timing["max"], 2000)

    def test_notify_utilization_and_trigger_counts_from_state_request_metric(self):
        """P1 修复核心：state_request_metric 驱动的 notify 利用率/
        watchdog/recovery 计数（不再依赖恒为 0 的 notify_to_state_matches）。"""
        with tempfile.TemporaryDirectory() as directory:
            logs = os.path.join(directory, "x.jsonl")
            records = [
                {"kind": "state_request_metric", "payload": {
                    "game_id": "g1", "requested_seq": 0, "returned_seq": 1,
                    "trigger": "recovery", "elapsed_ms": 50, "pending": False,
                    "gap": False, "state_hash": "h1"}},
                {"kind": "state_request_metric", "payload": {
                    "game_id": "g1", "requested_seq": 0, "returned_seq": 2,
                    "trigger": "notify", "elapsed_ms": 30, "pending": False,
                    "gap": False, "state_hash": "h2"}},
                {"kind": "state_request_metric", "payload": {
                    "game_id": "g1", "requested_seq": 0, "returned_seq": 2,
                    "trigger": "watchdog", "elapsed_ms": 40, "pending": False,
                    "gap": False, "state_hash": "h2"}},
                {"kind": "state_request_metric", "payload": {
                    "game_id": "g1", "requested_seq": 0, "returned_seq": None,
                    "trigger": "recovery", "elapsed_ms": 20, "pending": False,
                    "gap": True, "state_hash": None}},
            ]
            _write_jsonl(logs, records)
            report = analyze(os.path.join(directory, "*.jsonl"), os.path.join(directory, "*.json"),
                              os.path.join(directory, "r.json"))
            state_timing = report["state_timing"]
            self.assertEqual(state_timing["requests"], 4)
            self.assertEqual(state_timing["trigger_counts"]["notify"], 1)
            self.assertEqual(state_timing["trigger_counts"]["watchdog"], 1)
            self.assertEqual(state_timing["trigger_counts"]["recovery"], 2)
            self.assertEqual(state_timing["notify_triggered_requests"], 1)
            self.assertEqual(state_timing["watchdog_triggered_requests"], 1)
            self.assertEqual(state_timing["recovery_triggered_requests"], 2)
            self.assertAlmostEqual(state_timing["notify_utilization"], 0.25)
            self.assertEqual(state_timing["gap_count"], 1)

    def test_conflict_409_then_fallback_sent_modeled_as_transition_not_duplicate(self):
        """规则6：同一 decision_id 先 conflict_409 后 fallback_sent，必须
        建模为该决策的事件转换历史（最终态取最后一条），不能被误认为
        重复脏数据而被去重成 0 条或报错。"""
        with tempfile.TemporaryDirectory() as directory:
            logs = os.path.join(directory, "x.jsonl")
            records = [
                {"kind": "decision_attempt", "payload": {"game_id": "g1", "decision_id": "d1", "seat": 0}},
                {"kind": "decision_attempt_outcome", "payload": {
                    "decision_id": "d1", "outcome": "conflict_409"}},
                {"kind": "decision_attempt_outcome", "payload": {
                    "decision_id": "d1", "outcome": "fallback_sent"}},
            ]
            _write_jsonl(logs, records)
            report = analyze(os.path.join(directory, "*.jsonl"), os.path.join(directory, "*.json"),
                              os.path.join(directory, "r.json"))
            # 最终态只应统计一次 fallback_sent（取该 decision_id 最后一条），
            # 不是 conflict_409=1 且 fallback_sent=1 两条都独立计入"决策数"。
            self.assertEqual(report["final_outcome_breakdown"], {"fallback_sent": 1})

    def test_decision_attempt_outcome_without_game_id_resolved_via_decision_attempt(self):
        """P1 修复：decision_attempt_outcome 可以不带 game_id（历史日志），
        通过 decision_id 反查 decision_attempt 记录的 game_id 来正确归属
        与过滤——历史日志仍能正常导入分析。"""
        with tempfile.TemporaryDirectory() as directory:
            logs = os.path.join(directory, "x.jsonl")
            records = [
                {"kind": "decision_attempt", "payload": {"game_id": "room_a_g", "decision_id": "d1", "seat": 0}},
                # 历史日志格式：outcome 记录没有 game_id 字段。
                {"kind": "decision_attempt_outcome", "payload": {"decision_id": "d1", "outcome": "success"}},
            ]
            _write_jsonl(logs, records)
            report = analyze(os.path.join(directory, "*.jsonl"), os.path.join(directory, "*.json"),
                              os.path.join(directory, "r.json"), room_id="room_a")
            self.assertEqual(report["final_outcome_breakdown"], {"success": 1})

    def test_fallback_success_rate(self):
        with tempfile.TemporaryDirectory() as directory:
            logs = os.path.join(directory, "x.jsonl")
            records = [
                {"kind": "fallback_sent", "payload": {"game_id": "g1", "accepted": True}},
                {"kind": "fallback_sent", "payload": {"game_id": "g1", "accepted": False}},
            ]
            _write_jsonl(logs, records)
            report = analyze(os.path.join(directory, "*.jsonl"), os.path.join(directory, "*.json"),
                              os.path.join(directory, "r.json"))
            self.assertEqual(report["fallback_count"], 2)
            self.assertAlmostEqual(report["fallback_success_rate"], 0.5)


if __name__ == "__main__":
    unittest.main()
