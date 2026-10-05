"""mj.async_log 契约测试：有界队列+单写线程、关键事件不静默丢弃、
flush/close 排空语义、写线程异常不阻塞/杀死调用线程、并发生产者安全、
稳定采样、连续 state_hash 去重、异常前后环形窗口。
"""
import json
import os
import tempfile
import threading
import time
import unittest

from mj.async_log import AnomalyRingBuffer, AsyncDecisionLog, ConsecutiveStateDeduper
from mj.logging import DecisionLog
from mj.observability import stable_sample_state


class FakeSlowLog:
    """模拟慢速/间歇故障的底层日志，供测试写线程行为。"""

    def __init__(self, delay=0.0, fail_times=0):
        self.records = []
        self.delay = delay
        self.fail_times = fail_times
        self.lock = threading.Lock()

    def append(self, kind, payload):
        if self.delay:
            time.sleep(self.delay)
        with self.lock:
            if self.fail_times > 0:
                self.fail_times -= 1
                raise OSError("simulated disk error")
            self.records.append((kind, payload))


class BasicEnqueueTests(unittest.TestCase):
    def test_append_is_written_by_background_thread(self):
        inner = FakeSlowLog()
        log = AsyncDecisionLog(inner, maxsize=100)
        log.append("state", {"game_id": "g1"})
        self.assertTrue(log.flush(timeout=2.0))
        log.close(timeout=2.0)
        self.assertEqual(len(inner.records), 1)
        self.assertEqual(inner.records[0][0], "state")

    def test_action_method_returns_decision_id_and_enqueues(self):
        inner = FakeSlowLog()
        log = AsyncDecisionLog(inner, maxsize=100)
        snapshot = {"seat": 0, "phase": "draw", "my_hand": [], "melds": [[], [], [], []]}
        decision_id = log.action("g1", snapshot, {"action": "pass"})
        self.assertIsNotNone(decision_id)
        log.close(timeout=2.0)
        self.assertEqual(len(inner.records), 1)
        self.assertEqual(inner.records[0][1]["decision_id"], decision_id)


class CriticalNeverSilentlyDroppedTests(unittest.TestCase):
    def test_droppable_kind_is_dropped_when_queue_full_and_counted(self):
        inner = FakeSlowLog(delay=0.05)  # 写线程慢，制造队列积压
        log = AsyncDecisionLog(inner, maxsize=2)
        for i in range(20):
            log.append("state", {"i": i})  # 'state' 是可丢弃 kind
        time.sleep(0.3)
        log.close(timeout=3.0)
        dropped = log.dropped_count
        self.assertIn("state", dropped)
        self.assertGreater(dropped["state"], 0)

    def test_critical_kind_is_never_silently_dropped_under_pressure(self):
        """关键事件（action/action_rejected/fallback_sent/hu_detail/error/
        result）即使队列持续满，也不能悄无声息消失——要么最终写入，要么
        被计入 emergency_drop_count 并进入 emergency_records（可观测），
        不能既不写入又不计数。P0-2 返修：入队严格非阻塞，不存在阻塞重试，
        队列满时立即判定为紧急丢弃。"""
        inner = FakeSlowLog(delay=0.02)
        log = AsyncDecisionLog(inner, maxsize=1)
        n = 30
        for i in range(n):
            log.append("action_rejected", {"i": i, "decision_id": f"d{i}"})
        log.close(timeout=10.0)
        written = sum(1 for kind, _ in inner.records if kind == "action_rejected")
        emergency = log.emergency_drop_count
        self.assertEqual(written + emergency, n,
                         "every critical event must be either written or counted as emergency drop")
        if emergency:
            records = log.emergency_records
            self.assertGreater(len(records), 0)
            self.assertTrue(all(r["kind"] == "action_rejected" for r in records))

    def test_critical_enqueue_never_blocks_caller_even_when_queue_stays_full(self):
        """P0-2 硬性要求：关键事件的 enqueue 必须严格非阻塞——即使队列
        持续满，调用线程也不能有任何形式的阻塞重试（旧版
        critical_block_timeout 阻塞重试已删除）。"""
        inner = FakeSlowLog(delay=1.0)  # 写线程极慢，队列必然持续处于满状态
        log = AsyncDecisionLog(inner, maxsize=1)
        started = time.monotonic()
        for i in range(20):
            log.append("action_rejected", {"i": i})
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.5, "critical-kind enqueue must not block the caller thread")
        log.close(timeout=0.2)  # 允许尾部损失，不阻塞测试太久

    def test_dropped_count_is_broken_down_by_kind(self):
        inner = FakeSlowLog(delay=0.05)
        log = AsyncDecisionLog(inner, maxsize=1)
        for _ in range(10):
            log.append("state", {})
        for _ in range(10):
            log.append("notify", {"game_id": "g1"})
        time.sleep(0.3)
        log.close(timeout=3.0)
        dropped = log.dropped_count
        self.assertIn("state", dropped)
        self.assertIn("notify", dropped)


