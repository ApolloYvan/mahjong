"""B2/B3 观测生产闭环：有界队列+单写线程的异步日志包装，以及减量/异常窗口
辅助结构。

设计原则（任务书 B2/B3 硬性要求，P0-2 返修后）：
- 动作线程所有 enqueue 严格非阻塞：``_enqueue()`` 只做
  ``queue.put_nowait()``，绝不存在任何形式的阻塞重试（历史版本的
  ``critical_block_timeout`` 阻塞重试已删除——短暂阻塞也是阻塞，在对局
  线程的时间预算里同样不可接受）；
- 队列满时按优先级处理：高频低信息量的 kind（state/notify/piao_attempt/
  pass_window）直接丢弃并计数；action/action_rejected/fallback_sent/
  hu_detail/error/result 等关键事件永不阻塞、也永不"悄无声息"丢失——
  入队失败时立即记录到有界的 ``_emergency_records`` 环形缓冲（固定大小，
  不随失败次数无限增长）并计入 ``emergency_drop_count``，调用方可通过
  ``emergency_records``/``emergency_drop_count`` 属性事后发现；
- dropped_count 按 kind 统计，可查询；
- flush(timeout=...)/close(timeout=...) 使用 barrier/ack 语义：每个成功
  入队的条目都会分配一个单调递增的序列号，写线程在真正调用
  ``inner.append()``（无论成功还是被捕获异常）之后才推进"已确认序列号"。
  ``flush()`` 只有在写线程确认已经处理完调用时刻之前入队的全部条目后才
  返回 True——不再仅凭 ``queue.empty()`` 判断（旧实现的真实 bug：
  ``queue.get()`` 一取出条目就让队列变空，但此时 ``inner.append()`` 还
  没执行，``flush()`` 会在数据尚未真正落盘时就误报"已排空"）；
- 写线程内部异常不传播给调用方（不能因为磁盘满/权限错误杀死对局线程），
  但必须记录 write_error_count 并在无法恢复时写 stderr；
- 多对局线程并发安全（多个线程调用同一个 AsyncDecisionLog 实例的方法）。
"""
import collections
import hashlib
import queue
import sys
import threading
import time

from .sanitize import sanitize_text as _sanitize_text

# 关键事件 kind：队列满时永不静默丢弃。注意 "decision" 对应
# DecisionLog.action() 方法落盘时使用的 kind 值（不是字面的 "action"）。
CRITICAL_KINDS = frozenset({
    "decision", "action_rejected", "fallback_sent", "hu_detail", "error", "result",
    "round_end", "decision_attempt", "decision_attempt_outcome",
    # B3 生产接入：anomaly_window 是异常事件的完整前后窗口记录，必须与其它
    # 关键事件一样永不静默丢弃（丢弃即丢失"异常发生时上下文是什么"这一
    # 不可重建的信息）。
    "anomaly_window",
})
# 高频低信息量 kind：队列满时优先丢弃。state_dedup_summary/state_sample 属于
# B3 状态采样/去重的产出，本质上是"正常状态"的低频摘要，可以在队列持续
# 满载时被丢弃（不影响异常可观测性，只影响正常态的采样密度）。
DROPPABLE_KINDS = frozenset({
    "state", "notify", "piao_attempt", "pass_window",
    "state_dedup_summary", "state_sample", "state_request_metric",
})

# 紧急记录环形缓冲的固定容量：关键事件入队失败时，最多保留这么多条最近的
# 紧急记录供事后排查（不含原始 payload，避免占用无界内存/意外携带敏感
# 字段——只保留 kind/decision_id/时间戳等定位信息）。
DEFAULT_EMERGENCY_CAPACITY = 500


