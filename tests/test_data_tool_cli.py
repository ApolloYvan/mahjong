"""tools/data_tool.py 端到端测试：使用会话内构造的合成 fixture 验证导入器、
查询器、对账与 fixture 导出的完整闭环。不使用任何真实牌谱/日志/令牌。

覆盖任务书边界要求：
- 逐行流式、.jsonl/.jsonl.gz 均支持；
- 单行损坏不中止全文件；
- 重复导入同一文件（sha256 不变）不产生重复记录；
- glob 无匹配、数据库不存在、matched=0 均返回非零退出码（不得把"没读到
  数据"表现成"对账通过"）。
"""
import gzip
import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_TOOL = os.path.join(REPO_ROOT, "tools", "data_tool.py")


def run_cli(*args):
    proc = subprocess.run(
        [sys.executable, DATA_TOOL, *args],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    return proc.returncode, proc.stdout, proc.stderr


class ImportLogsEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.logs_dir = os.path.join(self.tmp.name, "logs")
        os.makedirs(self.logs_dir)
        self.db = os.path.join(self.tmp.name, "test.sqlite")

    def _write_jsonl(self, name, records):
        path = os.path.join(self.logs_dir, name)
        with open(path, "w", encoding="utf-8") as handle:
            for record in records:
                if isinstance(record, str):
                    handle.write(record + "\n")
                else:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return path

    def test_import_logs_streams_jsonl_and_reports_counts(self):
        self._write_jsonl("d1.jsonl", [
            {"time": "t1", "kind": "decision", "payload": {
                "decision_id": "d1", "game_id": "g1", "round_no": 1, "seat": 0,
                "decision": {"action": "discard", "tile": "1w"},
            }},
            {"time": "t2", "kind": "hu_detail", "payload": {
                "game_id": "g1", "round_no": 1, "seat": 0, "fan": 2, "detail": ["平胡"],
            }},
        ])
        code, out, err = run_cli(
            "import-logs", os.path.join(self.logs_dir, "*.jsonl"), "--db", self.db,
        )
        self.assertEqual(code, 0, err)
        lines = [json.loads(l) for l in out.strip().splitlines()]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["status"], "imported")
        self.assertEqual(lines[0]["decisions"], 1)
        self.assertEqual(lines[0]["round_results"], 1)

    def test_import_logs_supports_gzip(self):
        path = os.path.join(self.logs_dir, "d1.jsonl.gz")
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "time": "t1", "kind": "decision",
                "payload": {"decision_id": "d1", "game_id": "g1",
                            "decision": {"action": "pass"}},
            }, ensure_ascii=False) + "\n")
        code, out, err = run_cli(
            "import-logs", os.path.join(self.logs_dir, "*.jsonl.gz"), "--db", self.db,
        )
        self.assertEqual(code, 0, err)
        info = json.loads(out.strip())
        self.assertEqual(info["status"], "imported")
        self.assertEqual(info["decisions"], 1)

    def test_malformed_line_does_not_abort_file_import(self):
        self._write_jsonl("mixed.jsonl", [
            {"time": "t1", "kind": "decision", "payload": {
                "decision_id": "d1", "game_id": "g1", "decision": {"action": "discard"},
            }},
            "BROKEN NOT JSON {{{",
            {"time": "t2", "kind": "decision", "payload": {
                "decision_id": "d2", "game_id": "g1", "decision": {"action": "pass"},
            }},
        ])
        code, out, err = run_cli(
            "import-logs", os.path.join(self.logs_dir, "*.jsonl"), "--db", self.db,
        )
        self.assertEqual(code, 0, err)
        info = json.loads(out.strip())
        self.assertEqual(info["total_lines"], 3)
        self.assertEqual(info["success_lines"], 2)
        self.assertEqual(info["failed_lines"], 1)
        self.assertEqual(info["decisions"], 2)

    def test_reimport_same_content_is_skipped_not_duplicated(self):
        self._write_jsonl("d1.jsonl", [
            {"time": "t1", "kind": "decision", "payload": {
                "decision_id": "d1", "game_id": "g1", "decision": {"action": "discard"},
            }},
        ])
        run_cli("import-logs", os.path.join(self.logs_dir, "*.jsonl"), "--db", self.db)
        code, out, err = run_cli(
            "import-logs", os.path.join(self.logs_dir, "*.jsonl"), "--db", self.db,
        )
        self.assertEqual(code, 0, err)
        info = json.loads(out.strip())
        self.assertEqual(info["status"], "skipped_duplicate_content")

        # 验证数据库里确实只有一条 decision 记录，不是两条。
        import sqlite3
        conn = sqlite3.connect(self.db)
        count = conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        self.assertEqual(count, 1)

    def test_interrupted_import_then_reimport_matches_single_complete_import(self):
        """P0-4 端到端验收：模拟"批量提交后进程中断"——先手工制造一条
        status='importing' 且已经写入部分 decisions 的 source_files 记录
        （模拟批量提交发生但文件级 mark_complete 从未执行的中断场景），
        再对同一份文件调用一次正常导入。最终 decisions 表的行数必须与
        "从未失败过、一次性完整导入这份文件"完全一致——不多不少，不允许
        遗留半导入产生的残留行，也不允许因为跳过重试而缺行。"""
        records = [
            {"time": f"t{i}", "kind": "decision", "payload": {
                "decision_id": f"d{i}", "game_id": "g1", "round_no": 1, "seat": i % 4,
                "decision": {"action": "discard", "tile": "1w"},
            }}
            for i in range(12)
        ]
        path = self._write_jsonl("interrupt.jsonl", records)

        from mj import datastore as ds
        sha256 = ds.sha256_file(path)
        conn = ds.connect(self.db)
        ds.init_schema(conn)
        # 模拟"批量提交后进程中断"：source_files 停留在 'importing'，但已经
        # 有部分（例如前 5 条）decisions 真正落盘提交了——这是旧版 bug 的
        # 精确复现场景：batch_size 内的提交已经生效，但 mark_complete 从未
        # 被调用（进程在提交后、mark_complete 前崩溃）。
        import os as _os
        stat = _os.stat(path)
        source_file_id = ds.insert_source_file(
            conn, path=path, sha256=sha256, size_bytes=stat.st_size, mtime=stat.st_mtime,
            fmt="decision_jsonl", source_type="local_decision_log",
        )
        for i in range(5):
            ds.upsert_decision(
                conn, decision_id=f"d{i}", decision_id_synthesized=False, game_id="g1",
                round_no=1, seat=i % 4, state_hash=None, policy_version="legacy",
                schema_version="1", config_hash="c1", action="discard", tile="1w",
                time_value=f"t{i}", source_file_id=source_file_id,
            )
        conn.commit()
        conn.close()

        # 验证"中断态"确实是半导入：数据库里已有 5 条 decisions，但
        # source_files.status 仍是 'importing'（不是 complete）。
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        status_before = conn.execute(
            "SELECT status FROM source_files WHERE sha256=?", (sha256,)
        ).fetchone()["status"]
        self.assertEqual(status_before, "importing")
        count_before = conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        self.assertEqual(count_before, 5)
        conn.close()

        # 重新导入同一份文件：P0-4 要求 status='importing' 必须支持安全
        # 重试，不能被旧版"任意已存在记录都跳过"的逻辑直接短路。
        code, out, err = run_cli(
            "import-logs", os.path.join(self.logs_dir, "*.jsonl"), "--db", self.db,
        )
        self.assertEqual(code, 0, err)
        info = json.loads(out.strip())
        self.assertNotEqual(info["status"], "skipped_duplicate_content")
        self.assertEqual(info["decisions"], 12)

        # 最终行数必须与"一次性完整导入 12 条记录"完全一致：不多（没有
        # 残留的旧 5 条 + 新 12 条 = 17 条半导入垃圾），也不少（不是被跳过
        # 导致仍然停留在 5 条）。
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        final_count = conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        self.assertEqual(final_count, 12)
        final_status = conn.execute(
            "SELECT status FROM source_files WHERE sha256=?", (sha256,)
        ).fetchone()["status"]
        self.assertEqual(final_status, "complete")
        # 内容哈希级验证：重新导入后 decision_id 集合应恰好是 d0..d11，
        # 与"一次性完整导入"产生的集合完全相同（用集合比较代替单纯计数，
        # 排除"数量对但内容错"的可能）。
        ids = {row["decision_id"] for row in conn.execute("SELECT decision_id FROM decisions")}
        self.assertEqual(ids, {f"d{i}" for i in range(12)})
        conn.close()

    def test_glob_matching_nothing_returns_nonzero(self):
        code, out, err = run_cli(
            "import-logs", os.path.join(self.logs_dir, "does_not_exist_*.jsonl"),
            "--db", self.db,
        )
        self.assertNotEqual(code, 0)

    def test_batch_size_option_accepted_and_still_imports_all(self):
        self._write_jsonl("d1.jsonl", [
            {"time": f"t{i}", "kind": "decision", "payload": {
                "decision_id": f"d{i}", "game_id": "g1",
                "decision": {"action": "discard", "tile": "1w"},
            }}
            for i in range(50)
        ])
        code, out, err = run_cli(
            "import-logs", os.path.join(self.logs_dir, "*.jsonl"), "--db", self.db,
            "--batch-size", "7",
        )
        self.assertEqual(code, 0, err)
        info = json.loads(out.strip())
        self.assertEqual(info["decisions"], 50)

    def test_all_files_parse_failed_returns_nonzero(self):
        # A4 返修：全部文件解析失败时命令必须非零退出（旧版即使全部失败仍返回0）。
        path = os.path.join(self.logs_dir, "corrupt.jsonl.gz")
        with open(path, "wb") as handle:
            handle.write(b"this is not valid gzip content at all")
        code, out, err = run_cli(
            "import-logs", os.path.join(self.logs_dir, "*.jsonl.gz"), "--db", self.db,
        )
        self.assertNotEqual(code, 0)


class ImportEventsEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.events_dir = os.path.join(self.tmp.name, "portal_events", "room1")
        os.makedirs(self.events_dir)
        self.db = os.path.join(self.tmp.name, "test.sqlite")

    def test_import_events_extracts_round_ended_as_server_truth(self):
        path = os.path.join(self.events_dir, "b1.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({
                "game_id": "g1",
                "blocks": [{"events": [
                    {"type": "round_ended", "seat": 0,
                     "data": {"fan": 4, "detail": ["平胡", "财飘", "爆头"]}},
                ]}],
            }, handle, ensure_ascii=False)
        code, out, err = run_cli(
            "import-events", os.path.join(self.events_dir, "*.json"), "--db", self.db,
        )
        self.assertEqual(code, 0, err)
        info = json.loads(out.strip())
        self.assertEqual(info["round_results"], 1)


class SummaryAnomaliesReconcileEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "test.sqlite")

    def test_summary_on_nonexistent_db_is_nonzero(self):
        code, out, err = run_cli("summary", "--db", os.path.join(self.tmp.name, "nope.sqlite"))
        self.assertNotEqual(code, 0)

    def test_reconcile_on_empty_db_is_nonzero_not_silent_pass(self):
        """任务书明确要求：输入无匹配/服务端结果为零/matched 为零时给出
        明确非零退出码，不能把"没读到数据"表现成成功。"""
        from mj import datastore as ds
        conn = ds.connect(self.db)
        ds.init_schema(conn)
        conn.commit()
        code, out, err = run_cli("reconcile", "--db", self.db)
        self.assertNotEqual(code, 0)
        self.assertIn("nothing was reconciled", err.lower().replace("_", " "))

    def test_summary_empty_db_without_allow_empty_is_nonzero(self):
        # A4 返修：空数据库/空摘要默认非零退出，除非显式传 --allow-empty。
        from mj import datastore as ds
        conn = ds.connect(self.db)
        ds.init_schema(conn)
        conn.commit()
        code, out, err = run_cli("summary", "--db", self.db)
        self.assertNotEqual(code, 0)

    def test_summary_empty_db_with_allow_empty_is_zero(self):
        from mj import datastore as ds
        conn = ds.connect(self.db)
        ds.init_schema(conn)
        conn.commit()
        code, out, err = run_cli("summary", "--db", self.db, "--allow-empty")
        self.assertEqual(code, 0, err)

    def test_anomalies_output_is_sanitized(self):
        # A2 返修：anomalies 输出必须经统一脱敏，不能原样吐出令牌。
        from mj import datastore as ds
        conn = ds.connect(self.db)
        ds.init_schema(conn)
        fid = ds.insert_source_file(conn, path="a.jsonl", sha256="h1", size_bytes=1,
                                     mtime=1.0, fmt="decision_jsonl",
                                     source_type="local_decision_log")
        token = "b" * 64
        ds.upsert_error(conn, source_file_id=fid, line_no=1, category="action_rejected",
                         summary=f"Authorization: Bearer {token}")
        conn.commit()
        code, out, err = run_cli("anomalies", "--db", self.db)
        self.assertEqual(code, 0, err)
        self.assertNotIn(token, out)

    def test_reconcile_matched_zero_with_data_present_is_nonzero(self):
        """有数据但本地/服务端 key 完全不重叠：matched=0，也必须非零退出。"""
        from mj import datastore as ds
        conn = ds.connect(self.db)
        ds.init_schema(conn)
        ds.upsert_round_result(conn, game_id="gX", round_no=1, winner_seat=0, fan=1,
                                detail=["平胡"], scores=None, next_dealer=None,
                                source="local_estimate")
        ds.upsert_round_result(conn, game_id="gY", round_no=1, winner_seat=0, fan=1,
                                detail=["平胡"], scores=None, next_dealer=None,
                                source="server_truth")
        conn.commit()
        code, out, err = run_cli("reconcile", "--db", self.db)
        self.assertNotEqual(code, 0)

    def test_reconcile_finds_fan_discrepancy_and_exits_nonzero(self):
        from mj import datastore as ds
        conn = ds.connect(self.db)
        ds.init_schema(conn)
        ds.upsert_round_result(conn, game_id="g1", round_no=5, winner_seat=0, fan=2,
                                detail=["平胡", "爆头"], scores=None, next_dealer=None,
                                source="local_estimate")
        ds.upsert_round_result(conn, game_id="g1", round_no=5, winner_seat=0, fan=4,
                                detail=["平胡", "财飘", "爆头"], scores=[24, -8, -8, -8],
                                next_dealer=0, source="server_truth")
        conn.commit()
        code, out, err = run_cli("reconcile", "--db", self.db)
        self.assertEqual(code, 1)
        self.assertIn("MISMATCH", out)
        self.assertIn("fan", out)
        self.assertIn("'fan': 2", out)
        self.assertIn("'fan': 4", out)

    def test_reconcile_agreement_only_exits_zero(self):
        from mj import datastore as ds
        conn = ds.connect(self.db)
        ds.init_schema(conn)
        ds.upsert_round_result(conn, game_id="g1", round_no=1, winner_seat=0, fan=1,
                                detail=["平胡"], scores=None, next_dealer=None,
                                source="local_estimate")
        ds.upsert_round_result(conn, game_id="g1", round_no=1, winner_seat=0, fan=1,
                                detail=["平胡"], scores=[1, -1, 0, 0], next_dealer=1,
                                source="server_truth")
        conn.commit()
        code, out, err = run_cli("reconcile", "--db", self.db)
        self.assertEqual(code, 0)

    def test_summary_reports_error_categories(self):
        from mj import datastore as ds
        conn = ds.connect(self.db)
        ds.init_schema(conn)
        fid = ds.insert_source_file(conn, path="a.jsonl", sha256="h1", size_bytes=1,
                                     mtime=1.0, fmt="decision_jsonl",
                                     source_type="local_decision_log")
        ds.upsert_error(conn, source_file_id=fid, line_no=1, category="action_rejected",
                         summary="test")
        conn.commit()
        code, out, err = run_cli("summary", "--db", self.db, "--allow-empty")
        self.assertEqual(code, 0, err)
        summary = json.loads(out)
        self.assertEqual(summary["errors_by_category"]["action_rejected"], 1)

    def test_anomalies_filters_by_kind(self):
        from mj import datastore as ds
        conn = ds.connect(self.db)
        ds.init_schema(conn)
        fid = ds.insert_source_file(conn, path="a.jsonl", sha256="h1", size_bytes=1,
                                     mtime=1.0, fmt="decision_jsonl",
                                     source_type="local_decision_log")
        ds.upsert_error(conn, source_file_id=fid, line_no=1, category="action_rejected",
                         summary="rejected1")
        ds.upsert_error(conn, source_file_id=fid, line_no=2, category="fallback_sent",
                         summary="fallback1")
        conn.commit()
        code, out, err = run_cli("anomalies", "--db", self.db, "--kind", "action_rejected")
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["items"][0]["category"], "action_rejected")


class FixtureExportEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "test.sqlite")

    def test_fixture_export_writes_redacted_json(self):
        from mj import datastore as ds
        conn = ds.connect(self.db)
        ds.init_schema(conn)
        ds.upsert_decision(conn, decision_id="d1", decision_id_synthesized=False,
                            game_id="g1", round_no=5, seat=0, state_hash="h1",
                            policy_version="legacy", schema_version="1", config_hash="c1",
                            action="discard", tile="1w", time_value="t1")
        ds.upsert_round_result(conn, game_id="g1", round_no=5, winner_seat=0, fan=2,
                                detail=["平胡"], scores=None, next_dealer=None,
                                source="local_estimate")
        conn.commit()
        output = os.path.join(self.tmp.name, "fixture.json")
        code, out, err = run_cli(
            "fixture-export", "--db", self.db, "--game", "g1", "--round", "5",
            "--output", output, "--synthetic-source",
        )
        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.exists(output))
        with open(output, encoding="utf-8") as handle:
            fixture = json.load(handle)
        self.assertEqual(fixture["game_id"], "g1")
        self.assertEqual(len(fixture["decisions"]), 1)
        self.assertEqual(len(fixture["round_results"]), 1)
        self.assertIn("provenance", fixture)

    def test_fixture_export_no_data_returns_nonzero(self):
        from mj import datastore as ds
        conn = ds.connect(self.db)
        ds.init_schema(conn)
        conn.commit()
        code, out, err = run_cli(
            "fixture-export", "--db", self.db, "--game", "nonexistent", "--round", "1",
            "--output", os.path.join(self.tmp.name, "out.json"),
        )
        self.assertNotEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
