"""响应窗口"至多提交一次"P0 修复回归测试：按稳定窗口身份
（``mj.bot._response_window_key``）而不是 phase+seat 判断，覆盖任务书
要求的 5 类真实 409 类型 + 新窗口放行 + 未知 409 完整记录。"""
import unittest
from unittest.mock import MagicMock, Mock, patch

from mj.api import ApiError
from mj.bot import _response_window_key, play_game


def _api(states):
    api = MagicMock()
    api.rules.return_value = {}
    api.notify.return_value = MagicMock()
    api.notify.return_value.__iter__ = Mock(side_effect=TypeError("not iterable"))
    api.state.side_effect = states
    return api


class ResponseWindowKeyUnitTests(unittest.TestCase):
    """P0-2 独立复核修复：窗口键的 seq 分量必须来自调用方显式传入的
    ``returned_seq``（/state 响应外层 seq），不得继续读取
    ``snapshot.get("seq")``。"""

    def test_key_stable_for_identical_window(self):
        snapshot = {"round_no": 1, "phase": "response_peng", "window_tile": "5w",
                    "responding_seats": [1, 2]}
        self.assertEqual(_response_window_key("g1", snapshot, 4),
                          _response_window_key("g1", snapshot, 4))

    def test_key_differs_when_round_advances(self):
        base = {"round_no": 1, "phase": "response_peng", "window_tile": "5w",
                "responding_seats": [1, 2]}
        advanced = dict(base, round_no=2)
        self.assertNotEqual(_response_window_key("g1", base, 4), _response_window_key("g1", advanced, 4))

    def test_key_differs_when_window_tile_advances(self):
        base = {"round_no": 1, "phase": "response_peng", "window_tile": "5w",
                "responding_seats": [1, 2]}
        advanced = dict(base, window_tile="6w")
        self.assertNotEqual(_response_window_key("g1", base, 4), _response_window_key("g1", advanced, 4))

    def test_key_ignores_stale_inner_snapshot_seq_field(self):
        """独立复核发现的缺陷：即便 snapshot 内层仍残留一个（可能陈旧的）
        ``seq`` 字段，窗口键也绝不能读取它——必须完全依赖调用方显式传入的
        ``returned_seq``。"""
        snapshot_with_inner_seq = {"round_no": 1, "phase": "response_peng", "window_tile": "5w",
                                   "responding_seats": [1, 2], "seq": 999}
        self.assertEqual(
            _response_window_key("g1", snapshot_with_inner_seq, 4),
            _response_window_key("g1", dict(snapshot_with_inner_seq, seq=1), 4),
            msg="窗口键不应受 snapshot 内层 seq 字段影响，只应受 returned_seq 影响",
        )

    def test_key_differs_when_returned_seq_advances(self):
        """P0-2 新增测试1：snapshot 完全相同但外层 returned_seq 不同，
        窗口键必须不同。"""
        snapshot = {"round_no": 1, "phase": "response_peng", "window_tile": "5w",
                    "responding_seats": [1, 2]}
        self.assertNotEqual(_response_window_key("g1", snapshot, 4),
                             _response_window_key("g1", snapshot, 5))

    def test_key_allows_two_windows_with_same_tile_and_seats_via_returned_seq(self):
        """P0-2 新增测试2：同一轮两次弃出相同牌、相同 responding_seats，
        只要外层 returned_seq 不同，仍可分别响应（窗口键不同）。"""
        first_window = {"round_no": 3, "phase": "response_chi", "window_tile": "7t",
                        "responding_seats": [1]}
        second_window = dict(first_window)  # 完全相同的弃牌/响应者组合
        self.assertNotEqual(
            _response_window_key("g1", first_window, 10),
            _response_window_key("g1", second_window, 12),
        )

    def test_key_normalizes_responding_seats_order(self):
        a = {"round_no": 1, "phase": "response_peng", "window_tile": "5w",
             "responding_seats": [2, 1]}
        b = dict(a, responding_seats=[1, 2])
        self.assertEqual(_response_window_key("g1", a, 4), _response_window_key("g1", b, 4))

    def test_key_falls_back_to_last_discard_and_discarded_tile(self):
        last_discard = {"round_no": 1, "phase": "response_chi", "last_discard": "7t",
                        "responding_seats": [1]}
        discarded_tile = {"round_no": 1, "phase": "response_chi", "discarded_tile": "7t",
                          "responding_seats": [1]}
        self.assertEqual(_response_window_key("g1", last_discard, 4),
                          _response_window_key("g1", discarded_tile, 4))


