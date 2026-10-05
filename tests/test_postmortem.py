"""tools/postmortem.py 回归测试：脱敏且体积受控的 fixture（2 场、共 3 局，
含 1 次流局），覆盖真实实战数据暴露的两个根因——
1. 已移除私有函数 mj.responses._claim_worthit 不得被重新导入/恢复；
2. 无参数/room-id 缺失时必须给出清晰非零退出码，不得默认为任何旧房间。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from tools.postmortem import EXIT_MISSING_ROOM_ID, EXIT_NO_EVIDENCE, EXIT_OK, build_postmortem


def _write_jsonl(path, records):
    with open(path, "w", encoding="utf-8") as output:
        for record in records:
            output.write(json.dumps(record, ensure_ascii=False) + "\n")


def _write_event_file(path, game_id, seats, round_events):
    blocks = [{"events": [
        {"type": "round_ended", "seat": ev.get("winner_seat", -1) if not ev.get("is_draw") else -1,
         "data": {"round_no": ev["round_no"], "draw": ev.get("is_draw", False),
                   "scores": ev.get("scores", [])}}
        for ev in round_events
    ]}]
    with open(path, "w", encoding="utf-8") as output:
        json.dump({"game_id": game_id, "seats": seats, "blocks": blocks}, output, ensure_ascii=False)


class PostmortemNoLegacyImportTests(unittest.TestCase):
    def test_module_does_not_import_claim_worthit(self):
        """根因1回归：postmortem.py 不得依赖已删除的私有函数
        mj.responses._claim_worthit（导入即崩溃，本次修复要求删除依赖，
        不恢复该私有旧函数）。只检查真正的 import 语句，不检查说明文字
        （文档字符串里提及这个名字用于说明"删除了什么"是允许的）。"""
        source_path = os.path.join(REPO_ROOT, "tools", "postmortem.py")
        with open(source_path, encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if stripped.startswith("import ") or stripped.startswith("from "):
                    self.assertNotIn("_claim_worthit", stripped)

    def test_claim_worthit_indeed_removed_from_responses_module(self):
        from mj import responses
        self.assertFalse(hasattr(responses, "_claim_worthit"))


class PostmortemExplicitRoomIdRequiredTests(unittest.TestCase):
    def test_no_args_gives_clean_nonzero_exit(self):
        """根因2回归：无参数时必须给出清晰非零退出码，不得默认为任何
        旧房间（如历史遗留的 t_65d538e905c5）。"""
        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "tools", "postmortem.py")],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, EXIT_MISSING_ROOM_ID)
        self.assertNotEqual(result.returncode, 0)

    def test_build_postmortem_rejects_empty_room_id(self):
        with self.assertRaises(ValueError):
            build_postmortem("")
        with self.assertRaises(ValueError):
            build_postmortem(None)


class PostmortemRealShapedFixtureTests(unittest.TestCase):
    """脱敏且体积受控的 fixture：2 场、共 3 局（1 胜 1 负 1 流局），
    字段结构对齐真实生产日志/事件形态（真实字段名，假 ID）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.logs_dir = os.path.join(self._tmp.name, "logs")
        self.events_dir = os.path.join(self._tmp.name, "events")
        os.makedirs(self.logs_dir)
        os.makedirs(self.events_dir)
        self.room_id = "a_fixture0001"
        self.game1 = f"{self.room_id}_r1_b0_t0"
        self.game2 = f"{self.room_id}_r1_b1_t0"

        _write_jsonl(os.path.join(self.logs_dir, "x.jsonl"), [
            {"kind": "decision", "payload": {"game_id": self.game1, "seat": 1,
                                              "decision": {"action": "discard", "tile": "1w"}}},
            {"kind": "decision", "payload": {"game_id": self.game1, "seat": 1,
                                              "decision": {"action": "hu"}}},
            {"kind": "decision_attempt", "payload": {"game_id": self.game1, "decision_id": "d1", "seat": 1}},
            {"kind": "decision_attempt_outcome", "payload": {"decision_id": "d1", "outcome": "success"}},
            {"kind": "decision_attempt", "payload": {"game_id": self.game1, "decision_id": "d2", "seat": 1}},
            {"kind": "decision_attempt_outcome", "payload": {"decision_id": "d2", "outcome": "conflict_409"}},
            {"kind": "decision_attempt_outcome", "payload": {"decision_id": "d2", "outcome": "fallback_sent"}},
            {"kind": "action_rejected", "payload": {
                "game_id": self.game1,
                "error": '{"code":"INVALID_ACTION","message":"not your response turn"}'}},
            {"kind": "decision", "payload": {"game_id": self.game2, "seat": 2,
                                              "decision": {"action": "discard", "tile": "9b"}}},
        ])

        _write_event_file(
            os.path.join(self.events_dir, self.game1 + ".json"), self.game1,
            seats=[{"user_id": "u_a"}, {"user_id": "u_me"}, {"user_id": "u_c"}, {"user_id": "u_d"}],
            round_events=[
                {"round_no": 1, "is_draw": False, "winner_seat": 1, "scores": [-10, 30, -10, -10]},
                {"round_no": 2, "is_draw": True, "scores": [0, 0, 0, 0]},
            ],
        )
        _write_event_file(
            os.path.join(self.events_dir, self.game2 + ".json"), self.game2,
            seats=[{"user_id": "u_a"}, {"user_id": "u_c"}, {"user_id": "u_me"}, {"user_id": "u_d"}],
            round_events=[
                {"round_no": 1, "is_draw": False, "winner_seat": 0, "scores": [30, -10, -10, -10]},
            ],
        )

    def test_build_postmortem_over_fixture_matches_expected_rounds_and_outcome(self):
        report = build_postmortem(
            self.room_id,
            log_pattern=os.path.join(self.logs_dir, "*.jsonl"),
            event_pattern=os.path.join(self.events_dir, "*.json"),
        )
        self.assertTrue(report["has_evidence"])
        self.assertEqual(report["rounds_analyzed"], 3)
        self.assertEqual(report["draws"], 1)
        # 本 AI 在 game1 座位1赢一局（+30），在 game2 座位2输一局（-10）。
        self.assertEqual(report["my_wins"], 1)
        self.assertEqual(report["my_losses"], 1)
        self.assertEqual(report["my_user_id"], "u_me")
        self.assertEqual(report["accepted_actions"], 3)
        self.assertEqual(report["decision_attempts"], 2)
        self.assertEqual(report["final_outcome_breakdown"], {"success": 1, "fallback_sent": 1})
        self.assertEqual(report["rejected_409_count"], 1)
        self.assertEqual(report["rejected_409_reason_breakdown"], {"not_your_response_turn": 1})

    def test_cli_exits_zero_on_real_shaped_fixture(self):
        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "tools", "postmortem.py"), self.room_id,
             self.logs_dir + "/*.jsonl", self.events_dir + "/*.json"],
            cwd=self._tmp.name, capture_output=True, text=True, timeout=30,
            env={**os.environ, "PYTHONPATH": REPO_ROOT},
        )
        self.assertEqual(result.returncode, EXIT_OK, msg=result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["rounds_analyzed"], 3)
        self.assertEqual(report["draws"], 1)

    def test_cli_no_matching_evidence_gives_no_evidence_exit_code(self):
        """CLI 默认读取 logs/*.jsonl 与 models/events/*.json（相对 cwd）；
        换一个真实不存在的房间号、且 cwd 下没有任何 logs/models 子目录时，
        必须给出 EXIT_NO_EVIDENCE，不是 0，也不是崩溃。"""
        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "tools", "postmortem.py"), "a_no_such_room"],
            cwd=self._tmp.name, capture_output=True, text=True, timeout=30,
            env={**os.environ, "PYTHONPATH": REPO_ROOT},
        )
        self.assertEqual(result.returncode, EXIT_NO_EVIDENCE)


if __name__ == "__main__":
    unittest.main()
