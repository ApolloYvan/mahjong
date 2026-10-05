import unittest
from unittest.mock import MagicMock, Mock, patch

from mj import bot as bot_module
from mj.bot import _fetch_snapshot, _gang_bomb_choice, choose_action, choose_discard, play_game


class SevenPairsClaimTests(unittest.TestCase):
    HAND = ["1w", "1w", "3w", "3w", "5w", "5w", "7w", "7w", "9w", "9w", "2t", "4t", "8t"]

    def _snapshot(self, rivers):
        return {"phase": "response_peng", "seat": 0, "responding_seats": [0],
                "window_tile": "1w", "my_hand": self.HAND, "melds": [[], [], [], []],
                "discards": rivers, "dealer": 1, "wall_remaining": 50,
                "god": {"catch_play": False, "chain_count": 0}}

    def test_alive_pair_route_claim_rejected(self):
        # 首副露会关闭有效七对路线，不能以无收益碰牌破坏它。
        action = choose_action(self._snapshot([[], [], [], []]))
        self.assertEqual(action["action"], "pass")

    def test_dead_pair_route_still_requires_first_claim_improvement(self):
        rivers = [["2t", "2t", "2t", "4t", "4t", "4t", "8t", "8t", "8t", "8t"], [], [], []]
        action = choose_action(self._snapshot(rivers))
        self.assertEqual(action, {"action": "pass", "tile": ""})


class GangBombTests(unittest.TestCase):
    def test_canonical_bomb_gangs(self):
        # 爆头态摸成暗杠: 四组成型 + 白, 杠后仍听任意 → ×4 杠爆优先于 ×2 即胡
        hand = ["1w", "1w", "1w", "1w", "4w", "5w", "6w", "7w", "8w", "9w",
                "1t", "2t", "3t", "白"]
        snapshot = {"drawn_tile": "1w", "my_hand": hand, "seat": 0,
                    "wall_remaining": 40, "melds": [[], [], [], []]}
        self.assertEqual(_gang_bomb_choice(snapshot), {"action": "gang", "tile": "1w"})

    def test_broken_wait_never_gangs(self):
        # 杠后打断顺子(非听任意): 不打, 保 ×2 即胡
        hand = ["1w", "1w", "1w", "1w", "2w", "3w", "5w", "5w", "6w", "白",
                "2t", "3t", "4t", "9t"]
        snapshot = {"drawn_tile": "1w", "my_hand": hand, "seat": 0,
                    "wall_remaining": 40, "melds": [[], [], [], []]}
        self.assertIsNone(_gang_bomb_choice(snapshot))

    def test_no_quad_no_bomb(self):
        hand = ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w",
                "1t", "2t", "3t", "白", "东"]
        snapshot = {"drawn_tile": "东", "my_hand": hand, "seat": 0,
                    "wall_remaining": 40, "melds": [[], [], [], []]}
        self.assertIsNone(_gang_bomb_choice(snapshot))

    def test_never_gang_joker(self):
        hand = ["白", "白", "白", "白", "4w", "5w", "6w", "7w", "8w", "9w",
                "1t", "2t", "3t", "1w"]
        snapshot = {"drawn_tile": "白", "my_hand": hand, "seat": 0,
                    "wall_remaining": 40, "melds": [[], [], [], []]}
        self.assertIsNone(_gang_bomb_choice(snapshot))

    def test_wall_tail_forbids_bomb_gang_at_20(self):
        # 20 张墙尾（含）以内所有杠路径均禁止，含杠爆窗口；历史 bug 用 <=4 绕过该限制。
        hand = ["1w", "1w", "1w", "1w", "4w", "5w", "6w", "7w", "8w", "9w",
                "1t", "2t", "3t", "白"]
        snapshot = {"drawn_tile": "1w", "my_hand": hand, "seat": 0,
                    "wall_remaining": 20, "melds": [[], [], [], []]}
        self.assertIsNone(_gang_bomb_choice(snapshot))

    def test_wall_tail_allows_bomb_gang_at_21(self):
        hand = ["1w", "1w", "1w", "1w", "4w", "5w", "6w", "7w", "8w", "9w",
                "1t", "2t", "3t", "白"]
        snapshot = {"drawn_tile": "1w", "my_hand": hand, "seat": 0,
                    "wall_remaining": 21, "melds": [[], [], [], []]}
        self.assertEqual(_gang_bomb_choice(snapshot), {"action": "gang", "tile": "1w"})