class RealFourZeroNineTypesStopRetryTests(unittest.TestCase):
    """任务书要求：用真实 409 类型分别覆盖 not your response turn /
    already passed / cannot pass in phase 1 / chi only in chi window /
    peng only in peng window——每种都必须在第一次 409 后停止对同一窗口
    重试，不发送猜测式 fallback。"""

    REAL_409_MESSAGES = {
        "not_your_response_turn": '{"code":"INVALID_ACTION","message":"not your response turn"}',
        "already_passed": '{"code":"INVALID_ACTION","message":"already passed"}',
        "cannot_pass_in_phase_1": '{"code":"INVALID_ACTION","message":"cannot pass in phase 1"}',
        "chi_only_in_chi_window": '{"code":"INVALID_ACTION","message":"chi only in chi window"}',
        "peng_only_in_peng_window": '{"code":"INVALID_ACTION","message":"peng only in peng window"}',
    }

    def _run_case(self, phase, error_body, decision):
        snapshot = {
            "phase": phase, "seat": 1, "responding_seats": [1], "round_no": 1,
            "my_hand": [], "window_tile": "5w", "melds": [[], [], [], []],
            "wall_remaining": 40,
        }
        api = _api([{"snapshot": snapshot} for _ in range(3)] +
                   [{"finished": True, "snapshot": {"phase": "finished", "seat": 1}}])
        api.action.side_effect = ApiError(409, error_body)
        log = Mock()
        with patch("mj.bot.choose_action", return_value=decision):
            play_game(api, "g", log, rules={})
        return api, log

    def test_all_five_real_409_message_types_stop_retry_on_first_hit(self):
        cases = [
            ("response_peng", self.REAL_409_MESSAGES["not_your_response_turn"],
             {"action": "pass", "tile": ""}),
            ("response_chi", self.REAL_409_MESSAGES["already_passed"],
             {"action": "pass", "tile": ""}),
            ("response_chi", self.REAL_409_MESSAGES["cannot_pass_in_phase_1"],
             {"action": "pass", "tile": ""}),
            ("response_chi", self.REAL_409_MESSAGES["chi_only_in_chi_window"],
             {"action": "chi", "tile": "5w", "tiles": ["4w", "6w"]}),
            ("response_peng", self.REAL_409_MESSAGES["peng_only_in_peng_window"],
             {"action": "peng", "tile": "5w"}),
        ]
        for phase, error_body, decision in cases:
            with self.subTest(error_body=error_body):
                api, log = self._run_case(phase, error_body, decision)
                self.assertEqual(api.action.call_count, 1, msg=error_body)
                self.assertEqual(log.action_rejected.call_count, 1, msg=error_body)
                log.fallback_sent.assert_not_called()

    def test_unknown_409_still_fully_logged_not_swallowed(self):
        """未知 409（不在已知 5 类分类里）不得被吞掉——action_rejected 仍需
        完整记录（脱敏后的诊断数据），且同样按窗口身份停止重试。"""
        unknown_body = '{"code":"INVALID_ACTION","message":"some brand new server rule"}'
        api, log = self._run_case("response_peng", unknown_body, {"action": "pass", "tile": ""})
        self.assertEqual(api.action.call_count, 1)
        self.assertEqual(log.action_rejected.call_count, 1)
        recorded_error = log.action_rejected.call_args.args[3]
        self.assertIn("some brand new server rule", str(recorded_error))


