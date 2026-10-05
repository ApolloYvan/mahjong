"""目标二验收测试：build_hash 全链路——decision/decision_attempt ->
SQLite decisions/decision_attempts -> data_import -> tools/data_tool.py
summary/fixture-export。

覆盖：
- test_build_hash_round_trip_through_sqlite: JSONL(含 build_hash) ->
  data_import -> SQLite decisions/decision_attempts 保留 build_hash 原值；
  旧日志缺 build_hash 时标记 BUILD_HASH_UNAVAILABLE，不伪造。
- test_summary_reports_build_hash_coverage: summary 命令输出
  build_hash_known_decisions/build_hash_known_decision_attempts，各自含
  numerator/denominator/pct。
- test_fixture_export_preserves_build_hash: fixture-export 导出的
  decisions/decision_attempts 均保留 build_hash 字段原值。

不访问真实网络：全程本地临时目录 + 临时 SQLite，CLI 通过 subprocess 调用
tools/data_tool.py（与 tests/test_data_tool_cli.py 一致的调用方式）。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

from mj import data_import as di
from mj import datastore as ds

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_TOOL = os.path.join(REPO_ROOT, "tools", "data_tool.py")


def run_cli(*args):
    proc = subprocess.run(
        [sys.executable, DATA_TOOL, *args],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    return proc.returncode, proc.stdout, proc.stderr


class BuildHashRoundTripThroughSqliteTests(unittest.TestCase):
    def test_build_hash_round_trip_through_sqlite(self):
        records = [
            # 有 build_hash 的新记录（decision + decision_attempt + outcome）。
            {"time": "t1", "kind": "decision", "payload": {
                "decision_id": "d_new", "game_id": "g1", "round_no": 1, "seat": 0,
                "decision": {"action": "discard", "tile": "1w"},
                "build_hash": "abc123deadbeef01",
            }},
            {"time": "t1", "kind": "decision_attempt", "payload": {
                "decision_id": "d_new", "game_id": "g1", "round_no": 1, "seat": 0,
                "action": "discard", "tile": "1w", "build_hash": "abc123deadbeef01",
            }},
            {"time": "t1b", "kind": "decision_attempt_outcome", "payload": {
                "decision_id": "d_new", "outcome": "success",
            }},
            # 旧日志缺 build_hash（B3 生产接入之前写入的记录）。
            {"time": "t2", "kind": "decision", "payload": {
                "decision_id": "d_old", "game_id": "g1", "round_no": 1, "seat": 1,
                "decision": {"action": "pass", "tile": ""},
            }},
            {"time": "t2", "kind": "decision_attempt", "payload": {
                "decision_id": "d_old", "game_id": "g1", "round_no": 1, "seat": 1,
                "action": "pass", "tile": "",
            }},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "d.jsonl")
            with open(path, "w", encoding="utf-8") as handle:
                for r in records:
                    handle.write(json.dumps(r, ensure_ascii=False) + "\n")

            db_path = os.path.join(directory, "test.sqlite")
            conn = ds.connect(db_path)
            ds.init_schema(conn)
            sha = ds.sha256_file(path)
            fid = ds.insert_source_file(conn, path=path, sha256=sha, size_bytes=os.path.getsize(path),
                                         mtime=0.0, fmt="decision_jsonl",
                                         source_type="local_decision_log")
            conn.commit()
            stats = di.import_decision_log_stream(conn, di.open_text_lines(path),
                                                   source_file_id=fid, source_sha256=sha)
            ds.mark_source_file_complete(conn, fid, stats.total_lines, stats.success_lines,
                                          stats.failed_lines)
            conn.commit()

            # 新记录：SQLite 保留真实 build_hash 原值，不被替换。
            new_decision = conn.execute(
                "SELECT build_hash FROM decisions WHERE decision_id='d_new'"
            ).fetchone()
            self.assertEqual(new_decision["build_hash"], "abc123deadbeef01")
            new_attempt = conn.execute(
                "SELECT build_hash FROM decision_attempts WHERE decision_id='d_new'"
            ).fetchone()
            self.assertEqual(new_attempt["build_hash"], "abc123deadbeef01")

            # 旧记录：缺 build_hash 时标记为 BUILD_HASH_UNAVAILABLE 哨兵，
            # 不得伪造一个假的哈希值蒙混过关。
            old_decision = conn.execute(
                "SELECT build_hash FROM decisions WHERE decision_id='d_old'"
            ).fetchone()
            self.assertEqual(old_decision["build_hash"], di.BUILD_HASH_UNAVAILABLE)
            old_attempt = conn.execute(
                "SELECT build_hash FROM decision_attempts WHERE decision_id='d_old'"
            ).fetchone()
            self.assertEqual(old_attempt["build_hash"], di.BUILD_HASH_UNAVAILABLE)
            # 哨兵值本身不能长得像一个真实哈希（防止未来误把它当成真实
            # build_hash 用于比对）。
            self.assertIn("unavailable", di.BUILD_HASH_UNAVAILABLE)


class SummaryReportsBuildHashCoverageTests(unittest.TestCase):
    def test_summary_reports_build_hash_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = os.path.join(directory, "test.sqlite")
            conn = ds.connect(db_path)
            ds.init_schema(conn)
            # 2 条有真实 build_hash，1 条 unavailable。
            ds.upsert_decision(conn, decision_id="d1", decision_id_synthesized=False,
                                game_id="g1", round_no=1, seat=0, state_hash="h1",
                                policy_version="legacy", schema_version="1", config_hash="c1",
                                action="discard", tile="1w", time_value="t1",
                                build_hash="hash_a")
            ds.upsert_decision(conn, decision_id="d2", decision_id_synthesized=False,
                                game_id="g1", round_no=1, seat=1, state_hash="h2",
                                policy_version="legacy", schema_version="1", config_hash="c1",
                                action="pass", tile="", time_value="t2",
                                build_hash="hash_a")
            ds.upsert_decision(conn, decision_id="d3", decision_id_synthesized=False,
                                game_id="g1", round_no=1, seat=2, state_hash="h3",
                                policy_version="legacy", schema_version="1", config_hash="c1",
                                action="pass", tile="", time_value="t3",
                                build_hash=di.BUILD_HASH_UNAVAILABLE)
            ds.upsert_decision_attempt(conn, decision_id="d1", game_id="g1", round_no=1, seat=0,
                                        action="discard", tile="1w", attempted_at="t1",
                                        build_hash="hash_a")
            ds.upsert_decision_attempt(conn, decision_id="d2", game_id="g1", round_no=1, seat=1,
                                        action="pass", tile="", attempted_at="t2",
                                        build_hash=di.BUILD_HASH_UNAVAILABLE)
            conn.commit()
            conn.close()

            code, out, err = run_cli("summary", "--db", db_path)
            self.assertEqual(code, 0, err)
            summary = json.loads(out)
            coverage = summary["policy_config_build_coverage"]
            self.assertIn("build_hash_known_decisions", coverage)
            self.assertIn("build_hash_known_decision_attempts", coverage)

            dec = coverage["build_hash_known_decisions"]
            self.assertEqual(dec["numerator"], 2)
            self.assertEqual(dec["denominator"], 3)
            self.assertAlmostEqual(dec["pct"], 200.0 / 3, places=1)

            att = coverage["build_hash_known_decision_attempts"]
            self.assertEqual(att["numerator"], 1)
            self.assertEqual(att["denominator"], 2)
            self.assertAlmostEqual(att["pct"], 50.0, places=1)


class FixtureExportPreservesBuildHashTests(unittest.TestCase):
    def test_fixture_export_preserves_build_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = os.path.join(directory, "test.sqlite")
            conn = ds.connect(db_path)
            ds.init_schema(conn)
            ds.upsert_decision(conn, decision_id="d1", decision_id_synthesized=False,
                                game_id="g1", round_no=5, seat=0, state_hash="h1",
                                policy_version="legacy", schema_version="1", config_hash="c1",
                                action="discard", tile="1w", time_value="t1",
                                build_hash="hash_export_check")
            ds.upsert_decision_attempt(conn, decision_id="d1", game_id="g1", round_no=5, seat=0,
                                        action="discard", tile="1w", attempted_at="t1",
                                        outcome="success", resolved_at="t1b",
                                        build_hash="hash_export_check")
            conn.commit()
            conn.close()

            output = os.path.join(directory, "fixture.json")
            code, out, err = run_cli(
                "fixture-export", "--db", db_path, "--game", "g1", "--round", "5",
                "--output", output, "--synthetic-source",
            )
            self.assertEqual(code, 0, err)
            with open(output, encoding="utf-8") as handle:
                fixture = json.load(handle)

            self.assertEqual(len(fixture["decisions"]), 1)
            self.assertEqual(fixture["decisions"][0]["build_hash"], "hash_export_check")
            self.assertIn("decision_attempts", fixture)
            self.assertEqual(len(fixture["decision_attempts"]), 1)
            self.assertEqual(fixture["decision_attempts"][0]["build_hash"], "hash_export_check")


if __name__ == "__main__":
    unittest.main()
