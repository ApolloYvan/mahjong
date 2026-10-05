"""B 收尾要求测试：
- 10 线程压力测试报告 enqueue p50/p95/p99 和丢弃数；
- 异步日志落盘的文件能被 mj.data_import 直接导入并完整查询（生产闭环
  贯通验证：AsyncDecisionLog -> DecisionLog 落盘文件 -> data_import ->
  SQLite -> 可查询）；
- 不发真实网络请求（本文件全程使用本地临时目录 + 内存/临时 SQLite，
  没有任何 socket 调用）。
"""
import os
import tempfile
import threading
import time
import unittest

from mj import data_import as di
from mj import datastore as ds
from mj.async_log import AsyncDecisionLog
from mj.logging import DecisionLog


def _percentile(sorted_values, pct):
    if not sorted_values:
        return None
    k = (len(sorted_values) - 1) * pct
    f = int(k)
    c = min(f + 1, len(sorted_values) - 1)
    if f == c:
        return sorted_values[f]
    return sorted_values[f] * (c - k) + sorted_values[c] * (k - f)


class TenThreadStressTest(unittest.TestCase):
    def test_ten_threads_report_enqueue_latency_percentiles_and_drop_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            real_log = DecisionLog(directory=directory)
            log = AsyncDecisionLog(real_log, maxsize=2000)

            enqueue_latencies = []
            latencies_lock = threading.Lock()

            def producer(tid):
                local_latencies = []
                for i in range(200):
                    started = time.perf_counter()
                    log.append("state", {"thread": tid, "i": i})
                    local_latencies.append((time.perf_counter() - started) * 1000)
                with latencies_lock:
                    enqueue_latencies.extend(local_latencies)

            threads = [threading.Thread(target=producer, args=(t,)) for t in range(10)]
            started_at = time.monotonic()
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            produce_elapsed = time.monotonic() - started_at
            log.close(timeout=10.0)

            enqueue_latencies.sort()
            p50 = _percentile(enqueue_latencies, 0.50)
            p95 = _percentile(enqueue_latencies, 0.95)
            p99 = _percentile(enqueue_latencies, 0.99)
            dropped = log.dropped_count
            total_dropped = sum(dropped.values())

            print(f"\n[10-thread-stress-evidence] threads=10 events_per_thread=200 "
                  f"total=2000 produce_elapsed={produce_elapsed:.3f}s "
                  f"enqueue_p50={p50:.4f}ms enqueue_p95={p95:.4f}ms "
                  f"enqueue_p99={p99:.4f}ms dropped={dict(dropped)} "
                  f"emergency_drop={log.emergency_drop_count} "
                  f"write_error={log.write_error_count}")

            # enqueue 延迟应远低于 100ms（非阻塞入队的核心承诺）。
            self.assertLess(p99, 100.0, "enqueue p99 latency too high, enqueue is not non-blocking")
            # 总产出 = 落盘行数 + 丢弃数（不能既没落盘又没被计数——静默消失）。
            path = os.path.join(directory, os.listdir(directory)[0])
            with open(path, encoding="utf-8") as handle:
                written_lines = sum(1 for l in handle if l.strip())
            self.assertEqual(written_lines + total_dropped, 2000)


class AsyncLogOutputIsImportableTest(unittest.TestCase):
    """异步日志落盘的文件必须能被数据导入器直接导入并完整查询——打通
    "生产写日志 -> 数据层"这条链路，不是两套互不相干的实现。"""

    def test_async_logged_decisions_are_importable_and_queryable(self):
        with tempfile.TemporaryDirectory() as directory:
            real_log = DecisionLog(directory=directory)
            log = AsyncDecisionLog(real_log, maxsize=1000)

            snapshot = {
                "seat": 0, "phase": "draw", "turn": 0, "my_hand": ["1w", "2w"],
                "drawn_tile": "3w", "dealer": 0, "wall_remaining": 40,
                "god": {"chain_count": 1}, "melds": [[], [], [], []],
                "round_no": 3,
            }
            decision_id = log.action("g_async_1", snapshot, {"action": "discard", "tile": "1w"})
            log.close(timeout=5.0)

            path = os.path.join(directory, os.listdir(directory)[0])
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

            row = conn.execute(
                "SELECT decision_id, game_id, action, tile FROM decisions WHERE decision_id=?",
                (decision_id,),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["game_id"], "g_async_1")
            self.assertEqual(row["action"], "discard")
            self.assertEqual(row["tile"], "1w")


if __name__ == "__main__":
    unittest.main()