class NewWindowAfterAdvanceAllowsRetryTests(unittest.TestCase):
    """规则4：新一轮、新弃牌或 seq 推进后才允许新的响应——验证窗口推进后
    确实能提交新的决定（不会被旧窗口的陈旧标记永久拦住）。"""

    def test_new_round_after_stale_window_allows_new_submission(self):
        stale_snapshot = {
            "phase": "response_peng", "seat": 1, "responding_seats": [1], "round_no": 1,
            "my_hand": [], "window_tile": "5w", "melds": [[], [], [], []], "wall_remaining": 40,
        }
        fresh_snapshot = {
            "phase": "response_peng", "seat": 1, "responding_seats": [1], "round_no": 2,
            "my_hand": [], "window_tile": "9b", "melds": [[], [], [], []], "wall_remaining": 78,
        }
        api = _api([
            {"snapshot": stale_snapshot},
            {"snapshot": fresh_snapshot},
            {"finished": True, "snapshot": {"phase": "finished", "seat": 1}},
        ])
        call_log = []

        def action_side_effect(game_id, **decision):
            call_log.append(decision)
            if len(call_log) == 1:
                raise ApiError(409, '{"code":"INVALID_ACTION","message":"not your response turn"}')
            return {}

        api.action.side_effect = action_side_effect
        log = Mock()
        with patch("mj.bot.choose_action", return_value={"action": "peng", "tile": "9b"}):
            play_game(api, "g", log, rules={})
        # 第一次（陈旧窗口）被 409 拒绝，第二次（新一轮新窗口）必须能正常提交。
        self.assertEqual(len(call_log), 2)
        self.assertEqual(log.action_rejected.call_count, 1)


class ReturnedSeqDrivenWindowIntegrationTests(unittest.TestCase):
    """P0-2 新增测试3/4（独立复核要求）：通过完整 play_game() 集成路径，
    验证外层 returned_seq 才是窗口身份的权威来源。"""

    def test_repeated_returned_seq_only_allows_one_submission(self):
        """新增测试3：重复收到同一 returned_seq，只允许提交一次——即使
        snapshot 字典是同一个对象、被轮询到两次，只要 /state 响应外层
        seq 相同，第二次轮询必须被判定为"同一窗口已提交"而跳过。"""
        snapshot = {
            "phase": "response_peng", "seat": 1, "responding_seats": [1], "round_no": 1,
            "my_hand": [], "window_tile": "5w", "melds": [[], [], [], []], "wall_remaining": 40,
        }
        api = _api([
            {"snapshot": snapshot, "seq": 7},
            {"snapshot": snapshot, "seq": 7},  # 同一 returned_seq 重复出现
            {"finished": True, "snapshot": {"phase": "finished", "seat": 1}},
        ])
        log = Mock()
        with patch("mj.bot.choose_action", return_value={"action": "peng", "tile": "5w"}):
            play_game(api, "g", log, rules={})
        self.assertEqual(api.action.call_count, 1)

    def test_out_of_order_notify_does_not_trigger_stale_window_action(self):
        """新增测试4：倒序 notify 不触发旧窗口动作——即使 notify 线程先收到
        一个较大的 seq、随后又"倒序"收到一个较小的 seq，_SeqTracker 只保留
        已知最大值（不会因为倒序通知而回退/重复触发对旧窗口的动作）。"""
        from mj.bot import _SeqTracker

        tracker = _SeqTracker()
        tracker.observe_notify(10)
        tracker.observe_notify(3)  # 倒序到达，不应覆盖已知的更大 seq
        with tracker._lock:
            observed = tracker._notified_seq
        self.assertEqual(observed, 10)


if __name__ == "__main__":
    unittest.main()
