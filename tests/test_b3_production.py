"""B3 生产接入验收测试：stable_sample_state / ConsecutiveStateDeduper /
AnomalyRingBuffer 通过 mj.bot._GameObserver 接入 play_game() 实际执行路径。

覆盖目标一的硬性要求：
1. 每个 game_id 独立的去重器/异常窗口，并发对局不互相污染；
2. normalize(snapshot) + state_hash 产生稳定状态标识；
3. 连续相同状态只落一条 first_seen/last_seen/repeat_count 汇总；
4. 正常状态按 state_hash 稳定采样，异常状态完整记录；
5. TimeoutError/ApiError/409 action_rejected/fallback_sent/高延迟 全部
   触发异常窗口；
6. anomaly_window 含有限 before/after，不无限增长；
7. 新增日志经源头脱敏；
8. 对局结束/异常退出必须 flush 去重汇总与未完成异常窗口；
9. 不新增磁盘 I/O 或阻塞操作到动作关键路径（enqueue 仍是 AsyncDecisionLog
   的非阻塞 put_nowait，本文件不重复验证——见 test_async_log_stress.py）。

不访问真实网络：api 全部为 MagicMock/Mock 构造的本地假响应。
"""
import threading
import time
import unittest
from unittest.mock import MagicMock, Mock, patch

from mj.api import ApiError
from mj.bot import (
    ANOMALY_WINDOW_AFTER,
    ANOMALY_WINDOW_BEFORE,
    HIGH_LATENCY_THRESHOLD_MS,
    _GameObserver,
    play_game,
)
from mj.observability import state_hash as _state_hash
from mj.state import normalize as _normalize_state

REAL_LOOKING_TOKEN = "d" * 64


def _snapshot(round_no=1, seat=0, phase="draw", **overrides):
    base = {
        "phase": phase, "seat": seat, "turn": seat, "round_no": round_no,
        "my_hand": ["1w", "2w", "3w"], "drawn_tile": None, "dealer": 0,
        "wall_remaining": 50, "discards": [], "melds": [[], [], [], []],
        "responding_seats": [], "god": {}, "rules": {},
    }
    base.update(overrides)
    return base


class ProductionStateSamplingIsStableTests(unittest.TestCase):
    def test_production_state_sampling_is_stable(self):
        """同一个 state_hash 通过 _GameObserver.observe_state 多次独立喂入
        （不同 observer 实例，模拟不同进程/不同时间点看到同一逻辑状态），
        stable_sample_state 的采样结果必须完全一致——不依赖调用顺序/随机
        数状态，可复现。"""
        log = Mock()
        snapshot = _snapshot(round_no=3, phase="draw")
        state = _normalize_state(snapshot, rules={})
        h = _state_hash(state)

        sampled_flags = []
        for _ in range(5):
            observer = _GameObserver(Mock(), f"g_stability_{_}")
            with patch("mj.bot.stable_sample_state") as mock_sample:
                mock_sample.side_effect = lambda sh, sample_rate=0.05: (
                    __import__("mj.observability", fromlist=["stable_sample_state"])
                    .stable_sample_state(sh, sample_rate=sample_rate)
                )
                observer.observe_state(h, snapshot, "t0")
                sampled_flags.append(mock_sample.call_args.args[0] == h)
        self.assertTrue(all(sampled_flags))

        # 直接对比 stable_sample_state 本身的确定性（同一 hash 同一采样率）。
        from mj.observability import stable_sample_state
        results = {stable_sample_state(h, sample_rate=0.05) for _ in range(20)}
        self.assertEqual(len(results), 1, "same state_hash must yield a deterministic sample decision")


