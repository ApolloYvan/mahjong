"""tools/session_guard.py 回归测试（2026-09-22 进程存活可观测性修复，§3.5）。"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from tools.session_guard import live_check


def _write_log(path, records):
    with open(path, "w", encoding="utf-8") as handle:
        for rec in records:
            handle.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _decision(time_str, game_id):
    return {"time": time_str, "kind": "decision", "payload": {"game_id": game_id}}


def _finished_result(time_str, game_id):
    return {
        "time": time_str,
        "kind": "result",
        "payload": {"game_id": game_id, "state": {"snapshot": {"phase": "finished"}}},
    }


class TestSessionGuardLiveCheck(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.log_path = os.path.join(self._tmp.name, "2026-09-22.jsonl")
        self.now = datetime(2026, 9, 22, 16, 20, 0, tzinfo=timezone.utc)

    def test_silent_90s_and_unfinished_is_degraded(self):
        last = self.now - timedelta(seconds=200)
        _write_log(self.log_path, [
            _decision(last.isoformat(), "a_282b85347354_r1_b0_t0"),
        ])
        result = live_check(self.log_path, stale_seconds=90, now=self.now)
        self.assertTrue(result["a_282b85347354"])

    def test_continuously_updating_is_healthy(self):
        last = self.now - timedelta(seconds=5)
        _write_log(self.log_path, [
            _decision((self.now - timedelta(seconds=200)).isoformat(), "a_healthy_r1_b0_t0"),
            _decision(last.isoformat(), "a_healthy_r1_b0_t0"),
        ])
        result = live_check(self.log_path, stale_seconds=90, now=self.now)
        self.assertFalse(result["a_healthy"])

    def test_finished_room_healthy_even_if_silent(self):
        finished_at = self.now - timedelta(seconds=500)
        _write_log(self.log_path, [
            _decision((finished_at - timedelta(seconds=30)).isoformat(), "a_done_r1_b0_t0"),
            _finished_result(finished_at.isoformat(), "a_done_r1_b0_t0"),
        ])
        result = live_check(self.log_path, stale_seconds=90, now=self.now)
        self.assertFalse(result["a_done"])

    def test_main_returns_nonzero_exit_code_when_degraded(self):
        from tools.session_guard import main
        import sys

        # 用远早于任何真实运行时刻的时间戳，不依赖测试环境的系统时钟与
        # self.now 恰好接近——main() 内部用真实 datetime.now()。
        ancient = datetime(2020, 1, 1, tzinfo=timezone.utc)
        _write_log(self.log_path, [
            _decision(ancient.isoformat(), "a_282b85347354_r1_b0_t0"),
        ])
        argv = sys.argv
        sys.argv = ["session_guard.py", "--logs", self.log_path]
        try:
            exit_code = main()
        finally:
            sys.argv = argv
        self.assertEqual(exit_code, 1)


if __name__ == "__main__":
    unittest.main()
