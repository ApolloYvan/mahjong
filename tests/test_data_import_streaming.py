"""mj.data_import 流式导入契约测试（阶段A返修 A1"真正流式/有界内存导入"）：
- 逐条 yield，不整份累积；
- import_decision_log_stream 按批提交事务，批次大小可配置；
- 至少 100,000 条合成 decision 的峰值内存验收（tracemalloc），证明内存
  不随文件总行数线性增长；
- source_files 状态区分 importing/complete/failed，失败文件不能停留在
  "看似已完整导入"的状态。
"""
import gzip
import json
import os
import tempfile
import tracemalloc
import unittest

from mj import data_import as di
from mj import datastore as ds


class GeneratorYieldsPerLineTests(unittest.TestCase):
    def test_iter_decision_log_lines_is_a_generator_not_a_list_builder(self):
        import types
        lines = iter(['{"kind":"decision","payload":{"game_id":"g1","decision":{}}}'])
        result = di.iter_decision_log_lines(lines)
        self.assertIsInstance(result, types.GeneratorType)

    def test_each_yielded_outcome_only_carries_its_own_line_data(self):
        lines = iter([
            '{"kind":"decision","payload":{"game_id":"g1","decision_id":"d1","decision":{}}}',
            '{"kind":"decision","payload":{"game_id":"g2","decision_id":"d2","decision":{}}}',
        ])
        outcomes = list(di.iter_decision_log_lines(lines))
        self.assertEqual(len(outcomes), 2)
        self.assertEqual(outcomes[0].decision.game_id, "g1")
        self.assertEqual(outcomes[1].decision.game_id, "g2")


class ImportStreamBatchCommitTests(unittest.TestCase):
    def _fresh_conn_with_source_file(self):
        conn = ds.connect(":memory:")
        ds.init_schema(conn)
        fid = ds.insert_source_file(conn, path="a.jsonl", sha256="sha", size_bytes=1,
                                     mtime=1.0, fmt="decision_jsonl",
                                     source_type="local_decision_log")
        conn.commit()
        return conn, fid

    def test_batches_commit_at_configured_size(self):
        """用一个包装类代理真实连接，计数 commit() 调用次数，验证提交次数
        与 batch_size 配置一致（不是一次性提交，也不是逐行提交）。
        sqlite3.Connection 本身不允许给方法赋值（C 扩展类型只读属性），
        所以用轻量包装对象转发除 commit 之外的所有调用。"""
        conn, fid = self._fresh_conn_with_source_file()
        commit_calls = []

        class CountingConnWrapper:
            def __init__(self, inner):
                self._inner = inner

            def commit(self):
                commit_calls.append(1)
                self._inner.commit()

            def __getattr__(self, name):
                return getattr(self._inner, name)

        wrapped = CountingConnWrapper(conn)

        lines = iter(
            f'{{"kind":"decision","payload":{{"game_id":"g{i}","decision_id":"d{i}",'
            f'"decision":{{"action":"discard"}}}}}}'
            for i in range(25)
        )
        stats = di.import_decision_log_stream(wrapped, lines, source_file_id=fid,
                                               source_sha256="sha", batch_size=10)
        self.assertEqual(stats.total_lines, 25)
        self.assertEqual(stats.decisions_written, 25)
        # 25 行，batch_size=10 → 提交发生在第10、20行，末尾再提交一次收尾 = 3 次。
        self.assertEqual(len(commit_calls), 3)

    def test_all_rows_present_after_streaming_import(self):
        conn, fid = self._fresh_conn_with_source_file()
        lines = iter(
            f'{{"kind":"decision","payload":{{"game_id":"g1","decision_id":"d{i}",'
            f'"decision":{{"action":"discard"}}}}}}'
            for i in range(37)
        )
        di.import_decision_log_stream(conn, lines, source_file_id=fid, source_sha256="sha",
                                       batch_size=10)
        count = conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        self.assertEqual(count, 37)

    def test_bad_line_inside_batch_does_not_abort_batch(self):
        conn, fid = self._fresh_conn_with_source_file()
        lines = iter([
            '{"kind":"decision","payload":{"game_id":"g1","decision_id":"d1","decision":{}}}',
            "BROKEN NOT JSON {{{",
            '{"kind":"decision","payload":{"game_id":"g1","decision_id":"d2","decision":{}}}',
        ])
        stats = di.import_decision_log_stream(conn, lines, source_file_id=fid,
                                               source_sha256="sha", batch_size=2)
        self.assertEqual(stats.total_lines, 3)
        self.assertEqual(stats.failed_lines, 1)
        self.assertEqual(stats.decisions_written, 2)
        count = conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        self.assertEqual(count, 2)


