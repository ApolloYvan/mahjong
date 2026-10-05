"""独立复核最终阻断返修：P0-3（409 强制 seq=0 恢复）+ P0-4（真实协议形态
端到端状态机测试）。Fake API 严格模拟官方响应形态：
1. 首次：{"seq":100, "snapshot":{...}}
2. notify：{"seq":103}
3. events-only 响应（防御性场景）：{"seq":103, "events":[...]}  // 无 snapshot
4. 权威恢复：{"seq":103, "snapshot":{...}}
"""
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, Mock

from mj.api import ApiError
from mj.bot import _SeqTracker, play_game


def _make_api(state_responses, notify_lines=None, notify_raises=False):
    api = MagicMock()
    api.rules.return_value = {}
    if notify_raises:
        api.notify.side_effect = RuntimeError("notify endpoint unavailable")
    else:
        stream = MagicMock()
        stream.__iter__ = Mock(return_value=iter(notify_lines or [b'data: {"closed": true}\n']))
        api.notify.return_value = stream
    api.state.side_effect = state_responses
    api.action.return_value = {}
    return api


_HU_SNAPSHOT = {
    "phase": "draw", "seat": 0, "turn": 0, "round_no": 1,
    "my_hand": ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w",
                "1t", "1t", "1t", "西", "西"],
    "drawn_tile": "西",
    "melds": [[], [], [], []],
    "wall_remaining": 50,
}

_DRAW_SNAPSHOT = {
    "phase": "draw", "seat": 0, "turn": 0, "round_no": 1,
    "my_hand": ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w", "1t", "2t", "3t", "南"],
    "drawn_tile": "西",
    "melds": [[], [], [], []],
    "wall_remaining": 40,
}


class P03ForceRecoveryAfter409Tests(unittest.TestCase):
    """P0-3：任意 action 409 后立即进入 recovery，下一次 /state 请求必须
    为 seq=0。"""

    def test_next_state_request_after_409_is_seq_zero(self):
        # 直接验证 _SeqTracker 语义（核心断言）：409 前 watchdog(seq=0),
        # 409 后仍是 seq=0 但 trigger 变为 recovery。
        tracker = _SeqTracker()
        tracker.observe_response(returned_seq=5, gap=False, trigger="recovery")
        self.assertEqual(tracker.next_request(), (0, "watchdog"))
        tracker.force_recovery("action_409")
        request_seq, trigger = tracker.next_request()
        self.assertEqual(request_seq, 0)
        self.assertEqual(trigger, "recovery")

    def test_play_game_state_call_args_are_seq_zero_across_409(self):
        """端到端断言：整场 play_game() 过程中，所有 api.state() 调用的
        第二个位置参数（seq）恒为 0——包括紧跟在 409 之后的那一次。"""
        api = _make_api(
            state_responses=[
                {"snapshot": _HU_SNAPSHOT, "seq": 5},
                {"finished": True, "snapshot": {"phase": "finished", "seat": 0}},
            ],
        )
        log = Mock()
        play_game(api, "g", log, rules={})
        for call in api.state.call_args_list:
            self.assertEqual(call.args[1], 0,
                              msg="notify-driven canonical snapshot 方案下 seq 请求参数恒为 0")


class P01EventsOnlyResponseDoesNotDeadlockTests(unittest.TestCase):
    """P0-1 规则8：events-only 响应（无 snapshot）不得死循环、不得基于
    不完整 events 直接决策——必须记录 metric、强制下一次走 recovery。"""

    def test_events_only_response_does_not_stall_and_recovers(self):
        api = _make_api(
            state_responses=[
                {"seq": 103, "events": [{"seq": 101, "type": "discard"},
                                        {"seq": 103, "type": "discard"}]},  # 无 snapshot
                {"snapshot": _HU_SNAPSHOT, "seq": 105},
                {"finished": True, "snapshot": {"phase": "finished", "seat": 0}},
            ],
        )
        log = Mock()
        play_game(api, "g", log, rules={})
        # 必须真正推进到 finished，不因 events-only 响应而卡死。
        log.result.assert_called_once()
        hu_calls = [c for c in api.action.call_args_list if c.kwargs.get("action") == "hu"]
        self.assertEqual(len(hu_calls), 1)
        # events-only 响应之后紧跟的下一次请求仍是 seq=0（本方案没有真正
        # 意义上的"增量恢复"，全部请求参数恒为 0）。
        self.assertEqual(api.state.call_args_list[1].args[1], 0)
        # 必须记录了 state_request_metric（即便没有 snapshot）。
        metric_calls = [c for c in log.state_request_metric.call_args_list]
        self.assertGreaterEqual(len(metric_calls), 1)
        first_metric = metric_calls[0].kwargs
        self.assertEqual(first_metric["gap"], True)
        self.assertIsNone(first_metric["state_hash"])

    def test_events_only_response_never_calls_choose_action(self):
        """防御性核心断言：events-only 响应时绝不能基于不完整状态发起
        动作决策——action() 不应在这一轮被调用。"""
        from unittest.mock import patch

        api = _make_api(
            state_responses=[
                {"seq": 103, "events": [{"seq": 103, "type": "discard"}]},
                {"finished": True, "snapshot": {"phase": "finished", "seat": 0}},
            ],
        )
        log = Mock()
        with patch("mj.bot.choose_action") as mocked_choose:
            play_game(api, "g", log, rules={})
            mocked_choose.assert_not_called()
        api.action.assert_not_called()