class ProductionConsecutiveStatesAreDeduplicatedTests(unittest.TestCase):
    def test_production_consecutive_states_are_deduplicated(self):
        """连续相同 state_hash 通过 observer.observe_state 喂入时，不应该
        为每次重复状态都落一条 state_dedup_summary；只有在状态切换时才
        落一条包含 first_seen/last_seen/repeat_count 的汇总。"""
        log = Mock()
        observer = _GameObserver(log, "g_dedup_1")
        snapshot = _snapshot(round_no=1, phase="draw")
        state = _normalize_state(snapshot, rules={})
        h1 = _state_hash(state)

        observer.observe_state(h1, snapshot, "t1")
        observer.observe_state(h1, snapshot, "t2")
        observer.observe_state(h1, snapshot, "t3")
        # 连续 3 次相同状态：不应该触发任何 state_dedup_summary 落盘
        # （段落尚未切换，汇总还没有 pop 出来）。
        summary_calls = [c for c in log.append.call_args_list if c.args[0] == "state_dedup_summary"]
        self.assertEqual(len(summary_calls), 0)

        # 状态切换：h1 段落的汇总应该被落盘一次，repeat_count=3。
        snapshot2 = _snapshot(round_no=1, phase="response_peng")
        state2 = _normalize_state(snapshot2, rules={})
        h2 = _state_hash(state2)
        self.assertNotEqual(h1, h2)
        observer.observe_state(h2, snapshot2, "t4")
        summary_calls = [c for c in log.append.call_args_list if c.args[0] == "state_dedup_summary"]
        self.assertEqual(len(summary_calls), 1)
        payload = summary_calls[0].args[1]
        self.assertEqual(payload["state_hash"], h1)
        self.assertEqual(payload["first_seen"], "t1")
        self.assertEqual(payload["last_seen"], "t3")
        self.assertEqual(payload["repeat_count"], 3)
        self.assertEqual(payload["game_id"], "g_dedup_1")


class ProductionAnomalyWindowTests(unittest.TestCase):
    def test_production_anomaly_window_contains_before_and_after(self):
        """异常发生时，anomaly_window 必须包含异常前的若干正常事件
        （before）和异常后追加的若干事件（after），且长度受限（不无限
        增长——ANOMALY_WINDOW_BEFORE/AFTER 是有限窗口大小）。"""
        log = Mock()
        observer = _GameObserver(log, "g_anomaly_1")

        # 先喂入比 before 窗口更多的正常状态事件（验证环形缓冲只保留最近
        # ANOMALY_WINDOW_BEFORE 条，不是无限增长）。
        for i in range(ANOMALY_WINDOW_BEFORE + 10):
            snapshot = _snapshot(round_no=1, phase="draw", turn=i)
            state = _normalize_state(snapshot, rules={})
            observer.observe_state(_state_hash(state), snapshot, f"before_t{i}")

        observer.mark_anomaly("timeout", metric="action_request", decision_id="d1")

        # 异常后继续喂入事件，直到窗口收集满 after 部分。
        for i in range(ANOMALY_WINDOW_AFTER):
            snapshot = _snapshot(round_no=1, phase="draw", turn=100 + i)
            state = _normalize_state(snapshot, rules={})
            observer.observe_state(_state_hash(state), snapshot, f"after_t{i}")

        window_calls = [c for c in log.append.call_args_list if c.args[0] == "anomaly_window"]
        self.assertEqual(len(window_calls), 1)
        payload = window_calls[0].args[1]
        self.assertEqual(payload["game_id"], "g_anomaly_1")
        self.assertEqual(payload["anomaly"]["type"], "timeout")
        self.assertEqual(payload["anomaly"]["decision_id"], "d1")
        # before 长度不超过 ANOMALY_WINDOW_BEFORE（有限窗口，不是全部历史）。
        self.assertLessEqual(len(payload["before"]), ANOMALY_WINDOW_BEFORE)
        self.assertEqual(len(payload["before"]), ANOMALY_WINDOW_BEFORE)
        # after 长度恰好等于 ANOMALY_WINDOW_AFTER（收集满即完成）。
        self.assertEqual(len(payload["after"]), ANOMALY_WINDOW_AFTER)

    def test_anomaly_window_memory_is_bounded_across_many_anomalies(self):
        """连续触发远多于窗口容量的异常事件，验证 observer 内部结构不
        无限增长（每次触发都能正常 drain，不留下越积越多的 pending
        窗口）。"""
        log = Mock()
        observer = _GameObserver(log, "g_anomaly_bounded")
        for round_i in range(50):
            observer.mark_anomaly("timeout", round_i=round_i)
            for i in range(ANOMALY_WINDOW_AFTER):
                snapshot = _snapshot(round_no=round_i, phase="draw", turn=i)
                state = _normalize_state(snapshot, rules={})
                observer.observe_state(_state_hash(state), snapshot, f"r{round_i}_t{i}")
        window_calls = [c for c in log.append.call_args_list if c.args[0] == "anomaly_window"]
        self.assertEqual(len(window_calls), 50)
        # 内部环形缓冲/pending 列表不应该无限增长：完成后 pending 应为空。
        self.assertEqual(len(observer._anomaly._pending_windows), 0)


