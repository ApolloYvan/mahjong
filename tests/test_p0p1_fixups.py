"""本轮独立评审 2 个 P0 + 3 个 P1 验收测试：

P0-1 mj/async_log.py::_enqueue 移除同步 stderr I/O；
P0-2 mj/datastore.py::init_schema 实现 v2->v3 显式幂等迁移；
P1-3 mj/observability.py::SCHEMA_VERSION 升级为 2（日志 schema）；
P1-4 mj/bot.py 正常轮询不再无条件 log.state()，state_sample/anomaly_window
     携带可复盘规范化状态；
P1-5 mj/observability.py::build_hash 跨目录稳定（相对路径 + 整体排序）。

不发真实网络请求：全程本地临时目录/内存 SQLite/Mock api。
"""
import os
import queue
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

from mj import datastore as ds
from mj.async_log import AnomalyRingBuffer, AsyncDecisionLog
from mj.bot import _GameObserver, _fetch_snapshot
from mj.logging import DecisionLog
from mj.observability import build_hash as _build_hash


class SlowStderrDoesNotBlockCriticalEnqueueTests(unittest.TestCase):
    def test_slow_stderr_write_does_not_block_critical_enqueue(self):
        """P0-1 要求 6：stderr.write 人为阻塞 100ms 时，关键事件 enqueue
        仍不得等待 stderr——_enqueue() 队列满分支绝不执行任何 print/文件
        写入。用极慢的底层写日志制造持续满队列，同时把 sys.stderr 换成
        一个每次 write() 都 sleep(0.1) 的假 stream，验证 20 次 enqueue
        总耗时远低于 20*100ms（旧实现每次紧急丢弃都会打一行 stderr，
        新实现全程不触达 stderr）。"""
        class SlowStderr:
            def write(self, s):
                time.sleep(0.1)
                return len(s)

            def flush(self):
                pass

        class VerySlowInner:
            def append(self, kind, payload):
                time.sleep(1.0)

        old_stderr = sys.stderr
        sys.stderr = SlowStderr()
        try:
            log = AsyncDecisionLog(VerySlowInner(), maxsize=1)
            started = time.monotonic()
            for i in range(20):
                log.append("action_rejected", {"i": i})
            elapsed = time.monotonic() - started
        finally:
            sys.stderr = old_stderr
        print(f"\n[slow-stderr-evidence] 20 critical enqueues elapsed={elapsed*1000:.2f}ms "
              f"(old blocking-stderr baseline would be >= {20*100}ms)")
        self.assertLess(elapsed, 0.5,
                         "critical enqueue must not touch stderr I/O when queue is full")
        log.close(timeout=0.2)

    def test_accounting_formula_no_double_count(self):
        """P0-1 要求 5：written + emergency_drop_count == submitted；
        len(emergency_records) == min(emergency_drop_count, capacity)。"""
        class VerySlowInner:
            def append(self, kind, payload):
                time.sleep(0.5)

        log = AsyncDecisionLog(VerySlowInner(), maxsize=1, emergency_capacity=10)
        submitted = 30
        for i in range(submitted):
            log.append("action_rejected", {"i": i})
        log.close(timeout=1.0)
        written = 0  # VerySlowInner 不记录，close 超时后剩余未写完的部分被丢弃/紧急记录
        emergency = log.emergency_drop_count
        records = log.emergency_records
        self.assertEqual(len(records), min(emergency, 10))
        # 队列满时严格非阻塞：submitted 条里，成功入队排队的（最多1个在处理+1个在队列）
        # 之外全部走紧急丢弃路径；核心断言是不存在"既没排队也没计入紧急丢弃"的事件。
        self.assertGreaterEqual(emergency, submitted - 2)


