"""P0-3 源头脱敏端到端验收测试：真实反例，证明原始 JSONL、压缩轮转文件、
SQLite 数据库、stdout、stderr 全部不含令牌——脱敏不是只在导入阶段发生，
而是在写入 JSONL/打印 stdout-stderr 之前就已经统一经过 mj.sanitize。

覆盖：
1. DecisionLog.append()（同步落盘路径）+ AsyncDecisionLog（异步落盘路径，
   经同一个 DecisionLog.append() 写文件）——payload 内嵌 token 不得原样
   出现在落盘的 JSONL 文件里。
2. 按大小轮转 + gzip 压缩后的历史文件同样不含 token（不是只有"最新文件"
   干净，压缩归档前必须已经脱敏）。
3. mj.api.ApiError 的异常文本（str(error)）本身就不含 token——验证
   "API 错误文本必须先 sanitize_text" 的源头修复，而不是依赖下游各自
   记得脱敏。
4. mj.bot 打印到 stdout 的错误信息（repr(error) 经 sanitize_text 处理）
   不含 token。
5. 经 tools/data_tool.py 完整导入流程后，SQLite errors 表的 summary 字段
   不含 token（对应 mj/data_import.py 里已有的 sanitize_text 调用，这里
   做端到端复核）。

不访问真实网络：全程使用本地临时目录 + 内存/临时 SQLite。
"""
import gzip
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, Mock, patch

from mj import datastore as ds
from mj.api import ApiError
from mj.async_log import AsyncDecisionLog
from mj.bot import match_with_retry
from mj.logging import DecisionLog

REAL_LOOKING_TOKEN = "c" * 64  # 64 位十六进制令牌样式
BEARER_HEADER = f"Bearer {REAL_LOOKING_TOKEN}"


class SyncLogSourceLevelSanitizationTests(unittest.TestCase):
    def test_raw_jsonl_file_never_contains_token_even_without_salt(self):
        with tempfile.TemporaryDirectory() as directory:
            log = DecisionLog(directory=directory)
            log.append("error", {
                "game_id": "g1",
                "error": f"connection failed, Authorization: {BEARER_HEADER} rejected",
            })
            path = os.path.join(directory, os.listdir(directory)[0])
            with open(path, encoding="utf-8") as handle:
                raw_content = handle.read()
        self.assertNotIn(REAL_LOOKING_TOKEN, raw_content)
        self.assertIn("REDACTED", raw_content)

    def test_action_rejected_payload_error_token_not_in_raw_file(self):
        with tempfile.TemporaryDirectory() as directory:
            log = DecisionLog(directory=directory)
            log.append("action_rejected", {
                "decision_id": "d1", "game_id": "g1", "phase": "draw",
                "error": f"409 conflict, session token {REAL_LOOKING_TOKEN} invalid",
                "rejections": 2,
            })
            path = os.path.join(directory, os.listdir(directory)[0])
            with open(path, encoding="utf-8") as handle:
                raw_content = handle.read()
        self.assertNotIn(REAL_LOOKING_TOKEN, raw_content)

    def test_rotated_gzip_archive_never_contains_token(self):
        """轮转+压缩后的历史文件同样不含 token——脱敏发生在写入那一刻，
        不是靠"压缩时顺便清理"。"""
        with tempfile.TemporaryDirectory() as directory:
            log = DecisionLog(directory=directory, max_bytes=200)
            for i in range(30):
                log.append("error", {
                    "game_id": f"g{i}",
                    "error": f"padding {'x' * 30} Authorization: {BEARER_HEADER}",
                })
            gz_files = [f for f in os.listdir(directory) if f.endswith(".gz")]
            self.assertGreater(len(gz_files), 0, "test setup should have triggered rotation")
            for fname in gz_files:
                with gzip.open(os.path.join(directory, fname), "rt", encoding="utf-8") as handle:
                    content = handle.read()
                self.assertNotIn(REAL_LOOKING_TOKEN, content)


class AsyncLogSourceLevelSanitizationTests(unittest.TestCase):
    def test_async_written_jsonl_never_contains_token(self):
        with tempfile.TemporaryDirectory() as directory:
            inner = DecisionLog(directory=directory)
            log = AsyncDecisionLog(inner, maxsize=100)
            log.append("fallback_sent", {
                "decision_id": "d1", "game_id": "g1", "tile": "1w",
                "accepted": False,
                "error": f"timeout, last header was Authorization: {BEARER_HEADER}",
            })
            log.close(timeout=3.0)
            path = os.path.join(directory, os.listdir(directory)[0])
            with open(path, encoding="utf-8") as handle:
                raw_content = handle.read()
        self.assertNotIn(REAL_LOOKING_TOKEN, raw_content)


