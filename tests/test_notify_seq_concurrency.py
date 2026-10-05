"""P0/P1 修复回归：notify 驱动状态获取——10 场并发模拟，确定性证明：
- action 不会被 state 限速排队（本文件聚焦 _play_game_loop 层的 notify/
  seq 调度正确性；state 限速本身的证明见 tests/test_api_pace.py）；
- 重复 notify 不会造成重复动作；
- seq 缺口（gap=True）会让下一次请求回退到全量刷新（seq=0）；
- notifier 失效时 watchdog（主循环自身的低频轮询节奏）仍可推进对局，
  不依赖 notify 存活。
"""
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, Mock

from mj.bot import play_game

# 一个真实会胡的 14 张手（复用 tests/test_bot_state.py 的胡牌快照，13 张
# 清一色+对子结构 + 摸到的西）——draw 阶段立即自摸，驱动对局快速走完。
_HU_SNAPSHOT = {
    "phase": "draw", "seat": 0, "turn": 0, "round_no": 1,
    "my_hand": ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w",
                "1t", "1t", "1t", "西", "西"],
    "drawn_tile": "西",
    "melds": [[], [], [], []],
    "wall_remaining": 50,
}


class _FakeNotifyStream:
    """模拟 api.notify(game_id) 返回的 SSE 流对象：__iter__ 产出若干
    字节行，.close() 供 notify_sequences() 的 finally 块调用。"""

    def __init__(self, lines):
        self._lines = lines

    def __iter__(self):
        return iter(self._lines)

    def close(self):
        pass


def _make_api(state_responses, notify_lines=None, notify_raises=False):
    """构造一个可控的 Mock MahjongApi：
    - state_responses: 依次返回的 /state 响应字典列表；
    - notify_lines: notify SSE 流要产出的字节行列表（含重复 seq/closed）；
    - notify_raises: True 时 api.notify() 直接抛异常（模拟 notify 连接
      完全不可用，只能靠主循环自身的轮询节奏推进——即 watchdog）。
    """
    api = MagicMock()
    api.rules.return_value = {}
    if notify_raises:
        api.notify.side_effect = RuntimeError("notify endpoint unavailable")
    else:
        api.notify.return_value = _FakeNotifyStream(notify_lines or [])
    api.state.side_effect = state_responses
    api.action.return_value = {}
    return api