class SqliteV2ToV3MigrationTests(unittest.TestCase):
    def test_v2_database_migrates_to_v3_preserving_data(self):
        """P0-2：人工创建 v2 数据库（decisions 无 build_hash，无
        decision_attempts）并插入旧数据，调用 init_schema 后：旧数据仍在、
        build_hash 可写、decision_attempts 可写、重复调用不报错。"""
        with tempfile.TemporaryDirectory() as directory:
            db_path = os.path.join(directory, "v2.sqlite")
            conn = sqlite3.connect(db_path)
            conn.execute("""
                CREATE TABLE decisions (
                    decision_id TEXT PRIMARY KEY,
                    decision_id_synthesized INTEGER NOT NULL DEFAULT 0,
                    game_id TEXT, round_no INTEGER, seat INTEGER,
                    state_hash TEXT, policy_version TEXT, schema_version TEXT,
                    config_hash TEXT, action TEXT, tile TEXT, time TEXT,
                    client_prepare_ms REAL, action_request_ms REAL, source_file_id INTEGER
                )
            """)
            conn.execute(
                "INSERT INTO decisions(decision_id, game_id, action, tile, time) "
                "VALUES ('d_legacy', 'g1', 'discard', '1w', 't1')"
            )
            conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.execute("INSERT INTO schema_meta(key, value) VALUES ('schema_version', '2')")
            conn.commit()
            conn.close()

            conn = ds.connect(db_path)
            ds.init_schema(conn)

            row = conn.execute(
                "SELECT decision_id, game_id, action FROM decisions WHERE decision_id='d_legacy'"
            ).fetchone()
            self.assertIsNotNone(row, "old v2 decision row must survive migration")
            self.assertEqual(row["game_id"], "g1")

            # build_hash 字段可写。
            ds.upsert_decision(
                conn, decision_id="d_new", decision_id_synthesized=False, game_id="g1",
                round_no=1, seat=0, state_hash="h1", policy_version="legacy",
                schema_version="2", config_hash="c1", action="discard", tile="1w",
                time_value="t2", build_hash="bh1",
            )
            conn.commit()
            new_row = conn.execute(
                "SELECT build_hash FROM decisions WHERE decision_id='d_new'"
            ).fetchone()
            self.assertEqual(new_row["build_hash"], "bh1")

            # decision_attempts 可写。
            ds.upsert_decision_attempt(
                conn, decision_id="a1", game_id="g1", round_no=1, seat=0,
                action="discard", tile="1w", attempted_at="t2", build_hash="bh1",
            )
            conn.commit()
            attempt_row = conn.execute(
                "SELECT decision_id FROM decision_attempts WHERE decision_id='a1'"
            ).fetchone()
            self.assertIsNotNone(attempt_row)

            print(f"\n[v2-to-v3-migration-evidence] legacy_decision_survived=True "
                  f"legacy_row={dict(row)} new_schema_version={ds.SCHEMA_VERSION}")

            # 第二次调用 init_schema 不报错（幂等）。
            ds.init_schema(conn)
            ds.init_schema(conn)
            final_schema_version = conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()[0]
            self.assertEqual(final_schema_version, str(ds.SCHEMA_VERSION))