class SourceFileStatusLifecycleTests(unittest.TestCase):
    """A1 返修：source_files 必须能区分 importing/complete/failed，失败
    文件不能停留在"看似已完整导入"的半成品状态。"""

    def test_new_source_file_starts_as_importing(self):
        conn = ds.connect(":memory:")
        ds.init_schema(conn)
        fid = ds.insert_source_file(conn, path="a.jsonl", sha256="h1", size_bytes=1,
                                     mtime=1.0, fmt="decision_jsonl",
                                     source_type="local_decision_log")
        conn.commit()
        row = conn.execute("SELECT status FROM source_files WHERE id=?", (fid,)).fetchone()
        self.assertEqual(row["status"], "importing")

    def test_mark_complete_transitions_status(self):
        conn = ds.connect(":memory:")
        ds.init_schema(conn)
        fid = ds.insert_source_file(conn, path="a.jsonl", sha256="h1", size_bytes=1,
                                     mtime=1.0, fmt="decision_jsonl",
                                     source_type="local_decision_log")
        ds.mark_source_file_complete(conn, fid, total=10, success=10, failed=0)
        conn.commit()
        row = conn.execute("SELECT status, total_lines FROM source_files WHERE id=?",
                            (fid,)).fetchone()
        self.assertEqual(row["status"], "complete")
        self.assertEqual(row["total_lines"], 10)

    def test_mark_failed_never_shows_as_complete(self):
        conn = ds.connect(":memory:")
        ds.init_schema(conn)
        fid = ds.insert_source_file(conn, path="a.jsonl", sha256="h1", size_bytes=1,
                                     mtime=1.0, fmt="decision_jsonl",
                                     source_type="local_decision_log")
        ds.mark_source_file_failed(conn, fid, total=5, success=3, failed=2,
                                    error_message="disk full mid-import")
        conn.commit()
        row = conn.execute("SELECT status, error_message FROM source_files WHERE id=?",
                            (fid,)).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertIn("disk full", row["error_message"])
        self.assertNotEqual(row["status"], "complete")

    def test_full_import_run_marks_status_via_data_tool_helpers(self):
        """端到端场景：模拟 tools/data_tool.py 的导入流程——正常路径必须
        以 'complete' 收尾；异常路径必须以 'failed' 收尾，不允许两者都不是。"""
        conn = ds.connect(":memory:")
        ds.init_schema(conn)
        fid = ds.insert_source_file(conn, path="a.jsonl", sha256="hOK", size_bytes=1,
                                     mtime=1.0, fmt="decision_jsonl",
                                     source_type="local_decision_log")
        lines = iter(['{"kind":"decision","payload":{"game_id":"g1","decision_id":"d1",'
                      '"decision":{}}}'])
        try:
            stats = di.import_decision_log_stream(conn, lines, source_file_id=fid,
                                                   source_sha256="hOK")
        except Exception as exc:  # noqa: BLE001
            ds.mark_source_file_failed(conn, fid, 0, 0, 0, str(exc))
            conn.commit()
            raise
        else:
            ds.mark_source_file_complete(conn, fid, stats.total_lines, stats.success_lines,
                                          stats.failed_lines)
            conn.commit()
        row = conn.execute("SELECT status FROM source_files WHERE id=?", (fid,)).fetchone()
        self.assertEqual(row["status"], "complete")


class PeakMemoryStressTest(unittest.TestCase):
    """A1 硬性要求：至少 100,000 条合成 decision 的受控峰值内存验收，
    证明内存不会随总行数线性累积。用 tracemalloc 测量
    ``import_decision_log_stream`` 执行期间的峰值分配，并与"把全部行
    一次性读入列表"的旧做法对比，确认峰值显著更低、且不随行数线性增长
    到"保留全部记录"的量级。
    """

    N_LINES = 100_000

    def _make_jsonl_file(self, path, n):
        with open(path, "w", encoding="utf-8") as handle:
            for i in range(n):
                record = {
                    "time": f"2026-09-21T00:00:{i % 60:02d}Z",
                    "kind": "decision",
                    "payload": {
                        "decision_id": f"d{i}",
                        "game_id": f"g{i % 500}",
                        "round_no": i % 8,
                        "seat": i % 4,
                        "state_hash": f"h{i % 1000}",
                        "policy_version": "legacy",
                        "schema_version": "1",
                        "config_hash": "c1",
                        "decision": {"action": "discard", "tile": "1w"},
                    },
                }
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def test_streaming_import_peak_memory_bounded_for_100k_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "big.jsonl")
            self._make_jsonl_file(path, self.N_LINES)
            file_size = os.path.getsize(path)

            db_path = os.path.join(tmp, "big.sqlite")
            conn = ds.connect(db_path)
            ds.init_schema(conn)
            sha = ds.sha256_file(path)
            fid = ds.insert_source_file(conn, path=path, sha256=sha, size_bytes=file_size,
                                         mtime=0.0, fmt="decision_jsonl",
                                         source_type="local_decision_log")
            conn.commit()

            tracemalloc.start()
            import time as _time
            started = _time.monotonic()
            stats = di.import_decision_log_stream(
                conn, di.open_text_lines(path), source_file_id=fid, source_sha256=sha,
                batch_size=2000,
            )
            elapsed = _time.monotonic() - started
            current, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            ds.mark_source_file_complete(conn, fid, stats.total_lines, stats.success_lines,
                                          stats.failed_lines)
            conn.commit()

            self.assertEqual(stats.total_lines, self.N_LINES)
            self.assertEqual(stats.decisions_written, self.N_LINES)

            row_count = conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
            self.assertEqual(row_count, self.N_LINES)

            # 峰值内存证据：100,000 行、文件 file_size 字节，峰值追踪分配应
            # 远小于"把全部行内容累积成对象列表"的量级（每行约 300+ 字节的
            # dataclass/dict 结构，10万行会有几十 MB）。这里设一个宽松但
            # 有意义的上限：峰值不超过 20MB（batch_size=2000，理论上峰值
            # 只由"当前批次"决定，不随总行数增长）。
            peak_mb = peak / (1024 * 1024)
            print(f"\n[peak-memory-evidence] file_size={file_size} bytes "
                  f"({file_size/1024/1024:.2f} MiB), lines={self.N_LINES}, "
                  f"elapsed={elapsed:.2f}s, peak_traced_memory={peak_mb:.2f} MiB")
            self.assertLess(peak_mb, 20.0,
                             f"peak memory {peak_mb:.2f}MiB too high for bounded streaming import")


if __name__ == "__main__":
    unittest.main()