class FlushCloseTimeoutTests(unittest.TestCase):
    def test_flush_returns_true_when_queue_drains_before_timeout(self):
        inner = FakeSlowLog()
        log = AsyncDecisionLog(inner, maxsize=100)
        log.append("state", {"game_id": "g1"})
        self.assertTrue(log.flush(timeout=2.0))
        log.close(timeout=1.0)

    def test_flush_true_means_record_already_durably_written_barrier_ack(self):
        """P0-2 durability 断言：flush() 返回 True 时，之前入队的记录必须
        已经真正完成 inner.append()（不是仅仅"从队列取出"）。用一个会在
        append() 内人为延迟的慢写日志验证：flush() 返回的时刻，
        inner.records 里已经包含该条记录——不是先返回 True 再异步落盘。"""
        inner = FakeSlowLog(delay=0.15)
        log = AsyncDecisionLog(inner, maxsize=100)
        log.append("state", {"game_id": "durability-check"})
        ok = log.flush(timeout=5.0)
        self.assertTrue(ok)
        # flush() 返回 True 的那一刻，记录必须已经真正出现在底层存储里。
        self.assertEqual(len(inner.records), 1)
        self.assertEqual(inner.records[0][1]["game_id"], "durability-check")
        log.close(timeout=1.0)

    def test_flush_after_write_error_still_acks_not_hangs(self):
        """写入异常（被捕获、计入 write_error_count）也要被视为"已处理"，
        flush() 不能因为某条记录写入失败就永远等不到 ack。"""
        inner = FakeSlowLog(fail_times=1)
        log = AsyncDecisionLog(inner, maxsize=100)
        log.append("state", {"i": 0})  # 这条会触发 fail_times 的那次失败
        self.assertTrue(log.flush(timeout=2.0))
        self.assertGreaterEqual(log.write_error_count, 1)
        log.close(timeout=1.0)

    def test_flush_returns_false_on_timeout_when_writer_stuck(self):
        inner = FakeSlowLog(delay=1.0)  # 写一条要1秒
        log = AsyncDecisionLog(inner, maxsize=100)
        for _ in range(5):
            log.append("state", {})
        # 5条 * 1秒/条 远大于 0.2秒超时，flush 应该超时返回 False。
        self.assertFalse(log.flush(timeout=0.2))
        log.close(timeout=0.1)  # 允许尾部损失，不阻塞测试太久

    def test_close_drains_queue_under_normal_conditions(self):
        inner = FakeSlowLog()
        log = AsyncDecisionLog(inner, maxsize=100)
        for i in range(50):
            log.append("state", {"i": i})
        drained = log.close(timeout=5.0)
        self.assertTrue(drained)
        self.assertEqual(len(inner.records), 50)


class WriterExceptionDoesNotBlockCallerTests(unittest.TestCase):
    def test_writer_io_exception_does_not_propagate_to_caller_thread(self):
        inner = FakeSlowLog(fail_times=3)
        log = AsyncDecisionLog(inner, maxsize=100)
        # append() 本身绝不应该抛异常，即使底层写线程稍后遇到 IO 错误。
        for i in range(5):
            log.append("state", {"i": i})
        log.close(timeout=3.0)
        self.assertGreaterEqual(log.write_error_count, 1)
        # 前3次失败后的记录仍应被写入（不因为异常永久卡死写线程）。
        self.assertEqual(len(inner.records), 2)

    def test_caller_thread_never_blocks_on_writer_exception(self):
        inner = FakeSlowLog(fail_times=1000)  # 一直失败
        log = AsyncDecisionLog(inner, maxsize=100)
        started = time.monotonic()
        for i in range(20):
            log.append("state", {"i": i})
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.0, "caller thread must not block on writer failures")
        log.close(timeout=2.0)