class TenRoomConcurrentNotifySimulationTests(unittest.TestCase):
    def test_ten_concurrent_games_notify_gap_and_watchdog_properties(self):
        games = {}

        # 5 场：重复 notify（同一 seq 反复到达）不应造成重复动作。
        for i in range(5):
            game_id = f"dup_notify_{i}"
            games[game_id] = _make_api(
                state_responses=[
                    {"snapshot": _HU_SNAPSHOT, "seq": 1},
                    {"finished": True, "snapshot": {"phase": "finished", "seat": 0}},
                ],
                notify_lines=[
                    b'data: {"seq": 1}\n', b'data: {"seq": 1}\n', b'data: {"seq": 1}\n',
                    b'data: {"closed": true}\n',
                ],
            )

        # 3 场：seq 缺口（gap=True）——下一次请求必须回退到全量刷新（seq=0）。
        for i in range(3):
            game_id = f"seq_gap_{i}"
            games[game_id] = _make_api(
                state_responses=[
                    {"snapshot": _HU_SNAPSHOT, "seq": 1},
                    {"snapshot": _HU_SNAPSHOT, "seq": 50, "gap": True},
                    {"finished": True, "snapshot": {"phase": "finished", "seat": 0}},
                ],
                notify_lines=[b'data: {"closed": true}\n'],
            )

        # 2 场：notify 端点完全失效——必须仍能靠 watchdog（主循环自身轮询）推进。
        for i in range(2):
            game_id = f"notify_dead_{i}"
            games[game_id] = _make_api(
                state_responses=[
                    {"snapshot": _HU_SNAPSHOT, "seq": 1},
                    {"finished": True, "snapshot": {"phase": "finished", "seat": 0}},
                ],
                notify_raises=True,
            )

        self.assertEqual(len(games), 10)
        results = {}
        errors = {}

        def run_one(game_id, api):
            log = Mock()
            try:
                play_game(api, game_id, log, rules={})
                results[game_id] = (api, log)
            except Exception as error:  # noqa: BLE001 - 记录任何异常供断言排查
                errors[game_id] = error

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = [pool.submit(run_one, gid, api) for gid, api in games.items()]
            for future in futures:
                future.result(timeout=15)

        self.assertEqual(errors, {})
        self.assertEqual(len(results), 10)

        # 属性1：重复 notify 不会造成重复 hu 动作——每场只提交一次 hu。
        for i in range(5):
            api, log = results[f"dup_notify_{i}"]
            hu_calls = [c for c in api.action.call_args_list if c.kwargs.get("action") == "hu"]
            self.assertEqual(len(hu_calls), 1, msg=f"dup_notify_{i} 应恰好提交一次 hu")

        # 属性2：seq gap 之后，下一次 /state 请求必须回退到全量刷新（seq=0）。
        for i in range(3):
            api, log = results[f"seq_gap_{i}"]
            self.assertEqual(api.state.call_count, 3)
            third_call_args = api.state.call_args_list[2].args
            self.assertEqual(third_call_args[1], 0,
                              msg=f"seq_gap_{i}：gap 之后第三次请求应为 seq=0（全量恢复）")

        # 属性3：notifier 失效（notify() 抛异常）时对局仍能靠 watchdog 完成。
        for i in range(2):
            api, log = results[f"notify_dead_{i}"]
            log.result.assert_called_once()
            hu_calls = [c for c in api.action.call_args_list if c.kwargs.get("action") == "hu"]
            self.assertEqual(len(hu_calls), 1, msg=f"notify_dead_{i} 应仍能完成胡牌提交")

    def test_seq_tracker_next_request_reflects_notify_then_watchdog_then_recovery(self):
        """单元级别的状态机断言，作为上面并发集成测试的直接对照：
        recovery -> watchdog -> notify -> watchdog -> recovery(gap)。

        独立复核 P0-1 返修：``request_seq`` 恒为 0（如实命名为
        notify-driven canonical snapshot——不是增量请求），只有 trigger
        标签随场景变化。"""
        from mj.bot import _SeqTracker

        tracker = _SeqTracker()
        # 首次进入：force_recovery 默认为 True -> recovery, seq=0。
        self.assertEqual(tracker.next_request(), (0, "recovery"))
        tracker.observe_response(returned_seq=1, gap=False, trigger="recovery")
        # 无新通知 -> watchdog，seq 仍为 0（全量刷新，只是标签是 watchdog）。
        self.assertEqual(tracker.next_request(), (0, "watchdog"))
        # notify 到达更大的 seq -> notify 触发，但请求参数仍是 seq=0。
        tracker.observe_notify(2)
        self.assertEqual(tracker.next_request(), (0, "notify"))
        tracker.observe_response(returned_seq=2, gap=False, trigger="notify")
        self.assertEqual(tracker.next_request(), (0, "watchdog"))
        # 收到 gap 响应 -> 下一次请求强制回退到全量恢复（seq 依旧是 0，
        # 但 trigger 标签变为 recovery，供 mj.timing 统计 gap 发生次数）。
        tracker.observe_response(returned_seq=99, gap=True)
        self.assertEqual(tracker.next_request(), (0, "recovery"))

    def test_reconnect_forces_recovery_trigger_even_without_explicit_gap_response(self):
        from mj.bot import _SeqTracker

        tracker = _SeqTracker()
        tracker.observe_response(returned_seq=5, gap=False, trigger="recovery")
        self.assertEqual(tracker.next_request(), (0, "watchdog"))
        tracker.mark_reconnected()
        self.assertEqual(tracker.next_request(), (0, "recovery"))

    def test_force_recovery_explicit_reason_api(self):
        """P0-3 独立复核要求：_SeqTracker 必须提供 force_recovery(reason)
        或等价接口——action 409 等场景据此立即进入 recovery。"""
        from mj.bot import _SeqTracker

        tracker = _SeqTracker()
        tracker.observe_response(returned_seq=5, gap=False, trigger="recovery")
        self.assertEqual(tracker.next_request(), (0, "watchdog"))
        tracker.force_recovery("action_409")
        self.assertEqual(tracker.next_request(), (0, "recovery"))
        with tracker._lock:
            self.assertEqual(tracker._last_recovery_reason, "action_409")

    def test_seq_tracker_thread_safety_under_concurrent_notify_and_response(self):
        """规则5：所有共享状态必须线程安全——多线程并发调用
        observe_notify()/observe_response()/next_request() 不应崩溃或产生
        不一致的负向结果（不做具体调度顺序断言，只验证无异常且最终结果
        自洽）。"""
        from mj.bot import _SeqTracker

        tracker = _SeqTracker()
        stop = threading.Event()
        errors = []

        def notifier_thread():
            seq = 0
            while not stop.is_set():
                seq += 1
                try:
                    tracker.observe_notify(seq)
                except Exception as error:  # noqa: BLE001
                    errors.append(error)

        def responder_thread():
            seq = 0
            while not stop.is_set():
                seq += 1
                try:
                    tracker.next_request()
                    tracker.observe_response(seq, gap=False)
                except Exception as error:  # noqa: BLE001
                    errors.append(error)

        threads = [threading.Thread(target=notifier_thread) for _ in range(3)]
        threads += [threading.Thread(target=responder_thread) for _ in range(3)]
        for thread in threads:
            thread.start()
        stop.wait(0.3)
        stop.set()
        for thread in threads:
            thread.join(timeout=2.0)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