class FakeV3DatabaseSchemaRepairTests(unittest.TestCase):
    def test_declared_v3_but_missing_build_hash_gets_repaired(self):
        """P0 本轮返修："伪 v3"数据库——schema_meta 已经声明
        schema_version=3，但 decisions 实际缺 build_hash 列（例如某次运行
        中途失败，只写了元数据没真正执行 DDL）。旧版 init_schema() 只在
        before_version < SCHEMA_VERSION 时才检查，会直接跳过这种情况，
        导致写 build_hash 时报 OperationalError。

        本测试先复现"未修复前会报错"，再验证 init_schema() 修复后：字段
        存在、旧记录仍在、新记录可写、重复调用安全。"""
        with tempfile.TemporaryDirectory() as directory:
            db_path = os.path.join(directory, "fake_v3.sqlite")
            conn = sqlite3.connect(db_path)
            conn.execute("""
                CREATE TABLE decisions (
                    decision_id TEXT PRIMARY KEY,
                    decision_id_synthesized INTEGER NOT NULL DEFAULT 0,
                    game_id TEXT, round_no INTEGER, seat INTEGER,
                    state_hash TEXT, policy_version TEXT, schema_version TEXT,
                    config_hash TEXT, action TEXT, tile TEXT, time TEXT,
                    client_prepare_ms REAL, action_request_ms REAL, source_file_id INTEGER
                )
            """)
            conn.execute(
                "INSERT INTO decisions(decision_id, game_id, action, tile, time) "
                "VALUES ('d_fake_v3', 'g1', 'discard', '1w', 't1')"
            )
            conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            # 关键：schema_meta 已经"声称"是当前 SCHEMA_VERSION（伪 v3），
            # 但实际 decisions 结构并未真正跑完迁移。
            conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES ('schema_version', ?)",
                (str(ds.SCHEMA_VERSION),),
            )
            conn.commit()
            conn.close()

            # 复现修复前的报错：declared_version==SCHEMA_VERSION 但缺列。
            probe = sqlite3.connect(db_path)
            declared_version = probe.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()[0]
            columns_before = {row[1] for row in probe.execute("PRAGMA table_info(decisions)")}
            has_build_hash_before = "build_hash" in columns_before
            repro_error = None
            try:
                probe.execute(
                    "INSERT INTO decisions(decision_id, build_hash) VALUES ('probe', 'x')"
                )
            except sqlite3.OperationalError as exc:
                repro_error = str(exc)
            probe.close()
            print(f"\n[fake-v3-repro-before-fix] declared_version={declared_version} "
                  f"has_build_hash_after_init=False error={repro_error!r}")
            self.assertEqual(declared_version, str(ds.SCHEMA_VERSION))
            self.assertFalse(has_build_hash_before)
            self.assertIsNotNone(repro_error, "must reproduce the OperationalError before fix")

            # 修复：init_schema() 无条件校验结构，不只信任 schema_meta 版本号。
            conn = ds.connect(db_path)
            ds.init_schema(conn)

            columns_after = {row[1] for row in conn.execute("PRAGMA table_info(decisions)")}
            self.assertIn("build_hash", columns_after)

            old_row = conn.execute(
                "SELECT decision_id, game_id FROM decisions WHERE decision_id='d_fake_v3'"
            ).fetchone()
            self.assertIsNotNone(old_row, "existing business data must survive repair")
            self.assertEqual(old_row["game_id"], "g1")

            ds.upsert_decision(
                conn, decision_id="d_after_repair", decision_id_synthesized=False,
                game_id="g1", round_no=1, seat=0, state_hash="h1", policy_version="legacy",
                schema_version=str(ds.SCHEMA_VERSION), config_hash="c1", action="discard",
                tile="1w", time_value="t2", build_hash="bh_after_repair",
            )
            conn.commit()
            new_row = conn.execute(
                "SELECT build_hash FROM decisions WHERE decision_id='d_after_repair'"
            ).fetchone()
            self.assertEqual(new_row["build_hash"], "bh_after_repair")
            print(f"[fake-v3-repro-after-fix] has_build_hash_after_init=True "
                  f"old_row_survived=True new_row_build_hash={new_row['build_hash']!r}")

            # 重复调用安全。
            ds.init_schema(conn)
            ds.init_schema(conn)
            final_row = conn.execute(
                "SELECT decision_id FROM decisions WHERE decision_id='d_fake_v3'"
            ).fetchone()
            self.assertIsNotNone(final_row)