class FreezeRemovedTests(unittest.TestCase):
    """冻圈弃白机制已移除 (2026-09-20 用户决策): 白板是百搭+唯一安全牌,
    不再为防御阻挡而丢弃; 烂牌局面回归价值路径决策。"""

    def _snapshot(self, melds):
        return {"seat": 1, "wall_remaining": 57, "melds": melds}

    def test_old_freeze_scenario_uses_value_path(self):
        # 旧冻圈必触发的场景(13 孤张+白, 对手三副露冲刺): 现在走价值路径,
        # 不再有"无条件打白"捷径; 弃牌必须合法(来自手牌)
        hand = ["东", "南", "西", "北", "中", "发", "1w", "4w", "7w", "2b", "5b", "8b", "3t", "白"]
        melds = [[], [], [{"kind": "chi", "tiles": ["5b", "6b", "7b"]},
                          {"kind": "gang_ming", "tiles": ["1b", "1b", "1b", "1b"]}], []]
        result = choose_discard(self._snapshot(melds) | {"my_hand": hand})
        self.assertEqual(result["action"], "discard")
        self.assertIn(result["tile"], hand)


class FetchSnapshotTests(unittest.TestCase):
    def test_returns_response_and_snapshot(self):
        api = Mock()
        response = {"snapshot": {"phase": "draw"}, "seq": 7}
        api.state.return_value = response
        log = Mock()
        out_response, snapshot = _fetch_snapshot(api, "g", log)
        self.assertEqual(snapshot, {"phase": "draw"})
        self.assertEqual(out_response, response)
        # B3 返修：正常轮询路径不再无条件调用 log.state()（去重/采样职责
        # 转移到 observer.observe_state()，见 mj/bot.py::_fetch_snapshot）。
        log.state.assert_not_called()
        self.assertEqual(api.state.call_args.args, ("g", 0))
        self.assertEqual(api.state.call_args.kwargs.get("timeout"), 2.0)

    def test_timeout_returns_none_pair(self):
        api = Mock()
        api.state.side_effect = TimeoutError()
        self.assertEqual(_fetch_snapshot(api, "g"), (None, None))


