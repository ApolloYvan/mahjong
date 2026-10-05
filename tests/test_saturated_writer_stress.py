"""目标三验收测试：慢盘饱和压力验收。

构造可控慢写入器 + 小容量队列，验证 P0-2/B2 的核心承诺：
- enqueue 不等待写线程（严格非阻塞，`queue.put_nowait` 是唯一的入队路径，
  不存在任何阻塞式 `queue.put`）；
- flush()=True 时底层 append() 已经真正完成（barrier/ack 语义，不是仅
  "队列看起来空了"）；
- 每个关键事件都满足：written + emergency_records + emergency_drop_count
  = submitted（不存在"既没写入、也没被计数、也没进紧急记录"的静默丢失）；
- 输出 enqueue p50/p95/p99/max，供人工核查（打印到 stdout，不做硬编码
  绝对值断言——不同机器性能不同，但用相对边界发现旧版 0.5 秒阻塞行为）。

超时边界选择：旧版 bug 是 `critical_block_timeout=0.5`（阻塞最多 0.5 秒/
条）。测试用的慢写入器 delay 远小于此（如 0.05-0.2 秒），但队列容量故意
设得很小、提交条数远超容量，制造持续满载。断言用"远小于 0.5 秒"的宽松
上限（如 50ms），足以在旧版阻塞实现下必然失败，同时对当前机器的正常抖动
留出充分余量，不依赖具体机器性能。
"""
import os
import queue
import tempfile
import threading
import time
import unittest

from mj.async_log import CRITICAL_KINDS, AsyncDecisionLog
from mj.logging import DecisionLog


def _percentile(sorted_values, pct):
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (len(sorted_values) - 1) * pct
    f = int(k)
    c = min(f + 1, len(sorted_values) - 1)
    if f == c:
        return sorted_values[f]
    return sorted_values[f] * (c - k) + sorted_values[c] * (k - f)


class ControllableSlowWriter:
    """可控慢写入器：每次 append() 阻塞 ``delay`` 秒才返回，模拟慢盘。
    记录每次调用发生的顺序与时间，供测试断言"写线程确实很慢"。"""

    def __init__(self, delay=0.05):
        self.delay = delay
        self.records = []
        self.lock = threading.Lock()

    def append(self, kind, payload):
        time.sleep(self.delay)
        with self.lock:
            self.records.append((kind, payload))


class NonBlockingEnqueueNeverWaitsForSlowWriterTests(unittest.TestCase):
    def test_saturated_enqueue_never_waits_for_slow_writer(self):
        """核心断言：小容量队列 + 慢写入器持续饱和的情况下，enqueue（即
        log.append()）的耗时必须远低于写入延迟本身——enqueue 不等待写
        线程完成，只做 queue.put_nowait。"""
        slow_writer = ControllableSlowWriter(delay=0.05)  # 每条写入耗时 50ms
        log = AsyncDecisionLog(slow_writer, maxsize=2)  # 容量极小，必然很快打满

        n = 200
        enqueue_latencies_ms = []
        for i in range(n):
            started = time.perf_counter()
            log.append("action_rejected", {"i": i, "decision_id": f"d{i}", "game_id": "g1"})
            enqueue_latencies_ms.append((time.perf_counter() - started) * 1000)

        enqueue_latencies_ms.sort()
        p50 = _percentile(enqueue_latencies_ms, 0.50)
        p95 = _percentile(enqueue_latencies_ms, 0.95)
        p99 = _percentile(enqueue_latencies_ms, 0.99)
        p_max = enqueue_latencies_ms[-1]

        print(f"\n[saturated-enqueue-evidence] n={n} writer_delay_ms=50 queue_maxsize=2 "
              f"enqueue_p50={p50:.4f}ms enqueue_p95={p95:.4f}ms "
              f"enqueue_p99={p99:.4f}ms enqueue_max={p_max:.4f}ms")

        # 宽松且稳定的上限：旧版 critical_block_timeout=0.5s（500ms）阻塞重试
        # 在这个场景下必然导致大量 enqueue 耗时逼近甚至等于 500ms。这里用
        # 50ms 作为上限——远高于正常机器上 queue.put_nowait 的正常耗时
        # （通常 <1ms），但仍能可靠地区分"非阻塞"与"旧版 0.5 秒阻塞"两种
        # 实现，不依赖具体机器性能的精确数值。
        self.assertLess(p99, 50.0,
                        "enqueue p99 latency too high — enqueue must not block waiting for "
                        "the slow writer thread (regression toward old 0.5s blocking retry)")
        self.assertLess(p_max, 200.0,
                        "enqueue max latency too high — even worst-case enqueue must stay far "
                        "below the old 500ms blocking-retry threshold")

        log.close(timeout=15.0)

    def test_no_blocking_queue_put_is_used_anywhere_in_enqueue_path(self):
        """静态验证：AsyncDecisionLog._enqueue 的实现只使用
        queue.Queue.put_nowait（本身在 CPython 内部实现为
        ``self.put(item, block=False)``），绝不会以 ``block=True``（或省略
        ``block`` 参数，默认值也是 True）调用 ``Queue.put`` ——用
        monkeypatch 拦截 ``Queue.put`` 调用，记录每次调用时的 ``block``
        参数，确认入队路径从不以阻塞模式触达它（这正是旧版
        ``critical_block_timeout`` 阻塞重试的实现方式：
        ``queue.put(item, block=True, timeout=...)``）。"""
        original_put = queue.Queue.put
        blocking_calls = {"count": 0}

        def _tripwire_put(self, item, block=True, timeout=None):
            if block:
                blocking_calls["count"] += 1
            return original_put(self, item, block=block, timeout=timeout)

        queue.Queue.put = _tripwire_put
        try:
            slow_writer = ControllableSlowWriter(delay=0.02)
            log = AsyncDecisionLog(slow_writer, maxsize=1)
            for i in range(50):
                log.append("state", {"i": i})
                log.append("action_rejected", {"i": i, "decision_id": f"d{i}"})
            log.close(timeout=10.0)
        finally:
            queue.Queue.put = original_put
        self.assertEqual(blocking_calls["count"], 0,
                         "blocking queue.Queue.put(block=True) must never be called by the "
                         "enqueue path — only put_nowait()/put(block=False) is allowed")