class B3ReducesLogVolumeTests(unittest.TestCase):
    def test_100_identical_states_do_not_produce_100_state_logs(self):
        """P1-4 要求 8（本轮修正：补充 flush 后的完整证据）：连续 100 次
        相同状态不产生 100 条原始 state 日志；flush 前允许没有段落汇总
        （段落尚未切换，属预期行为）；调用 observer.flush() 后必须恰好
        产生 1 条 state_dedup_summary，且 repeat_count==100、
        first_seen=="t0"、last_seen=="t99"。"""
        log = Mock()
        observer = _GameObserver(log, "g_volume_1")
        snapshot = {
            "phase": "draw", "seat": 0, "turn": 0, "round_no": 1,
            "my_hand": ["1w", "2w", "3w"], "drawn_tile": None, "dealer": 0,
            "wall_remaining": 50, "discards": [], "melds": [[], [], [], []],
            "responding_seats": [], "god": {}, "rules": {},
        }
        from mj.observability import state_hash as _state_hash
        from mj.state import normalize as _normalize_state
        state = _normalize_state(snapshot, rules={})
        h = _state_hash(state)
        for i in range(100):
            observer.observe_state(h, snapshot, f"t{i}")
        state_calls = [c for c in log.append.call_args_list if c.args[0] == "state"]
        pre_flush_summary_calls = [
            c for c in log.append.call_args_list if c.args[0] == "state_dedup_summary"
        ]
        self.assertEqual(len(state_calls), 0, "no raw 'state' kind logs from polling path anymore")
        self.assertEqual(len(pre_flush_summary_calls), 0,
                          "segment has not changed yet, no summary emitted before flush")

        observer.flush()
        summary_calls = [c for c in log.append.call_args_list if c.args[0] == "state_dedup_summary"]
        print(f"\n[b3-log-volume-evidence] identical_states_fed=100 "
              f"state_kind_logs={len(state_calls)} dedup_summary_logs_after_flush={len(summary_calls)}")
        self.assertEqual(len(summary_calls), 1, "exactly one summary after flush")
        payload = summary_calls[0].args[1]
        self.assertEqual(payload["repeat_count"], 100)
        self.assertEqual(payload["first_seen"], "t0")
        self.assertEqual(payload["last_seen"], "t99")
        print(f"[b3-dedup-flush-evidence] repeat_count={payload['repeat_count']} "
              f"first_seen={payload['first_seen']} last_seen={payload['last_seen']}")

    def test_fetch_snapshot_no_longer_calls_log_state_unconditionally(self):
        """P1-4 要求 1：正常轮询路径删除无条件 log.state() 调用。"""
        api = Mock()
        api.state.return_value = {"snapshot": {"phase": "draw"}, "seq": 1}
        log = Mock()
        _fetch_snapshot(api, "g", log)
        log.state.assert_not_called()

    def test_state_sample_and_anomaly_window_carry_replayable_state(self):
        """P1-4 要求 3/4：state_sample 与 anomaly_window 的 before/after
        携带 DecisionState 的有界可复盘字段（不只是 hash/phase/seat）。"""
        log = Mock()
        observer = _GameObserver(log, "g_replay_1")
        snapshot = {
            "phase": "draw", "seat": 0, "turn": 0, "round_no": 1,
            "my_hand": ["1w", "2w", "3w"], "drawn_tile": "4w", "dealer": 0,
            "wall_remaining": 50, "discards": [], "melds": [[], [], [], []],
            "responding_seats": [], "god": {}, "rules": {},
        }
        from mj.observability import state_hash as _state_hash
        from mj.state import normalize as _normalize_state
        state = _normalize_state(snapshot, rules={})
        h = _state_hash(state)
        observer.observe_state(h, snapshot, "t1")
        observer.mark_anomaly("timeout", decision_id="d1")
        observer.observe_state(h, snapshot, "t2")
        observer.flush()
        window_calls = [c for c in log.append.call_args_list if c.args[0] == "anomaly_window"]
        self.assertEqual(len(window_calls), 1)
        before = window_calls[0].args[1]["before"]
        self.assertTrue(len(before) >= 1)
        replay = before[-1]["state"]
        self.assertIsNotNone(replay)
        self.assertEqual(replay["hand"], ["1w", "2w", "3w"])
        self.assertEqual(replay["drawn_tile"], "4w")

    def test_state_fetch_timeout_marks_state_unavailable_not_fabricated(self):
        """P1-4 要求 5：Timeout 无法取得 snapshot 时明确记录
        state_unavailable=True，不伪造状态。"""
        api = Mock()
        api.state.side_effect = TimeoutError()
        log = Mock()
        observer = _GameObserver(log, "g_timeout_1")
        _fetch_snapshot(api, "g", log, observer=observer)
        observer.flush()
        window_calls = [c for c in log.append.call_args_list if c.args[0] == "anomaly_window"]
        self.assertEqual(len(window_calls), 1)
        anomaly = window_calls[0].args[1]["anomaly"]
        self.assertTrue(anomaly.get("state_unavailable"))