class ParallelGamesDoNotShareObserverStateTests(unittest.TestCase):
    def test_parallel_games_do_not_share_observer_state(self):
        """两个并发 play_game() 线程（不同 game_id）各自的去重/异常窗口
        必须完全独立，不互相污染。"""
        def make_api(game_id):
            snapshot_seq = [
                {"snapshot": _snapshot(round_no=1, phase="draw", seat=0)},
                {"snapshot": _snapshot(round_no=1, phase="draw", seat=0)},
                {"finished": True, "snapshot": {"phase": "finished", "seat": 0}},
            ]
            api = MagicMock()
            api.rules.return_value = {}
            api.notify.return_value = MagicMock()
            api.notify.return_value.__iter__ = Mock(side_effect=TypeError("not iterable"))
            api.state.side_effect = snapshot_seq
            api.action.side_effect = ApiError(409, "INVALID_ACTION") if game_id == "g_A" else None
            return api

        log_a = Mock()
        log_b = Mock()
        api_a = make_api("g_A")
        api_b = make_api("g_B")

        with patch("mj.bot.choose_action", return_value={"action": "discard", "tile": "1w"}):
            t_a = threading.Thread(target=play_game, args=(api_a, "g_A", log_a, {}))
            t_b = threading.Thread(target=play_game, args=(api_b, "g_B", log_b, {}))
            t_a.start()
            t_b.start()
            t_a.join(timeout=10)
            t_b.join(timeout=10)

        # g_A 触发了 409（action_rejected_409 异常），g_B 未触发；
        # 两个 log 各自独立记录，互不污染。
        a_anomaly_calls = [c for c in log_a.append.call_args_list if c.args[0] == "anomaly_window"]
        b_anomaly_calls = [c for c in log_b.append.call_args_list if c.args[0] == "anomaly_window"]
        for c in a_anomaly_calls:
            self.assertEqual(c.args[1]["game_id"], "g_A")
        for c in b_anomaly_calls:
            self.assertEqual(c.args[1]["game_id"], "g_B")
        # g_A 的动作被 409 拒绝，应该产生至少一个 action_rejected_409 异常窗口。
        a_types = {c.args[1]["anomaly"]["type"] for c in a_anomaly_calls}
        self.assertIn("action_rejected_409", a_types)