class FlushImpliesUnderlyingAppendCompletedTests(unittest.TestCase):
    def test_flush_true_means_append_already_completed_under_saturation(self):
        """饱和写入场景下，flush()=True 必须意味着底层 append() 已经真正
        完成（不是仅仅"入队序号已确认"这种表面语义）——用带时间戳的慢
        写入器验证：flush() 返回的时刻，最后一条记录的完成时间戳已经早于
        或等于当前时刻。"""
        class TimestampedSlowWriter:
            def __init__(self, delay):
                self.delay = delay
                self.completed_at = []
                self.lock = threading.Lock()

            def append(self, kind, payload):
                time.sleep(self.delay)
                with self.lock:
                    self.completed_at.append(time.monotonic())

        writer = TimestampedSlowWriter(delay=0.03)
        log = AsyncDecisionLog(writer, maxsize=3)
        for i in range(20):
            log.append("action_rejected", {"i": i})
        ok = log.flush(timeout=10.0)
        flush_returned_at = time.monotonic()
        self.assertTrue(ok)
        self.assertGreater(len(writer.completed_at), 0)
        # flush() 返回的时刻，必须晚于（或等于）最后一条记录真正完成写入
        # 的时刻——不能是 flush() 先返回、写入还在异步进行中。
        self.assertGreaterEqual(flush_returned_at, writer.completed_at[-1])
        log.close(timeout=5.0)