class ApiErrorSourceLevelSanitizationTests(unittest.TestCase):
    def test_api_error_str_never_contains_token_from_response_body(self):
        """P0-3：ApiError 消息文本本身就必须已经脱敏——不依赖调用方
        （mj/bot.py、mj/data_import.py）各自记得再脱敏一次。"""
        error = ApiError(401, f"unauthorized: Authorization: {BEARER_HEADER} expired")
        self.assertNotIn(REAL_LOOKING_TOKEN, str(error))
        self.assertNotIn(REAL_LOOKING_TOKEN, error.body)

    def test_api_error_from_real_request_flow_stays_sanitized_in_log(self):
        """完整链路复核：ApiError 在 mj/bot.py 的 action_rejected 路径里
        被 str(error) 后写入日志，全程不出现 token（源头已脱敏，下游
        无需重新处理）。"""
        with tempfile.TemporaryDirectory() as directory:
            log = DecisionLog(directory=directory)
            error = ApiError(409, f"conflict, Authorization: {BEARER_HEADER}")
            log.action_rejected("g1", "draw", {"action": "discard", "tile": "1w"}, error, 1,
                                 {"seat": 0, "round_no": 1, "wall_remaining": 40,
                                  "my_hand": [], "drawn_tile": None, "melds": []},
                                 decision_id="d1")
            path = os.path.join(directory, os.listdir(directory)[0])
            with open(path, encoding="utf-8") as handle:
                raw_content = handle.read()
        self.assertNotIn(REAL_LOOKING_TOKEN, raw_content)


class StdoutStderrSourceLevelSanitizationTests(unittest.TestCase):
    def test_bot_stdout_print_of_api_error_never_contains_token(self):
        """mj.bot.match_with_retry 打印到 stdout 的错误信息（repr(error)
        经 sanitize_text 处理）不含 token。"""
        api = MagicMock()
        api.match.side_effect = ApiError(403, f"forbidden Authorization: {BEARER_HEADER}")
        buf = io.StringIO()
        with self.assertRaises(SystemExit):
            with patch("mj.bot.time.sleep"):
                with redirect_stdout(buf):
                    match_with_retry(api, max_seconds=-1)
        printed = buf.getvalue()
        self.assertNotIn(REAL_LOOKING_TOKEN, printed)

    def test_async_log_stderr_write_error_message_never_contains_token(self):
        """写线程异常打印到 stderr 的文本也经过 sanitize_text 处理。"""
        class TokenLeakingLog:
            def append(self, kind, payload):
                raise OSError(f"disk full, cached Authorization: {BEARER_HEADER}")

        log = AsyncDecisionLog(TokenLeakingLog(), maxsize=10)
        stderr_buf = io.StringIO()
        old_stderr = sys.stderr
        sys.stderr = stderr_buf
        try:
            log.append("state", {"i": 0})
            log.close(timeout=2.0)
        finally:
            sys.stderr = old_stderr
        self.assertNotIn(REAL_LOOKING_TOKEN, stderr_buf.getvalue())


class DatabaseSourceLevelSanitizationTests(unittest.TestCase):
    def test_imported_errors_table_summary_never_contains_token(self):
        """完整生产闭环复核：JSONL（含 token 反例）-> data_import ->
        SQLite errors.summary 字段不含 token。"""
        from mj import data_import as di

        with tempfile.TemporaryDirectory() as directory:
            log = DecisionLog(directory=directory)
            log.append("action_rejected", {
                "decision_id": "d1", "game_id": "g1", "phase": "draw",
                "error": f"409, Authorization: {BEARER_HEADER}",
                "rejections": 1,
            })
            path = os.path.join(directory, os.listdir(directory)[0])
            with open(path, encoding="utf-8") as handle:
                raw_content = handle.read()
            # 源头（JSONL 文件本身）已经不含 token。
            self.assertNotIn(REAL_LOOKING_TOKEN, raw_content)

            db_path = os.path.join(directory, "test.sqlite")
            conn = ds.connect(db_path)
            ds.init_schema(conn)
            sha = ds.sha256_file(path)
            fid = ds.insert_source_file(conn, path=path, sha256=sha,
                                         size_bytes=os.path.getsize(path), mtime=0.0,
                                         fmt="decision_jsonl", source_type="local_decision_log")
            conn.commit()
            di.import_decision_log_stream(conn, di.open_text_lines(path), source_file_id=fid,
                                           source_sha256=sha)
            conn.commit()
            rows = conn.execute("SELECT summary FROM errors").fetchall()
            self.assertGreater(len(rows), 0)
            for row in rows:
                self.assertNotIn(REAL_LOOKING_TOKEN, row["summary"])


if __name__ == "__main__":
    unittest.main()