class AsyncDecisionLog:
    """包装一个具备 ``append(kind, payload)`` 接口的同步日志对象（通常是
    ``mj.logging.DecisionLog``），把落盘操作转移到单个后台写线程，调用方
    线程只做 ``queue.put_nowait`` 级别的非阻塞入队。

    对外方法签名与 ``DecisionLog`` 完全一致（``action``/``piao_attempt``/
    ``action_rejected``/``fallback_sent``/``notify``/``state``/`round_end``/
    ``result``/``error``/``append``），可以直接替换 ``mj.bot.py`` 里创建的
    ``DecisionLog()`` 实例，不需要改动调用点的参数传递方式。
    """

    def __init__(self, inner, *, maxsize=10000, emergency_capacity=DEFAULT_EMERGENCY_CAPACITY):
        self._inner = inner
        self._queue: "queue.Queue" = queue.Queue(maxsize=maxsize)
        self._dropped_count = collections.Counter()
        self._write_error_count = 0
        self._emergency_drop_count = 0
        self._emergency_records = collections.deque(maxlen=emergency_capacity)
        self._lock = threading.Lock()
        # barrier/ack 序列号：_enqueued_seq 是最近一次成功入队时分配的序号，
        # _acked_seq 是写线程真正完成 inner.append()（或捕获其异常）之后
        # 推进到的序号。flush() 只比较这两个数字，不看队列是否"看起来空"。
        self._seq_lock = threading.Lock()
        self._enqueued_seq = 0
        self._acked_seq = 0
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="AsyncDecisionLogWriter")
        self._thread.start()

    # ------------------------------------------------------------------
    # 入队：严格非阻塞，队列满时按优先级丢弃/记紧急记录。
    # ------------------------------------------------------------------
    def _enqueue(self, kind, payload):
        """P0 返修：本方法（含队列满分支）绝不执行 print/文件写入或任何可能
        阻塞的 I/O——独立评审复现过慢终端/阻塞管道下单次 stderr 写入达到
        105.83ms，会直接拖慢调用线程（动作关键路径）的 enqueue 耗时。

        队列满时只做纯内存操作：更新计数 + 写入固定容量的
        ``_emergency_records`` 环形缓冲。告警改为在 ``close()``/显式
        summary 调用时一次性输出（见 ``_print_drop_summary_if_any``），
        不再每个丢弃事件打印一行。
        """
        full = False
        with self._seq_lock:
            seq = self._enqueued_seq + 1
            try:
                self._queue.put_nowait((kind, payload, seq))
            except queue.Full:
                full = True
            else:
                self._enqueued_seq = seq
        if not full:
            return
        if kind in CRITICAL_KINDS:
            # 关键事件：绝不阻塞重试（P0-2 明确禁止）。入队失败时立即写入
            # 固定大小的紧急记录环形缓冲 + 计数，保证"可观测"而不是消失。
            # 核算口径：emergency_records 与 emergency_drop_count 描述同一批
            # 事件（len(emergency_records) == min(emergency_drop_count,
            # emergency_capacity)），written + emergency_drop_count ==
            # submitted，两者不得重复相加。
            record = {
                "kind": kind,
                "time": time.time(),
                "decision_id": payload.get("decision_id") if isinstance(payload, dict) else None,
                "game_id": payload.get("game_id") if isinstance(payload, dict) else None,
            }
            with self._lock:
                self._emergency_drop_count += 1
                self._dropped_count[kind] += 1
                self._emergency_records.append(record)
            return
        # 可丢弃的高频 kind：直接丢弃计数，不阻塞调用线程。
        with self._lock:
            self._dropped_count[kind] += 1

    def append(self, kind, payload):
        self._enqueue(kind, payload)

    def action(self, game_id, snapshot, decision, client_prepare_ms=None, action_request_ms=None,
              opp_chain=False, rules=None, decision_id=None, mc=None):
        """action 走关键路径：本方法必须先确定 decision_id（可能是调用方
        显式传入的，B1 要求由调用方在 api.action() 之前生成），再构造完整
        payload 后入队——不做真正的哈希/序列化工作转移到写线程（哈希计算
        本身是纯 CPU 操作，量级是毫秒级以内，转移到写线程只会让"决策是否
        已经被记录"这件事变得不确定；保持在调用线程同步完成哈希计算，只
        把最终的"落盘 I/O"转移到写线程）。"""
        from .observability import (
            SCHEMA_VERSION, POLICY_VERSION, build_hash as _build_hash,
            config_hash as _config_hash, new_decision_id, state_hash as _state_hash,
        )
        from .state import normalize as _normalize_state

        decision_id = decision_id or new_decision_id()
        payload = {
            "schema_version": SCHEMA_VERSION,
            "policy_version": POLICY_VERSION,
            "decision_id": decision_id,
            "game_id": game_id,
            "seat": snapshot.get("seat"),
            "round_no": snapshot.get("round_no"),
            "dealer": snapshot.get("dealer"),
            "phase": snapshot.get("phase"),
            "opp_chain": bool(opp_chain),
            "wall_remaining": snapshot.get("wall_remaining"),
            "scores": snapshot.get("scores"),
            "drawn_tile": snapshot.get("drawn_tile"),
            "seq": snapshot.get("seq"),
            "turn": snapshot.get("turn"),
            "responding_seats": snapshot.get("responding_seats"),
            "hand": snapshot.get("my_hand"),
            "melds": snapshot.get("melds"),
            "rules": snapshot.get("rules") or {},
            "piao": snapshot.get("piao", 0),
            "god": snapshot.get("god"),
            "decision": decision,
            "config_hash": _config_hash(rules if rules is not None else snapshot.get("rules")),
            "build_hash": _build_hash(),
        }
        state = _normalize_state(snapshot, rules=rules)
        if state is not None:
            payload["state_hash"] = _state_hash(state)
        if mc:
            payload["mc"] = mc   # 实时 MC 摘要（只在 MC 实际参与这次决策时才有）
        if client_prepare_ms is not None:
            payload["client_prepare_ms"] = round(client_prepare_ms, 3)
        if action_request_ms is not None:
            payload["action_request_ms"] = round(action_request_ms, 3)
        # kind 必须是 "decision"，与同步 DecisionLog.action() 落盘的 kind
        # 保持一致（mj.data_import 按 kind=="decision" 识别决策记录）——
        # 历史 bug：早期版本误用 kind="action"，导致异步日志落盘的文件
        # 表面上"看起来能导入"，但 decisions 表实际一条都查不到。
        self._enqueue("decision", payload)
        return decision_id

    def decision_attempt(self, game_id, *, decision_id, round_no=None, seat=None, state_hash=None,
                          action=None, tile=None, policy_version=None, schema_version=None,
                          config_hash=None, build_hash=None, attempted_at=None):
        """P0-1：与同步 ``DecisionLog.decision_attempt`` 语义一致，走关键
        路径 enqueue（kind='decision_attempt' 在 CRITICAL_KINDS 内）。"""
        from .observability import (
            SCHEMA_VERSION, POLICY_VERSION, build_hash as _build_hash,
        )
        self._enqueue("decision_attempt", {
            "decision_id": decision_id,
            "game_id": game_id,
            "round_no": round_no,
            "seat": seat,
            "state_hash": state_hash,
            "action": action,
            "tile": tile,
            "policy_version": policy_version if policy_version is not None else POLICY_VERSION,
            "schema_version": schema_version if schema_version is not None else SCHEMA_VERSION,
            "config_hash": config_hash,
            "build_hash": build_hash if build_hash is not None else _build_hash(),
            "outcome": "pending",
        })

    def decision_attempt_outcome(self, *, decision_id, outcome, outcome_detail=None,
                                  resolved_at=None, game_id=None):
        """P0-1：与对应 decision_attempt 使用相同的 decision_id 记录最终
        结果，走关键路径 enqueue（kind='decision_attempt_outcome' 在
        CRITICAL_KINDS 内）。P1 修复：``game_id`` 为可选补充字段，语义与
        ``DecisionLog.decision_attempt_outcome`` 一致。"""
        self._enqueue("decision_attempt_outcome", {
            "decision_id": decision_id,
            "game_id": game_id,
            "outcome": outcome,
            "outcome_detail": outcome_detail,
        })

    def piao_attempt(self, game_id, snapshot, decision_id=None):
        self._enqueue("piao_attempt", {
            "decision_id": decision_id,
            "game_id": game_id,
            "seat": snapshot.get("seat"),
            "round_no": snapshot.get("round_no"),
            "dealer": snapshot.get("dealer"),
            "phase": snapshot.get("phase"),
            "hand": snapshot.get("my_hand"),
            "melds": snapshot.get("melds"),
            "piao": snapshot.get("piao", 0),
            "god": snapshot.get("god"),
            "chain_count": snapshot.get("chain_count", 0),
        })

    def action_rejected(self, game_id, phase, decision, error, count, snapshot, decision_id=None):
        self._enqueue("action_rejected", {
            "decision_id": decision_id,
            "game_id": game_id,
            "seat": snapshot.get("seat"),
            "round_no": snapshot.get("round_no"),
            "phase": phase,
            "decision": decision,
            "rejections": count,
            "error": str(error),
            "wall_remaining": snapshot.get("wall_remaining"),
            "hand": snapshot.get("my_hand"),
            "drawn_tile": snapshot.get("drawn_tile"),
            "melds": snapshot.get("melds"),
        })

    def fallback_sent(self, game_id, snapshot, tile, accepted, error=None, decision_id=None):
        self._enqueue("fallback_sent", {
            "decision_id": decision_id,
            "game_id": game_id,
            "seat": snapshot.get("seat"),
            "round_no": snapshot.get("round_no"),
            "tile": tile,
            "accepted": bool(accepted),
            "error": None if error is None else str(error),
            "hand": snapshot.get("my_hand"),
            "drawn_tile": snapshot.get("drawn_tile"),
        })

    def notify(self, game_id, payload):
        self._enqueue("notify", {
            "game_id": game_id,
            "seq": payload.get("seq"),
            "closed": bool(payload.get("closed")),
        })

    def state(self, game_id, requested_seq, response, elapsed_ms):
        self._enqueue("state", {
            "game_id": game_id,
            "requested_seq": requested_seq,
            "response_seq": response.get("seq"),
            "pending": bool(response.get("pending")),
            "gap": bool(response.get("gap")),
            "event_count": len(response.get("events") or []),
            "state_request_ms": round(elapsed_ms, 3),
        })

    def state_request_metric(self, *, game_id, requested_seq, returned_seq, trigger,
                             elapsed_ms, pending=False, gap=False, state_hash=None):
        self._enqueue("state_request_metric", {
            "game_id": game_id,
            "requested_seq": requested_seq,
            "returned_seq": returned_seq,
            "trigger": trigger,
            "elapsed_ms": round(elapsed_ms, 3),
            "pending": bool(pending),
            "gap": bool(gap),
            "state_hash": state_hash,
        })

    def round_end(self, game_id, result):
        self._enqueue("round_end", {"game_id": game_id, "result": result})

    def result(self, game_id, state):
        self._enqueue("result", {"game_id": game_id, "state": state})

    def error(self, game_id, error):
        self._enqueue("error", {"game_id": game_id, "error": str(error)})

    # ------------------------------------------------------------------
    # 写线程：批量消费队列，调用底层 inner.append()。
    # ------------------------------------------------------------------
    def _run(self):
        while not self._stop_event.is_set() or not self._queue.empty():
            try:
                kind, payload, seq = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._inner.append(kind, payload)
            except Exception as exc:  # noqa: BLE001 - 写线程异常绝不传播给调用线程
                with self._lock:
                    self._write_error_count += 1
                print(f"[AsyncDecisionLog] write error (kind={kind!r}): "
                      f"{exc.__class__.__name__}: {_sanitize_text(str(exc), limit=200)}",
                      file=sys.stderr)
            finally:
                self._queue.task_done()
                # barrier/ack：只有 inner.append() 真正返回（无论成功还是
                # 抛出异常被捕获）之后，才把这条记录标记为"已确认"。
                # flush() 依据这个序号判断"排空"，不再仅凭队列是否为空
                # （队列在 get() 那一刻就已经变空，但此时落盘还没发生）。
                with self._seq_lock:
                    if seq > self._acked_seq:
                        self._acked_seq = seq

    # ------------------------------------------------------------------
    # 排空/关闭：barrier/ack 语义，只有底层 append 真正完成才算排空。
    # ------------------------------------------------------------------
    def flush(self, timeout=5.0):
        """阻塞直到调用时刻之前入队的全部条目都已被写线程真正调用过
        ``inner.append()``（成功或捕获异常，都算"已处理"），或超时。
        返回 True 表示确认排空（此时之前入队的记录要么已经真正落盘，
        要么其写入异常已经被计入 write_error_count——不存在"返回 True
        但记录其实还没落盘"的情况）；False 表示超时（调用方可以据此决定
        是否需要重试/报警，而不是无限等待）。"""
        with self._seq_lock:
            target = self._enqueued_seq
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._seq_lock:
                if self._acked_seq >= target:
                    return True
            time.sleep(0.005)
        with self._seq_lock:
            return self._acked_seq >= target

    def close(self, timeout=5.0):
        """通知写线程停止，尽量排空队列后再退出。正常关闭路径下应该等到
        全部已入队条目都被写线程确认（ack）；如果 timeout 内未能确认完
        （如写盘卡死），返回 False，调用方应当接受"异常退出容许尾部损失"
        （任务书明确允许），但仍会把 dropped/error 计数暴露出来供事后
        报告。"""
        drained = self.flush(timeout=timeout)
        self._stop_event.set()
        self._thread.join(timeout=max(0.5, timeout - (0 if drained else 0)))
        self._print_drop_summary_if_any()
        return drained

    def _print_drop_summary_if_any(self):
        """P0 返修：告警不再逐事件打印（会在调用线程/队列满时执行阻塞式
        stderr I/O）。改为在 close() 这个显式、非动作关键路径的收尾点，
        一次性汇总输出 emergency_drop_count/dropped_count，供事后排查。"""
        emergency = self.emergency_drop_count
        dropped = self.dropped_count
        if not emergency and not dropped:
            return
        print(f"[AsyncDecisionLog] drop summary: emergency_drop_count={emergency} "
              f"dropped_count={dropped}", file=sys.stderr)

    # ------------------------------------------------------------------
    # 可观测的计数器（供 summary/CLI/压力测试使用）。
    # ------------------------------------------------------------------
    @property
    def dropped_count(self):
        with self._lock:
            return dict(self._dropped_count)

    @property
    def write_error_count(self):
        with self._lock:
            return self._write_error_count

    @property
    def emergency_drop_count(self):
        with self._lock:
            return self._emergency_drop_count

    @property
    def emergency_records(self):
        """固定大小环形缓冲里的紧急记录快照（最近 ``emergency_capacity``
        条），供事后排查"哪些关键事件因为队列满而未能真正入队"。"""
        with self._lock:
            return list(self._emergency_records)

    def qsize(self):
        return self._queue.qsize()