class ConcurrentProducersTests(unittest.TestCase):
    def test_multiple_game_threads_do_not_corrupt_output(self):
        with tempfile.TemporaryDirectory() as directory:
            real_log = DecisionLog(directory=directory)
            log = AsyncDecisionLog(real_log, maxsize=5000)

            def producer(tid):
                for i in range(100):
                    log.append("state", {"thread": tid, "i": i})

            threads = [threading.Thread(target=producer, args=(t,)) for t in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            log.close(timeout=10.0)

            # 校验落盘文件每一行都是合法 JSON（未被并发写坏），且总行数
            # 与 10*100 - dropped 一致。
            path = os.path.join(directory, os.listdir(directory)[0])
            with open(path, encoding="utf-8") as handle:
                lines = [l for l in handle if l.strip()]
            for line in lines:
                json.loads(line)  # 抛异常即视为测试失败
            dropped_total = sum(log.dropped_count.values())
            self.assertEqual(len(lines) + dropped_total, 1000)


class StableSamplingTests(unittest.TestCase):
    def test_same_state_hash_same_sample_rate_gives_consistent_decision(self):
        for _ in range(20):
            a = stable_sample_state("hash_abc", sample_rate=0.3)
            b = stable_sample_state("hash_abc", sample_rate=0.3)
            self.assertEqual(a, b)

    def test_different_hashes_are_not_all_same_decision(self):
        results = {stable_sample_state(f"hash_{i}", sample_rate=0.3) for i in range(200)}
        # 200个不同哈希，采样率0.3，应该同时出现True和False（不是全部一样）。
        self.assertEqual(results, {True, False})

    def test_empty_state_hash_never_sampled(self):
        self.assertFalse(stable_sample_state("", sample_rate=1.0))
        self.assertFalse(stable_sample_state(None, sample_rate=1.0))


class ConsecutiveStateDeduperTests(unittest.TestCase):
    def test_repeated_hash_does_not_emit_until_change(self):
        dedup = ConsecutiveStateDeduper()
        self.assertIsNone(dedup.observe("h1", "t1"))
        self.assertIsNone(dedup.observe("h1", "t2"))
        self.assertIsNone(dedup.observe("h1", "t3"))
        summary = dedup.observe("h2", "t4")
        self.assertEqual(summary["state_hash"], "h1")
        self.assertEqual(summary["first_seen"], "t1")
        self.assertEqual(summary["last_seen"], "t3")
        self.assertEqual(summary["repeat_count"], 3)

    def test_flush_returns_last_segment(self):
        dedup = ConsecutiveStateDeduper()
        dedup.observe("h1", "t1")
        dedup.observe("h1", "t2")
        summary = dedup.flush()
        self.assertEqual(summary["repeat_count"], 2)

    def test_flush_on_empty_returns_none(self):
        dedup = ConsecutiveStateDeduper()
        self.assertIsNone(dedup.flush())


class AnomalyRingBufferTests(unittest.TestCase):
    def test_before_window_captures_recent_normal_events(self):
        buf = AnomalyRingBuffer(before=3, after=2)
        for i in range(5):
            buf.record({"i": i})
        buf.mark_anomaly({"kind": "action_rejected"})
        buf.record({"i": 5})
        buf.record({"i": 6})
        windows = buf.drain_windows()
        self.assertEqual(len(windows), 1)
        window = windows[0]
        self.assertEqual([e["i"] for e in window["before"]], [2, 3, 4])
        self.assertEqual([e["i"] for e in window["after"]], [5, 6])

    def test_pending_window_not_drained_until_after_filled(self):
        buf = AnomalyRingBuffer(before=2, after=3)
        buf.record({"i": 0})
        buf.mark_anomaly({"kind": "error"})
        buf.record({"i": 1})
        self.assertEqual(buf.drain_windows(), [])  # after 还没收集够
        buf.record({"i": 2})
        buf.record({"i": 3})
        windows = buf.drain_windows()
        self.assertEqual(len(windows), 1)

    def test_finalize_pending_returns_incomplete_windows_at_stream_end(self):
        buf = AnomalyRingBuffer(before=2, after=5)
        buf.mark_anomaly({"kind": "error"})
        buf.record({"i": 0})
        windows = buf.finalize_pending()
        self.assertEqual(len(windows), 1)
        self.assertEqual(len(windows[0]["after"]), 1)  # 不足5条也要返回


if __name__ == "__main__":
    unittest.main()