class PlayGameTests(unittest.TestCase):
    def _api(self, states):
        api = MagicMock()
        api.rules.return_value = {}
        api.notify.return_value = MagicMock()
        api.notify.return_value.__iter__ = Mock(side_effect=TypeError("not iterable"))
        api.state.side_effect = states
        return api

    def test_exits_on_finished(self):
        api = self._api([
            {"snapshot": {"phase": "deal", "seat": 0, "round_no": 1}},
            {"finished": True, "snapshot": {"phase": "finished", "seat": 0}},
        ])
        log = Mock()
        play_game(api, "g", log, rules={})
        log.result.assert_called_once()

    def test_exits_on_finished_phase(self):
        api = self._api([
            {"snapshot": {"phase": "finished", "seat": 0, "round_no": 8}},
        ])
        log = Mock()
        play_game(api, "g", log, rules={})
        log.result.assert_called_once()

    def test_submit_hu_then_finish(self):
        snapshot = {
            "phase": "draw",
            "seat": 0,
            "turn": 0,
            "round_no": 1,
            "my_hand": ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w", "1t", "1t", "1t", "西", "西"],
            "drawn_tile": "西",
            "melds": [[], [], [], []],
            "wall_remaining": 50,
        }
        api = self._api([
            {"snapshot": snapshot},
            {"finished": True, "snapshot": {"phase": "finished", "seat": 0}},
        ])
        log = Mock()
        play_game(api, "g", log, rules={})
        api.action.assert_called_once()
        kwargs = api.action.call_args.kwargs
        self.assertEqual(kwargs.get("action"), "hu")

    def test_409_once_marks_response_window_stale_no_resubmit(self):
        """P0 修复：响应阶段发生 409 后立即标记该窗口陈旧（不等到第二次
        409），同一窗口（相同 snapshot 反复轮询返回）不得再提交第二次
        同一决定——旧版行为是"两次 409 后才标记 responded"，新版第一次
        409 就必须停止对同一窗口的重试。"""
        from mj.api import ApiError

        snapshot = {
            "phase": "response_peng",
            "seat": 1,
            "responding_seats": [1],
            "round_no": 1,
            "my_hand": [],
            "window_tile": "5w",
            "melds": [[], [], [], []],
            "wall_remaining": 40,
        }
        api = self._api([{"snapshot": snapshot} for _ in range(3)] +
                        [{"finished": True, "snapshot": {"phase": "finished", "seat": 1}}])
        api.action.side_effect = ApiError(409, "INVALID_ACTION")
        log = Mock()
        with patch("mj.bot.choose_action", return_value={"action": "peng", "tile": "5w"}):
            play_game(api, "g", log, rules={})
        self.assertEqual(api.action.call_count, 1)

    def test_409_draw_fallback_sent_and_logged(self):
        # 独立复核 P0-3 返修：draw 阶段连续 409 后，fallback 决策必须基于
        # 每次全新拿到的权威 snapshot（本测试固定重复同一 snapshot 对象，
        # 但每次都经过真实的一次 /state 拉取 + is_fallback 判定），不得在
        # 同一次 409 异常处理里就地对旧 snapshot 发起 fallback。
        # 门禁：rejections["draw"]>=2 时进入 fallback；fallback_attempts
        # 上限为 2 次——被拒尝试与回退都必须落日志 (s44 第三局幽灵南教训)。
        from mj.api import ApiError

        snapshot = {
            "phase": "draw",
            "seat": 0,
            "turn": 0,
            "round_no": 1,
            "my_hand": ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w", "1t", "2t", "3t", "南"],
            "drawn_tile": "西",
            "melds": [[], [], [], []],
            "wall_remaining": 40,
        }
        api = self._api([{"snapshot": snapshot} for _ in range(5)] +
                        [{"finished": True, "snapshot": {"phase": "finished", "seat": 0}}])
        api.action.side_effect = ApiError(409, "INVALID_ACTION")
        log = Mock()
        with patch("mj.bot.choose_discard", return_value={"action": "discard", "tile": "1w"}):
            play_game(api, "g", log, rules={})
        # 5 次迭代：前2次是正常 choose_discard 决策（rejections 0->1->2），
        # 第3/4次达到 fallback 门禁（rejections>=2 且 fallback_attempts<2），
        # 第5次 fallback_attempts 已达上限(2)，回退到正常 choose_discard
        # 决策（rejections 继续累加，但不再 fallback）。
        self.assertEqual(api.action.call_count, 5)
        # 独立复核修复后：每一次 409（无论是否 fallback 尝试）都必须完整
        # 记录 action_rejected——不吞掉任何一次真实拒绝。
        self.assertEqual(log.action_rejected.call_count, 5)
        self.assertEqual(log.fallback_sent.call_count, 2)
        for call in log.fallback_sent.call_args_list:
            self.assertEqual(call.args[2], "西")
            self.assertFalse(call.args[3])

    def test_409_draw_fallback_uses_fresh_snapshot_not_stale_snapshot_from_409(self):
        """独立复核 P0-3 规则6 的直接回归：fallback 的 tile 必须来自
        fallback 决策发生时那一次全新 fetch 的 snapshot.drawn_tile，不是
        触发 409 的旧 snapshot——用两份 drawn_tile 不同的 snapshot 验证
        fallback 用的是"当前这一次"的新值。"""
        from mj.api import ApiError

        stale_snapshot = {
            "phase": "draw", "seat": 0, "turn": 0, "round_no": 1,
            "my_hand": ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w", "1t", "2t", "3t", "南"],
            "drawn_tile": "西", "melds": [[], [], [], []], "wall_remaining": 40,
        }
        fresh_snapshot = dict(stale_snapshot, drawn_tile="北")
        api = self._api([
            {"snapshot": stale_snapshot},
            {"snapshot": stale_snapshot},
            {"snapshot": fresh_snapshot},  # fallback 门禁触发时应使用这份新 snapshot 的 drawn_tile
            {"finished": True, "snapshot": {"phase": "finished", "seat": 0}},
        ])
        api.action.side_effect = ApiError(409, "INVALID_ACTION")
        log = Mock()
        with patch("mj.bot.choose_discard", return_value={"action": "discard", "tile": "1w"}):
            play_game(api, "g", log, rules={})
        self.assertEqual(log.fallback_sent.call_count, 1)
        self.assertEqual(log.fallback_sent.call_args_list[0].args[2], "北")

    def test_409_response_never_sends_fallback(self):
        from mj.api import ApiError

        snapshot = {
            "phase": "response_peng",
            "seat": 1,
            "responding_seats": [1],
            "round_no": 1,
            "my_hand": [],
            "window_tile": "5w",
            "melds": [[], [], [], []],
            "wall_remaining": 40,
        }
        api = self._api([{"snapshot": snapshot} for _ in range(3)] +
                        [{"finished": True, "snapshot": {"phase": "finished", "seat": 1}}])
        api.action.side_effect = ApiError(409, "INVALID_ACTION")
        log = Mock()
        with patch("mj.bot.choose_action", return_value={"action": "peng", "tile": "5w"}):
            play_game(api, "g", log, rules={})
        # P0 修复：响应阶段第一次 409 后该窗口即被标记陈旧，不再重复提交
        # 同一决定——action_rejected 只应记录这一次真实拒绝，不应为同一
        # 窗口重复调用。
        self.assertEqual(log.action_rejected.call_count, 1)
        log.fallback_sent.assert_not_called()