class ConsecutiveStateDeduper:
    """B3：连续相同 ``state_hash`` 只记首次、末次、重复次数，不逐条重复
    落盘。调用方每次拿到一个新的 state_hash 时调用 ``observe()``：
    - 与上一次不同 → 返回上一段的汇总（如果有），并开始新的一段；
    - 与上一次相同 → 更新末次时间/计数，不返回汇总（调用方不落盘这一条）。
    在流结束时调用 ``flush()`` 拿到最后一段的汇总。
    """

    def __init__(self):
        self._current_hash = None
        self._first_seen = None
        self._last_seen = None
        self._repeat_count = 0

    def observe(self, state_hash, timestamp):
        if state_hash == self._current_hash:
            self._last_seen = timestamp
            self._repeat_count += 1
            return None
        summary = self._pop_summary()
        self._current_hash = state_hash
        self._first_seen = timestamp
        self._last_seen = timestamp
        self._repeat_count = 1
        return summary

    def flush(self):
        return self._pop_summary()

    def _pop_summary(self):
        if self._current_hash is None:
            return None
        summary = {
            "state_hash": self._current_hash,
            "first_seen": self._first_seen,
            "last_seen": self._last_seen,
            "repeat_count": self._repeat_count,
        }
        self._current_hash = None
        return summary