class EventAccountingUnderSaturationTests(unittest.TestCase):
    def test_every_critical_event_is_written_or_counted_never_silently_lost(self):
        """核心事件核算断言：written + emergency_records + emergency_drop_count
        == submitted。对每一个关键 kind（CRITICAL_KINDS）在小队列 + 慢写入器
        持续饱和场景下提交事件，验证三者之和恰好等于提交总数——不存在
        "既没写入、也没被计入紧急丢弃"的静默消失。

        本测试刻意把提交总量控制在紧急记录环形缓冲容量（默认 500）以内，
        使得 emergency_records 长度与 emergency_drop_count 严格相等，可以
        直接验证任务书字面要求的三者求和公式；另有一个独立测试
        （见下方 docstring）验证\"当紧急丢弃超过环形缓冲容量时，只截断
        emergency_records 快照，written+emergency_drop_count 仍然精确
        等于 submitted\"这一更极端的场景。
        """
        with tempfile.TemporaryDirectory() as directory:
            real_log = DecisionLog(directory=directory)
            slow_writer_delay_log = _DelayWrapper(real_log, delay=0.01)
            log = AsyncDecisionLog(slow_writer_delay_log, maxsize=3)

            submitted = 0
            n_per_kind = 20  # 10 个 CRITICAL_KINDS * 20 = 200 条，远小于紧急缓冲容量 500
            for kind in sorted(CRITICAL_KINDS):
                for i in range(n_per_kind):
                    payload = {"decision_id": f"{kind}_{i}", "game_id": "g1", "i": i}
                    log.append(kind, payload)
                    submitted += 1

            log.close(timeout=20.0)

            written = len(slow_writer_delay_log.records)
            emergency_drop = log.emergency_drop_count
            emergency_records_count = len(log.emergency_records)

            print(f"\n[event-accounting-evidence] submitted={submitted} written={written} "
                  f"emergency_drop_count={emergency_drop} "
                  f"emergency_records_len={emergency_records_count}")

            # 任务书字面要求：written + emergency_records + emergency_drop_count
            # == submitted。由于本场景提交量未超过环形缓冲容量，
            # emergency_records 长度与 emergency_drop_count 严格相等，
            # 因此 written + emergency_records == written + emergency_drop_count
            # == submitted（两种写法在本场景下等价，都必须成立）。
            self.assertEqual(emergency_records_count, emergency_drop)
            self.assertEqual(written + emergency_records_count, submitted,
                             "written + emergency_records must equal submitted total "
                             "(no event may silently disappear)")
            self.assertEqual(written + emergency_drop, submitted,
                             "written + emergency_drop_count must equal submitted total "
                             "(no event may silently disappear)")

    def test_event_accounting_holds_even_when_emergency_buffer_capacity_is_exceeded(self):
        """极端场景：紧急丢弃次数超过环形缓冲固定容量时，emergency_records
        只保留最近的一部分（旧记录被覆盖，符合\"有限窗口，不无限增长内存\"
        的设计要求），但 written + emergency_drop_count（计数器，不是快照
        长度）必须依然精确等于 submitted——计数不会因为缓冲区溢出而失真。"""
        with tempfile.TemporaryDirectory() as directory:
            real_log = DecisionLog(directory=directory)
            slow_writer_delay_log = _DelayWrapper(real_log, delay=0.01)
            log = AsyncDecisionLog(slow_writer_delay_log, maxsize=3, emergency_capacity=50)

            submitted = 0
            for i in range(600):  # 远超 emergency_capacity=50
                log.append("action_rejected", {"decision_id": f"d{i}", "game_id": "g1"})
                submitted += 1
            log.close(timeout=20.0)

            written = len(slow_writer_delay_log.records)
            emergency_drop = log.emergency_drop_count
            emergency_records_count = len(log.emergency_records)
            print(f"\n[event-accounting-overflow-evidence] submitted={submitted} "
                  f"written={written} emergency_drop_count={emergency_drop} "
                  f"emergency_records_len={emergency_records_count}")

            self.assertEqual(written + emergency_drop, submitted)
            self.assertLessEqual(emergency_records_count, 50)

    def test_droppable_kinds_accounting_written_plus_dropped_equals_submitted(self):
        """对可丢弃 kind（DROPPABLE_KINDS）同样验证：written + dropped_count
        == submitted（可丢弃事件允许被丢弃，但必须被计数，不能既不写入也
        不计数）。"""
        with tempfile.TemporaryDirectory() as directory:
            real_log = DecisionLog(directory=directory)
            slow_writer_delay_log = _DelayWrapper(real_log, delay=0.01)
            log = AsyncDecisionLog(slow_writer_delay_log, maxsize=2)

            submitted = 0
            for i in range(300):
                log.append("state", {"game_id": "g1", "i": i})
                submitted += 1

            log.close(timeout=20.0)
            written = len(slow_writer_delay_log.records)
            dropped = sum(log.dropped_count.values())
            print(f"\n[droppable-accounting-evidence] submitted={submitted} written={written} "
                  f"dropped={dropped}")
            self.assertEqual(written + dropped, submitted)


class _DelayWrapper:
    """在真实 DecisionLog.append() 前后加一个受控延迟，同时透传给底层
    对象记录真实写入次数（供 EventAccountingUnderSaturationTests 统计
    written 数量）——比直接用 FakeSlowLog 更贴近生产：真正走一次磁盘
    JSONL 写入 + 延迟叠加。"""

    def __init__(self, inner, delay):
        self._inner = inner
        self.delay = delay
        self.records = []
        self.lock = threading.Lock()

    def append(self, kind, payload):
        time.sleep(self.delay)
        self._inner.append(kind, payload)
        with self.lock:
            self.records.append((kind, payload))


if __name__ == "__main__":
    unittest.main()