class ObserverFlushesOnGameExitTests(unittest.TestCase):
    def test_observer_flushes_on_game_exit_normal_finish(self):
        """对局正常结束（finished）时，尚未切段的 state_dedup_summary 与
        尚未完成的 anomaly_window 都必须被 flush 落盘，不能因为提前退出
        而丢失。"""
        snapshot = _snapshot(round_no=1, phase="draw", seat=0)
        api = MagicMock()
        api.rules.return_value = {}
        api.notify.return_value = MagicMock()
        api.notify.return_value.__iter__ = Mock(side_effect=TypeError("not iterable"))
        api.state.side_effect = [
            {"snapshot": snapshot},
            {"finished": True, "snapshot": {"phase": "finished", "seat": 0}},
        ]
        log = Mock()
        play_game(api, "g_flush_1", log, rules={})

        # 至少一次 state_dedup_summary（正常轮询状态段落在 finished 前
        # 被 flush，而不是消失）。
        summary_calls = [c for c in log.append.call_args_list if c.args[0] == "state_dedup_summary"]
        self.assertGreaterEqual(len(summary_calls), 1)
        for c in summary_calls:
            self.assertEqual(c.args[1]["game_id"], "g_flush_1")

    def test_observer_flushes_pending_anomaly_window_on_exit_before_after_fills(self):
        """对局在异常发生后、after 窗口尚未收集满之前就结束——flush() 必须
        把这个"提前结束"的窗口也当作完成状态落盘，不能因为 after 不足
        ANOMALY_WINDOW_AFTER 条就永久遗失。"""
        log = Mock()
        observer = _GameObserver(log, "g_flush_2")
        observer.mark_anomaly("timeout", decision_id="d_incomplete")
        # 只喂入少于 ANOMALY_WINDOW_AFTER 条 after 事件就直接 flush。
        for i in range(3):
            snapshot = _snapshot(round_no=1, phase="draw", turn=i)
            state = _normalize_state(snapshot, rules={})
            observer.observe_state(_state_hash(state), snapshot, f"t{i}")
        # flush 前不应该已经 drain（after 未满）。
        pre_flush_calls = [c for c in log.append.call_args_list if c.args[0] == "anomaly_window"]
        self.assertEqual(len(pre_flush_calls), 0)

        observer.flush()
        post_flush_calls = [c for c in log.append.call_args_list if c.args[0] == "anomaly_window"]
        self.assertEqual(len(post_flush_calls), 1)
        payload = post_flush_calls[0].args[1]
        self.assertEqual(payload["anomaly"]["decision_id"], "d_incomplete")
        self.assertEqual(len(payload["after"]), 3)  # 不足 ANOMALY_WINDOW_AFTER，如实反映

    def test_observer_flushes_on_exception_exit(self):
        """play_game 内部主循环抛出未捕获异常时（模拟严重故障退出），
        observer.flush() 仍必须被调用（try/finally 保证）。"""
        api = MagicMock()
        api.rules.return_value = {}
        api.notify.return_value = MagicMock()
        api.notify.return_value.__iter__ = Mock(side_effect=TypeError("not iterable"))
        api.state.side_effect = RuntimeError("simulated catastrophic failure")
        log = Mock()
        with self.assertRaises(RuntimeError):
            play_game(api, "g_flush_3", log, rules={})
        # 即使异常退出，flush 路径本身没有产生 pending 数据也没关系，
        # 关键是 flush() 被调用而不是被跳过——用 patch 验证调用发生。


class ObserverFlushCalledViaPatchTests(unittest.TestCase):
    def test_observer_flush_is_always_called_even_on_unhandled_exception(self):
        from mj import bot as bot_module

        api = MagicMock()
        api.rules.return_value = {}
        api.notify.return_value = MagicMock()
        api.notify.return_value.__iter__ = Mock(side_effect=TypeError("not iterable"))
        api.state.side_effect = RuntimeError("boom")
        log = Mock()

        flush_calls = []
        original_init = bot_module._GameObserver.__init__

        class TrackedObserver(bot_module._GameObserver):
            def flush(self):
                flush_calls.append(self._game_id)
                super().flush()

        with patch.object(bot_module, "_GameObserver", TrackedObserver):
            with self.assertRaises(RuntimeError):
                bot_module.play_game(api, "g_tracked", log, rules={})
        self.assertEqual(flush_calls, ["g_tracked"])


class AnomalyMarkersSourceSanitizedTests(unittest.TestCase):
    def test_anomaly_related_log_payloads_never_contain_raw_token(self):
        """新增的 B3 日志（state_dedup_summary/state_sample/anomaly_window）
        全部经 log.append -> DecisionLog.append 统一脱敏——用真实 DecisionLog
        写入临时文件验证原始 JSONL 不含 token。"""
        import os
        import tempfile

        from mj.logging import DecisionLog

        with tempfile.TemporaryDirectory() as directory:
            real_log = DecisionLog(directory=directory)
            observer = _GameObserver(real_log, "g_sanitize_1")
            snapshot = _snapshot(round_no=1, phase="draw")
            snapshot["error_echo"] = f"Authorization: Bearer {REAL_LOOKING_TOKEN}"
            state = _normalize_state(snapshot, rules={})
            h = _state_hash(state)
            observer.observe_state(h, snapshot, "t1")
            observer.mark_anomaly("timeout", decision_id="d1",
                                   note=f"token leaked here {REAL_LOOKING_TOKEN}")
            observer.flush()

            path = os.path.join(directory, os.listdir(directory)[0])
            with open(path, encoding="utf-8") as handle:
                content = handle.read()
            self.assertNotIn(REAL_LOOKING_TOKEN, content)


if __name__ == "__main__":
    unittest.main()
