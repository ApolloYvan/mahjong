"""P0-1 端到端验收测试：证明超时（TimeoutError）后，尝试过的动作及其结果
能够从落盘的 JSONL 日志和导入后的 SQLite 数据库中被精确还原。

覆盖链路：mj.bot.play_game（TimeoutError 分支）-> DecisionLog（同步落盘到
临时目录）-> mj.data_import.import_decision_log_stream（导入 SQLite）->
mj.datastore.get_decision_attempt（按 decision_id 查询还原）。

不访问真实网络：api 为 MagicMock，state()/action() 均为本地构造的假响应。
"""
import glob
import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch

from mj import data_import as di
from mj import datastore as ds
from mj.api import ApiError
from mj.bot import play_game
from mj.logging import DecisionLog


class TimeoutDecisionAttemptReconstructionTests(unittest.TestCase):
    def _api(self, states):
        api = MagicMock()
        api.rules.return_value = {}
        api.notify.return_value = MagicMock()
        api.notify.return_value.__iter__ = Mock(side_effect=TypeError("not iterable"))
        api.state.side_effect = states
        return api

    def test_timeout_attempt_and_outcome_reconstructable_from_jsonl_and_sqlite(self):
        snapshot = {
            "phase": "draw", "seat": 0, "turn": 0, "round_no": 7,
            "my_hand": ["1w"] * 13 + ["2w"], "drawn_tile": "2w",
            "melds": [[], [], [], []], "wall_remaining": 40,
        }
        api = self._api([{"snapshot": snapshot},
                        {"finished": True, "snapshot": {"phase": "finished", "seat": 0}}])
        api.action.side_effect = TimeoutError()

        with tempfile.TemporaryDirectory() as directory:
            logs_dir = os.path.join(directory, "logs")
            log = DecisionLog(directory=logs_dir)

            with patch("mj.bot.choose_discard", return_value={"action": "discard", "tile": "1w"}):
                play_game(api, "g_timeout_1", log, rules={})

            # --- 阶段1：直接从原始 JSONL 文件还原（不经过 SQLite）。 ---
            paths = glob.glob(os.path.join(logs_dir, "*.jsonl"))
            self.assertEqual(len(paths), 1)
            with open(paths[0], encoding="utf-8") as handle:
                records = [json.loads(line) for line in handle if line.strip()]

            attempt_records = [r for r in records if r["kind"] == "decision_attempt"]
            outcome_records = [r for r in records if r["kind"] == "decision_attempt_outcome"]
            self.assertEqual(len(attempt_records), 1)
            self.assertEqual(len(outcome_records), 1)

            attempt_payload = attempt_records[0]["payload"]
            outcome_payload = outcome_records[0]["payload"]
            decision_id = attempt_payload["decision_id"]
            self.assertIsNotNone(decision_id)
            self.assertEqual(attempt_payload["action"], "discard")
            self.assertEqual(attempt_payload["tile"], "1w")
            self.assertEqual(attempt_payload["game_id"], "g_timeout_1")
            self.assertIsNotNone(attempt_payload["policy_version"])
            self.assertIsNotNone(attempt_payload["schema_version"])
            self.assertIsNotNone(attempt_payload["config_hash"])
            self.assertIsNotNone(attempt_payload["build_hash"])
            self.assertEqual(outcome_payload["decision_id"], decision_id)
            self.assertEqual(outcome_payload["outcome"], "timeout")

            # --- 阶段2：导入 SQLite 后同样能按 decision_id 精确还原。 ---
            db_path = os.path.join(directory, "test.sqlite")
            conn = ds.connect(db_path)
            ds.init_schema(conn)
            sha = ds.sha256_file(paths[0])
            fid = ds.insert_source_file(
                conn, path=paths[0], sha256=sha, size_bytes=os.path.getsize(paths[0]),
                mtime=0.0, fmt="decision_jsonl", source_type="local_decision_log",
            )
            conn.commit()
            stats = di.import_decision_log_stream(
                conn, di.open_text_lines(paths[0]), source_file_id=fid, source_sha256=sha,
            )
            ds.mark_source_file_complete(conn, fid, stats.total_lines, stats.success_lines,
                                          stats.failed_lines)
            conn.commit()

            self.assertEqual(stats.decision_attempts_written, 1)
            self.assertEqual(stats.attempt_outcomes_written, 1)

            row = ds.get_decision_attempt(conn, decision_id)
            self.assertIsNotNone(row)
            self.assertEqual(row["game_id"], "g_timeout_1")
            self.assertEqual(row["action"], "discard")
            self.assertEqual(row["tile"], "1w")
            self.assertEqual(row["outcome"], "timeout")
            self.assertIsNotNone(row["build_hash"])
            self.assertIsNotNone(row["attempted_at"])
            self.assertIsNotNone(row["resolved_at"])


if __name__ == "__main__":
    unittest.main()