class AnomalyRingBuffer:
    """B3：内存环形缓冲，保留异常前后窗口。``record()`` 每条事件都调用，
    正常事件只是被存进环形缓冲（旧的自动被挤出）；当遇到异常事件
    （调用方通过 ``mark_anomaly()`` 显式标记）时，把"环形缓冲里已有的
    before 部分 + 之后继续追加的 after 部分"整合成一个窗口，通过
    ``drain_windows()`` 取出。

    P0 返修：``_pending_windows``（正在收集 after 部分、尚未完成的窗口）
    之前是无上限 list——连续触发异常且之后没有 ``record()`` 事件喂入时
    （after 部分永远收集不满），pending 数量会随异常次数无限增长（例如
    连续 10000 次 ``mark_anomaly()`` 且没有 record，会留下 10000 个未完成
    窗口常驻内存）。现在用 ``max_pending`` 固定上限 + 确定性淘汰策略：
    超限时丢弃**最旧**的 pending 窗口，保留最新的（最旧的窗口本身已经
    等待最久，继续等待收集 after 的价值最低，且淘汰旧窗口是确定性的，
    不依赖随机采样）；淘汰次数计入 ``pending_overflow_count``，供上游
    在异常窗口输出旁附带这一计数，暴露"发生过多少次未完成窗口被挤出"。
    淘汰本身只是纯内存 list 操作，不执行任何同步 I/O。

    ``_completed_windows``（已经收集满 after、待 ``drain_windows()`` 取走
    的窗口）语义上是"待消费队列"，其有界性由调用方保证：``_GameObserver``
    在每次 ``record()``/``mark_anomaly()`` 之后都立即调用
    ``_drain_anomaly_windows()`` 把已完成窗口取走并落盘（见 mj/bot.py），
    因此本类内部不会持续累积已完成窗口——不需要额外的硬上限。
    """

    DEFAULT_MAX_PENDING = 64

    def __init__(self, before=30, after=30, max_pending=DEFAULT_MAX_PENDING):
        if max_pending <= 0:
            # 卫生修复：max_pending<=0 会让 mark_anomaly() 里的
            # "self._max_pending > 0 and ..." 判断恒为 False，淘汰逻辑
            # 整体失效，_pending_windows 重新退化为无上限 list——必须在
            # 构造期直接拒绝这种配置错误，而不是静默产生无界增长。
            raise ValueError(f"max_pending must be > 0, got {max_pending!r}")
        self._before = before
        self._after = after
        self._max_pending = max_pending
        self._buffer = collections.deque(maxlen=before)
        self._pending_windows = []  # 正在收集 after 部分的窗口，长度 <= max_pending
        self._completed_windows = []
        self._pending_overflow_count = 0

    def record(self, event):
        self._buffer.append(event)
        still_pending = []
        for window in self._pending_windows:
            window["after"].append(event)
            if len(window["after"]) >= self._after:
                self._completed_windows.append(window)
            else:
                still_pending.append(window)
        self._pending_windows = still_pending

    def mark_anomaly(self, anomaly_event):
        window = {
            "anomaly": anomaly_event,
            "before": list(self._buffer),
            "after": [],
        }
        if self._after <= 0:
            self._completed_windows.append(window)
            return
        if self._max_pending > 0 and len(self._pending_windows) >= self._max_pending:
            # 固定上限 + 确定性淘汰：丢弃最旧的 pending 窗口，保留最新的。
            self._pending_windows.pop(0)
            self._pending_overflow_count += 1
        self._pending_windows.append(window)

    @property
    def pending_overflow_count(self):
        return self._pending_overflow_count

    @property
    def pending_count(self):
        return len(self._pending_windows)

    def drain_windows(self):
        """取出所有已经收集完 after 部分的窗口（清空内部已完成列表）。
        仍在等待更多 after 事件的窗口不会被取出。"""
        out = self._completed_windows
        self._completed_windows = []
        return out

    def finalize_pending(self):
        """流结束时调用：把所有仍在等待 after 事件的窗口也算作"完成"
        （after 部分可能不足 ``after`` 条，这是流提前结束的正常情况）。"""
        out = self._pending_windows + self._completed_windows
        self._pending_windows = []
        self._completed_windows = []
        return out