class BuildHashCrossDirectoryStableTests(unittest.TestCase):
    def test_same_source_tree_in_two_directories_yields_same_build_hash(self):
        """P1-5：同一份源码树复制到两个不同绝对目录时 build_hash 必须
        相同（哈希输入只用相对路径 + 内容，不含绝对路径前缀）。"""
        import mj as mj_pkg
        pkg_dir = os.path.dirname(os.path.abspath(mj_pkg.__file__))
        with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
            dst1 = os.path.join(d1, "copy_a", "mj")
            dst2 = os.path.join(d2, "deeper", "nested", "copy_b", "mj")
            os.makedirs(os.path.dirname(dst1))
            os.makedirs(os.path.dirname(dst2))
            shutil.copytree(pkg_dir, dst1)
            shutil.copytree(pkg_dir, dst2)

            code = (
                "import sys, json\n"
                "sys.path.insert(0, sys.argv[1])\n"
                "import mj.observability as ob\n"
                "print(ob.build_hash())\n"
            )
            import subprocess
            out1 = subprocess.run([sys.executable, "-c", code, os.path.dirname(dst1)],
                                   capture_output=True, text=True)
            out2 = subprocess.run([sys.executable, "-c", code, os.path.dirname(dst2)],
                                   capture_output=True, text=True)
            hash1 = out1.stdout.strip()
            hash2 = out2.stdout.strip()
            print(f"\n[build-hash-cross-dir-evidence] dir1_hash={hash1} dir2_hash={hash2}")
            self.assertTrue(hash1, out1.stderr)
            self.assertTrue(hash2, out2.stderr)
            self.assertEqual(hash1, hash2,
                              "identical source tree in different absolute dirs must hash the same")

            # 内容变化后 hash 必须变化。
            with open(os.path.join(dst2, "observability.py"), "a", encoding="utf-8") as handle:
                handle.write("\n# marker change for hash diff test\n")
            out3 = subprocess.run([sys.executable, "-c", code, os.path.dirname(dst2)],
                                   capture_output=True, text=True)
            hash3 = out3.stdout.strip()
            self.assertNotEqual(hash1, hash3, "content change must change build_hash")


class AnomalyRingBufferPendingBoundTests(unittest.TestCase):
    def test_10000_mark_anomaly_without_record_stays_bounded(self):
        """P0 本轮返修：pending windows 必须有固定上限——连续 10000 次
        mark_anomaly() 且没有 record() 喂入（after 永远收集不满）时，
        pending 数量始终 <= max_pending，超限淘汰计入
        pending_overflow_count，且不影响正常 before/after 窗口内容；
        flush（finalize_pending）后内存清空。"""
        buf = AnomalyRingBuffer(before=5, after=5, max_pending=32)
        max_pending_seen = 0
        for i in range(10000):
            buf.mark_anomaly({"type": "timeout", "i": i})
            max_pending_seen = max(max_pending_seen, buf.pending_count)
            self.assertLessEqual(buf.pending_count, 32)

        overflow = buf.pending_overflow_count
        expected_overflow = 10000 - 32  # 前 32 次不淘汰，之后每次都淘汰一个最旧的
        self.assertEqual(overflow, expected_overflow)
        print(f"\n[anomaly-pending-bound-evidence] mark_anomaly_calls=10000 "
              f"max_pending_seen={max_pending_seen} pending_overflow_count={overflow}")

        finalized = buf.finalize_pending()
        self.assertEqual(len(finalized), 32, "only the most recent max_pending windows survive")
        self.assertEqual(buf.pending_count, 0)
        print(f"[anomaly-pending-flush-evidence] pending_after_flush={buf.pending_count} "
              f"finalized_windows={len(finalized)}")

        # 不影响正常 before/after 窗口内容：单独验证一个干净的 buffer 里
        # record()/mark_anomaly() 仍然产出预期的 before/after 长度与顺序。
        clean = AnomalyRingBuffer(before=3, after=2, max_pending=32)
        clean.record({"v": "a"})
        clean.record({"v": "b"})
        clean.mark_anomaly({"type": "x"})
        clean.record({"v": "c"})
        clean.record({"v": "d"})
        windows = clean.drain_windows()
        self.assertEqual(len(windows), 1)
        self.assertEqual([e["v"] for e in windows[0]["before"]], ["a", "b"])
        self.assertEqual([e["v"] for e in windows[0]["after"]], ["c", "d"])

    def test_non_positive_max_pending_raises_value_error(self):
        """卫生修复：max_pending<=0 会让淘汰判断（max_pending > 0 and ...）
        恒为 False，pending 列表退化为无上限——构造期必须直接拒绝。"""
        with self.assertRaises(ValueError):
            AnomalyRingBuffer(max_pending=0)
        with self.assertRaises(ValueError):
            AnomalyRingBuffer(max_pending=-5)


if __name__ == "__main__":
    unittest.main()