class SignalExitTests(unittest.TestCase):
    """2026-09-22 存活可观测性修复（§3.5）：SIGTERM/SIGINT 必须先写终止记录、
    close 日志，再退出——不真的发信号，只注入假 log 校验行为（避免杀掉测试
    进程本身）。"""

    def test_sigterm_closes_log_and_writes_termination_record(self):
        fake_log = Mock()
        captured = {}

        def fake_signal(sig, handler):
            captured[sig] = handler

        with patch.object(bot_module.signal, "signal", side_effect=fake_signal), \
             patch.object(bot_module.os, "_exit") as fake_exit:
            bot_module._install_signal_handlers(fake_log)
            handler = captured[bot_module.signal.SIGTERM]
            handler(bot_module.signal.SIGTERM, None)

        fake_log.append.assert_called_once()
        kind, payload = fake_log.append.call_args[0]
        self.assertEqual(kind, "error")
        self.assertEqual(payload["reason"], "signal_exit")
        self.assertEqual(payload["signal"], "SIGTERM")
        fake_log.close.assert_called_once_with(timeout=10.0)
        fake_exit.assert_called_once()

    def test_sigterm_reports_active_game_ids(self):
        fake_log = Mock()
        captured = {}

        def fake_signal(sig, handler):
            captured[sig] = handler

        with bot_module._active_game_ids_lock:
            bot_module._active_game_ids.add("g_in_flight")
        try:
            with patch.object(bot_module.signal, "signal", side_effect=fake_signal), \
                 patch.object(bot_module.os, "_exit"):
                bot_module._install_signal_handlers(fake_log)
                handler = captured[bot_module.signal.SIGTERM]
                handler(bot_module.signal.SIGTERM, None)
        finally:
            with bot_module._active_game_ids_lock:
                bot_module._active_game_ids.discard("g_in_flight")

        _, payload = fake_log.append.call_args[0]
        self.assertIn("g_in_flight", payload["active_games"])


class BaotouGangOpenTests(unittest.TestCase):
    """rule_baotou_gang_open_enabled：爆头态补杠 / 暗杠后仍听任意 → 杠开 ×2。"""

    ON = {"rule_baotou_gang_open_enabled": 1}

    def _bu_gang_snapshot(self):
        hand = ["发", "4w", "5w", "6w", "7w", "8w", "9w", "1t", "2t", "3t", "白"]
        return {"drawn_tile": "发", "my_hand": hand, "seat": 0, "wall_remaining": 40,
                "melds": [[{"kind": "peng", "tiles": ["发", "发", "发"]}], [], [], []]}

    def test_bu_gang_in_baotou(self):
        from mj.bot import _baotou_gang_open_choice
        self.assertEqual(_baotou_gang_open_choice(self._bu_gang_snapshot(), self.ON),
                         {"action": "gang", "tile": "发"})

    def test_off_by_default(self):
        from mj.bot import _baotou_gang_open_choice
        self.assertIsNone(_baotou_gang_open_choice(self._bu_gang_snapshot(), {}))

    def test_wall_tail_forbids(self):
        from mj.bot import _baotou_gang_open_choice
        snap = {**self._bu_gang_snapshot(), "wall_remaining": 20}
        self.assertIsNone(_baotou_gang_open_choice(snap, self.ON))

    def test_concealed_quad_breaking_wait_not_ganged(self):
        from mj.bot import _baotou_gang_open_choice
        hand = ["1w", "1w", "1w", "1w", "2w", "3w", "5w", "5w", "6w", "白",
                "2t", "3t", "4t", "9t"]
        snap = {"drawn_tile": "9t", "my_hand": hand, "seat": 0, "wall_remaining": 40,
                "melds": [[], [], [], []]}
        self.assertIsNone(_baotou_gang_open_choice(snap, self.ON))