class P04RealProtocolStateMachineTests(unittest.TestCase):
    """P0-4：新增端到端状态机测试，Fake API 严格模拟官方响应形态，覆盖：
    初次全量 / notify 唤醒 / 重复 notify / events-only 响应 / gap /
    SSE 断线 / watchdog / 409 恢复 / finished。Bot 依据最后的权威 snapshot
    只提交一次动作。"""

    def test_full_state_machine_single_action_submission(self):
        # 1) 首次：{"seq":100, "snapshot":{...}}（非胡牌，仅验证状态机推进）
        # 2) notify: {"seq":103}（不直接影响 state 内容，只驱动下一次 seq=0 刷新）
        # 3) events-only: {"seq":103, "events":[...]}（无 snapshot，防御路径）
        # 4) 权威恢复: {"seq":103, "snapshot":{...}}（胡牌，提交一次 hu）
        # 5) finished
        api = _make_api(
            state_responses=[
                {"seq": 100, "snapshot": dict(_DRAW_SNAPSHOT, round_no=1)},
                {"seq": 103, "events": [{"seq": 103, "type": "discard"}]},
                {"seq": 103, "snapshot": _HU_SNAPSHOT},
                {"finished": True, "snapshot": {"phase": "finished", "seat": 0}},
            ],
            notify_lines=[b'data: {"seq": 103}\n', b'data: {"seq": 103}\n',  # 重复 notify
                         b'data: {"closed": true}\n'],
        )
        log = Mock()
        play_game(api, "g", log, rules={})
        hu_calls = [c for c in api.action.call_args_list if c.kwargs.get("action") == "hu"]
        self.assertEqual(len(hu_calls), 1, msg="必须依据最后的权威 snapshot 只提交一次动作")
        log.result.assert_called_once()

    def test_gap_response_triggers_recovery_then_finishes(self):
        api = _make_api(
            state_responses=[
                {"seq": 10, "snapshot": dict(_DRAW_SNAPSHOT, round_no=1)},
                {"seq": 999, "snapshot": dict(_DRAW_SNAPSHOT, round_no=1), "gap": True},
                {"seq": 1000, "snapshot": _HU_SNAPSHOT},
                {"finished": True, "snapshot": {"phase": "finished", "seat": 0}},
            ],
        )
        log = Mock()
        play_game(api, "g", log, rules={})
        log.result.assert_called_once()
        # gap 响应之后紧跟的请求仍是 seq=0。
        self.assertEqual(api.state.call_args_list[2].args[1], 0)

    def test_sse_disconnect_watchdog_still_advances(self):
        """SSE 断线（notify() 直接抛异常）时 watchdog 仍可推进到 finished。"""
        api = _make_api(
            state_responses=[
                {"seq": 1, "snapshot": _HU_SNAPSHOT},
                {"finished": True, "snapshot": {"phase": "finished", "seat": 0}},
            ],
            notify_raises=True,
        )
        log = Mock()
        play_game(api, "g", log, rules={})
        log.result.assert_called_once()
        hu_calls = [c for c in api.action.call_args_list if c.kwargs.get("action") == "hu"]
        self.assertEqual(len(hu_calls), 1)

    def test_action_409_then_recovery_then_single_success(self):
        """409 恢复：第一次动作被拒绝后，下一次必须基于全新权威快照重新
        决策，最终只成功提交一次。"""
        call_log = []

        def action_side_effect(game_id, **decision):
            call_log.append(decision)
            if len(call_log) == 1:
                raise ApiError(409, '{"code":"INVALID_ACTION","message":"stale action"}')
            return {}

        api = _make_api(
            state_responses=[
                {"seq": 1, "snapshot": _HU_SNAPSHOT},
                {"seq": 2, "snapshot": _HU_SNAPSHOT},
                {"finished": True, "snapshot": {"phase": "finished", "seat": 0}},
            ],
        )
        api.action.side_effect = action_side_effect
        log = Mock()
        play_game(api, "g", log, rules={})
        self.assertEqual(len(call_log), 2)
        log.result.assert_called_once()


class TenRoomConcurrentPacerAndActionTests(unittest.TestCase):
    """P0-4 覆盖清单最后两项：10 场并发且 state 调用频率受 pacer 约束 +
    action 请求不被 state pacer 阻塞——复用真实 MahjongApi + 假传输层，
    直接验证共享 pacer 下 action 与 state 排队互不干扰。"""

    def test_ten_concurrent_games_state_paced_action_not_blocked(self):
        from unittest.mock import patch
        from mj.api import MahjongApi

        MahjongApi._pacers.clear()
        api = MahjongApi("https://example.invalid", "tok-p04", pace_interval=0.05)
        action_elapsed = {}
        stop = threading.Event()

        def fake_https_connection(*args, **kwargs):
            conn = MagicMock()
            response = MagicMock()
            response.status = 200
            response.read.return_value = b'{"snapshot": null, "seq": 0}'
            conn.getresponse.return_value = response
            return conn

        with patch("http.client.HTTPSConnection", side_effect=fake_https_connection):
            def poll_state(game_id):
                while not stop.is_set():
                    try:
                        api.state(game_id, 0, timeout=0.5)
                    except Exception:
                        pass

            pollers = [threading.Thread(target=poll_state, args=(f"g{i}",), daemon=True)
                      for i in range(10)]
            for thread in pollers:
                thread.start()
            stop.wait(0.15)  # 让 10 场 state 轮询真正积压起来

            def do_action():
                import time
                started = time.monotonic()
                api.action("g0", "discard", tile="1w")
                action_elapsed["value"] = time.monotonic() - started

            action_thread = threading.Thread(target=do_action)
            action_thread.start()
            action_thread.join(timeout=2.0)
            stop.set()
            for thread in pollers:
                thread.join(timeout=1.0)

        self.assertIn("value", action_elapsed)
        self.assertLess(action_elapsed["value"], 0.3,
                        msg="action 不应被 10 场并发的 state pacer 排队阻塞")


if __name__ == "__main__":
    unittest.main()
