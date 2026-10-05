"""正式赛事生命周期状态机测试：录制形状 + 假服务覆盖完整生命周期，
虚拟时钟测试长等待，覆盖 429/超时/断线/非法动作与恢复。
"""
import unittest
from unittest.mock import MagicMock

from mj.api import ApiError
from mj.tournament import (
    Decision,
    GameLedger,
    TournamentLifecycle,
    TournamentSnapshot,
    run_tournament,
)


def snap(tournament_id="t_abc", status=None, active_games=None, **kw):
    return TournamentSnapshot(
        tournament_id=tournament_id, status=status,
        active_games=active_games or [], **kw,
    )


class LifecycleStateMachineTests(unittest.TestCase):
    """纯状态机测试：不涉及网络，直接喂快照序列。"""

    def test_no_tournament_id_waits(self):
        lc = TournamentLifecycle()
        decision = lc.step(snap(tournament_id=None, status=None))
        self.assertEqual(decision.action, "wait")
        self.assertEqual(decision.reason, "no_tournament_id")

    def test_registers_when_status_unknown(self):
        lc = TournamentLifecycle()
        decision = lc.step(snap(status=None))
        self.assertEqual(decision.action, "register")

    def test_full_lifecycle_registering_to_running_to_stage_done_to_stage_open_to_finished(self):
        lc = TournamentLifecycle()
        # 1. 尚无状态：报名（confirm 模拟 api.register() 调用成功）
        self.assertEqual(lc.step(snap(status=None)).action, "register")
        lc.confirm_registered()
        # 2. registering：首次需要 ready（confirm 模拟 api.ready() 调用成功）
        d = lc.step(snap(status="registering"))
        self.assertEqual(d.action, "ready")
        lc.confirm_readied()
        # 3. registering 仍在等待开赛，本轮不再重复 ready（已确认）
        d = lc.step(snap(status="registering"))
        self.assertIn(d.action, ("wait",))
        # 4. running 有 active_games：打
        d = lc.step(snap(status="running", active_games=[{"game_id": "g1"}]))
        self.assertEqual(d.action, "play")
        self.assertEqual(d.game_ids, ["g1"])
        # 5. running 但 active_games 为空（阶段间隙）：必须继续等待，不能判定结束
        d = lc.step(snap(status="running", active_games=[]))
        self.assertEqual(d.action, "wait")
        self.assertEqual(d.reason, "running_no_active_games")
        # 6. stage_done：等待管理员推进，不是结束
        d = lc.step(snap(status="stage_done", active_games=[]))
        self.assertEqual(d.action, "wait")
        self.assertEqual(d.reason, "stage_done_waiting_for_admin")
        # 7. stage_open：必须重新确认出席（不是"已经ready过就跳过"）
        d = lc.step(snap(status="stage_open"))
        self.assertEqual(d.action, "ready")
        self.assertEqual(d.reason, "enter_stage_open")
        lc.confirm_readied()
        # 8. 再次轮询仍是 stage_open：不重复 ready（已确认）
        d = lc.step(snap(status="stage_open"))
        self.assertEqual(d.action, "wait")
        # 9. 进入下一阶段 running
        d = lc.step(snap(status="running", active_games=[{"game_id": "g2"}]))
        self.assertEqual(d.action, "play")
        # 10. 决赛结束
        d = lc.step(snap(status="finished"))
        self.assertEqual(d.action, "done")
        self.assertTrue(lc.done)

    def test_ready_keeps_retrying_until_confirmed(self):
        """P0 回归锁定：ready 决定在 confirm_readied() 被调用前必须持续重发，
        不能因为"已经发出过一次决定"就被状态机静默跳过。"""
        lc = TournamentLifecycle()
        lc.step(snap(status=None))
        lc.confirm_registered()
        for _ in range(5):
            d = lc.step(snap(status="stage_open"))
            self.assertEqual(d.action, "ready")
        # 直到显式确认，才会转为 wait
        lc.confirm_readied()
        d = lc.step(snap(status="stage_open"))
        self.assertEqual(d.action, "wait")

    def test_register_keeps_retrying_until_confirmed(self):
        """P0 回归锁定：register 决定在 confirm_registered() 被调用前必须
        持续重发，不能因为"已经发出过一次决定"就被状态机静默跳过。"""
        lc = TournamentLifecycle()
        for _ in range(5):
            d = lc.step(snap(status=None))
            self.assertEqual(d.action, "register")
        lc.confirm_registered()
        d = lc.step(snap(status="registering"))
        self.assertEqual(d.action, "ready")

    def test_same_stage_number_fault_restart_requires_reready(self):
        """同阶段号故障重赛：从 stage_open 掉到 running（故障恢复中间态）
        再回到 stage_open，必须重新确认出席——不能靠"已经 ready 过"的缓存。"""
        lc = TournamentLifecycle()
        lc.step(snap(status=None))
        d = lc.step(snap(status="stage_open"))
        self.assertEqual(d.action, "ready")
        # 故障：服务端把状态打回 running（无 active_games，重赛准备中）
        d = lc.step(snap(status="running", active_games=[]))
        self.assertEqual(d.action, "wait")
        # 重新回到 stage_open：必须再次 ready（幂等，但状态机必须重新触发）
        d = lc.step(snap(status="stage_open"))
        self.assertEqual(d.action, "ready")
        self.assertEqual(d.reason, "enter_stage_open")

    def test_empty_active_games_never_treated_as_finished(self):
        lc = TournamentLifecycle()
        lc.step(snap(status=None))
        lc.step(snap(status="registering"))
        for _ in range(5):
            d = lc.step(snap(status="running", active_games=[]))
            self.assertEqual(d.action, "wait")
            self.assertFalse(lc.done)

    def test_closed_and_void_are_terminal(self):
        for terminal_status in ("closed", "void", "finished"):
            with self.subTest(status=terminal_status):
                lc = TournamentLifecycle()
                d = lc.step(snap(status=terminal_status))
                self.assertEqual(d.action, "done")
                self.assertTrue(lc.done)

    def test_done_state_is_sticky(self):
        lc = TournamentLifecycle()
        lc.step(snap(status="finished"))
        self.assertTrue(lc.done)
        # 再次 step 直接返回 done，不重新判定
        d = lc.step(snap(status="running", active_games=[{"game_id": "gX"}]))
        self.assertEqual(d.action, "done")

    def test_unknown_status_waits_instead_of_erroring(self):
        lc = TournamentLifecycle()
        lc.step(snap(status=None))
        d = lc.step(snap(status="some_future_status_v99"))
        self.assertEqual(d.action, "wait")
        self.assertFalse(lc.done)


class RunTournamentVirtualClockTests(unittest.TestCase):
    """使用虚拟时钟测试长等待（决赛圈 3 小时级别）和网络异常恢复，
    完全离线，不发起真实网络请求。"""

    def _virtual_clock(self, start=0.0):
        state = {"t": start}

        def clock():
            return state["t"]

        def sleep(seconds):
            state["t"] += seconds

        return clock, sleep

    def test_long_wait_across_three_hours_with_virtual_clock(self):
        """决赛圈 15:00-18:00 是 3 小时，旧 --wait 7200s 上限会中途退出；
        新状态机需要能在虚拟时钟下稳定运行超过 2 小时而不提前放弃，
        并在真正 finished 时正确结束。"""
        clock, sleep = self._virtual_clock()
        api = MagicMock()
        # 前 2.5 小时（按 poll_interval=60s 计算约 150 次轮询）保持 running 空转，
        # 之后进入 finished。
        call_count = {"n": 0}

        def me_side_effect():
            call_count["n"] += 1
            return {"tournament_id": "t_final", "active_games": []}

        def tournament_side_effect(tid):
            # 前 150 次轮询空转 running；第 150 次之后转 finished
            if call_count["n"] < 150:
                return {"status": "running"}
            return {"status": "finished"}

        api.me.side_effect = me_side_effect
        api.tournament.side_effect = tournament_side_effect
        decision = run_tournament(
            api, max_seconds=3 * 3600, poll_interval=60.0,
            clock=clock, sleep=sleep,
        )
        self.assertEqual(decision.action, "done")
        self.assertEqual(decision.reason, "finished")
        # 虚拟时钟应已推进超过 2 小时（约 150*60s ≈ 2.5h），
        # 证明状态机确实撑过了长时段而不是提前假装完成。
        self.assertGreaterEqual(clock(), 2 * 3600)

    def test_timeout_returns_wait_not_exception(self):
        clock, sleep = self._virtual_clock()
        api = MagicMock()
        api.me.return_value = {"tournament_id": "t_x", "active_games": []}
        api.tournament.return_value = {"status": "running"}
        decision = run_tournament(
            api, max_seconds=100, poll_interval=10.0, clock=clock, sleep=sleep,
        )
        self.assertEqual(decision.action, "wait")
        self.assertEqual(decision.reason, "timeout")

    def test_429_and_timeout_retry_then_recover(self):
        """429/超时/断线：捕获后退避重试，不崩溃退出。"""
        clock, sleep = self._virtual_clock()
        api = MagicMock()
        responses = [
            ApiError(429, "rate limited"),
            TimeoutError(),
            OSError("connection reset"),
            {"tournament_id": "t_y", "active_games": []},
        ]

        def me_side_effect():
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        api.me.side_effect = me_side_effect
        api.tournament.return_value = {"status": "finished"}
        decision = run_tournament(
            api, max_seconds=1000, poll_interval=1.0, clock=clock, sleep=sleep,
        )
        self.assertEqual(decision.action, "done")
        self.assertEqual(decision.reason, "finished")

    def test_disconnect_exceeding_max_consecutive_errors_raises(self):
        """连续错误超过阈值才放弃（非法动作/持续断线视为需要人工介入）。"""
        clock, sleep = self._virtual_clock()
        api = MagicMock()
        api.me.side_effect = OSError("persistent network failure")
        with self.assertRaises(OSError):
            run_tournament(
                api, max_seconds=10000, poll_interval=1.0, clock=clock, sleep=sleep,
                max_consecutive_errors=3,
            )

    def test_illegal_action_on_register_409_treated_as_idempotent(self):
        """已报名（重复报名 409）视为幂等成功，不阻塞状态机推进。"""
        clock, sleep = self._virtual_clock()
        api = MagicMock()
        api.me.side_effect = [
            {"tournament_id": "t_z", "active_games": []},
            {"tournament_id": "t_z", "active_games": []},
        ]
        api.tournament.side_effect = [{"status": None}, {"status": "finished"}]
        api.register.side_effect = ApiError(409, "ALREADY_REGISTERED")
        decision = run_tournament(
            api, max_seconds=1000, poll_interval=1.0, clock=clock, sleep=sleep,
        )
        self.assertEqual(decision.action, "done")

    def test_play_callback_invoked_with_game_ids(self):
        clock, sleep = self._virtual_clock()
        api = MagicMock()
        api.me.side_effect = [
            {"tournament_id": "t_p", "active_games": [{"game_id": "g1"}, {"game_id": "g2"}]},
            {"tournament_id": "t_p", "active_games": []},
        ]
        api.tournament.side_effect = [{"status": "running"}, {"status": "finished"}]
        played = []
        run_tournament(
            api, max_seconds=1000, poll_interval=1.0, clock=clock, sleep=sleep,
            on_play=lambda ids: played.append(ids),
        )
        self.assertEqual(played, [["g1", "g2"]])

    def test_tournament_404_treated_as_no_status_not_crash(self):
        """/api/tournaments/{id} 404（例如赛事刚创建尚未可查）不应崩溃，
        应视为暂无状态继续等待/重试。"""
        clock, sleep = self._virtual_clock()
        api = MagicMock()
        api.me.side_effect = [
            {"tournament_id": "t_404", "active_games": []},
            {"tournament_id": "t_404", "active_games": []},
        ]
        api.tournament.side_effect = [ApiError(404, "not found"), {"status": "finished"}]
        decision = run_tournament(
            api, max_seconds=1000, poll_interval=1.0, clock=clock, sleep=sleep,
        )
        self.assertEqual(decision.action, "done")

    def test_ready_500_then_success_retries_until_confirmed(self):
        """P0 端到端回归：ready() 第一次返回 500，服务端保持 stage_open，
        必须持续重试 ready 直到成功；用户复核报告的最小复现场景
        （旧实现下 api.ready.call_count 会永远停在 1）。"""
        clock, sleep = self._virtual_clock()
        api = MagicMock()
        api.me.return_value = {"tournament_id": "t_r", "active_games": []}
        api.tournament.side_effect = (
            [{"status": None}]
            + [{"status": "stage_open"}] * 4
            + [{"status": "finished"}]
        )
        api.register.return_value = {}
        api.ready.side_effect = [ApiError(500, "server error")] + [{}] * 10
        decision = run_tournament(
            api, max_seconds=10000, poll_interval=1.0, clock=clock, sleep=sleep,
        )
        self.assertEqual(decision.action, "done")
        # ready 必须被多次调用（失败一次后重试直到成功），不能停在 1。
        self.assertGreaterEqual(api.ready.call_count, 2)

    def test_ready_persistent_failure_raises_after_command_error_threshold(self):
        """连续失败：ready 一直失败，独立的命令错误计数器必须能触发熔断
        （用户复核发现的缺陷修复：旧版命令错误与快照拉取错误共用一个
        计数器，只要快照拉取正常，register/ready 持续失败也永远不会
        触发熔断，只会空转到 max_seconds 超时）。"""
        clock, sleep = self._virtual_clock()
        api = MagicMock()
        api.me.return_value = {"tournament_id": "t_r2", "active_games": []}
        api.tournament.return_value = {"status": "stage_open"}
        api.ready.side_effect = ApiError(500, "server error")
        with self.assertRaises(ApiError):
            run_tournament(
                api, max_seconds=None, poll_interval=1.0, clock=clock, sleep=sleep,
                max_consecutive_errors=1000, max_consecutive_command_errors=5,
            )
        # 命令错误计数器独立生效：在阈值处停止，不会无限空转。
        self.assertEqual(api.ready.call_count, 5)

    def test_ready_409_already_ready_treated_as_idempotent_no_circuit_break(self):
        """协议对齐热修需求二：ready 409 若明确包含 ALREADY_READY 或
        TOURNAMENT_STARTED，视为幂等成功——confirm_readied() 并清零命令
        错误计数器，绝不会像普通失败那样累计到阈值触发熔断（哪怕连续跑
        远超过 max_consecutive_command_errors 次调用）。"""
        clock, sleep = self._virtual_clock()
        api = MagicMock()
        api.me.return_value = {"tournament_id": "t_idem", "active_games": []}
        api.tournament.return_value = {"status": "stage_open"}
        api.ready.side_effect = ApiError(409, '{"error":"ALREADY_READY"}')
        # confirm_readied() 后状态机进入 stage_open_waiting，不会再重复
        # 调用 ready；用 max_seconds 限定跑够多轮验证不会因为循环终止而
        # 掩盖"其实会一直重试"的问题。
        run_tournament(
            api, max_seconds=100, poll_interval=1.0, clock=clock, sleep=sleep,
            max_consecutive_errors=1000, max_consecutive_command_errors=5,
        )
        # 幂等成功后只应调用一次 ready（confirm_readied 后 step() 不再
        # 返回 ready 决定），而不是像旧版那样累计到阈值 5 次后熔断/抛异常。
        self.assertEqual(api.ready.call_count, 1)

        # TOURNAMENT_STARTED 标记同样视为幂等成功。
        clock2, sleep2 = self._virtual_clock()
        api2 = MagicMock()
        api2.me.return_value = {"tournament_id": "t_idem2", "active_games": []}
        api2.tournament.return_value = {"status": "stage_open"}
        api2.ready.side_effect = ApiError(409, '{"error":"TOURNAMENT_STARTED"}')
        run_tournament(
            api2, max_seconds=100, poll_interval=1.0, clock=clock2, sleep=sleep2,
            max_consecutive_errors=1000, max_consecutive_command_errors=5,
        )
        self.assertEqual(api2.ready.call_count, 1)

    def test_ready_409_unknown_reason_still_triggers_circuit_break(self):
        """协议对齐热修需求二：未明确包含 ALREADY_READY/TOURNAMENT_STARTED
        标记的 409（原因未知）不得被误判为幂等成功，必须保持原重试/熔断
        行为——累计到 max_consecutive_command_errors 阈值后抛出异常。"""
        clock, sleep = self._virtual_clock()
        api = MagicMock()
        api.me.return_value = {"tournament_id": "t_unk", "active_games": []}
        api.tournament.return_value = {"status": "stage_open"}
        api.ready.side_effect = ApiError(409, '{"error":"UNKNOWN_CONFLICT"}')
        with self.assertRaises(ApiError):
            run_tournament(
                api, max_seconds=None, poll_interval=1.0, clock=clock, sleep=sleep,
                max_consecutive_errors=1000, max_consecutive_command_errors=5,
            )
        self.assertEqual(api.ready.call_count, 5)

    def test_ready_401_not_swallowed_as_idempotent_success(self):
        """401/403 等真实错误不得被吞掉：不含 409 幂等标记，必须继续走
        原有重试/熔断路径并最终抛出，不会被误判为 ready 成功。"""
        clock, sleep = self._virtual_clock()
        api = MagicMock()
        api.me.return_value = {"tournament_id": "t_401", "active_games": []}
        api.tournament.return_value = {"status": "stage_open"}
        api.ready.side_effect = ApiError(401, '{"error":"UNAUTHORIZED"}')
        with self.assertRaises(ApiError) as ctx:
            run_tournament(
                api, max_seconds=None, poll_interval=1.0, clock=clock, sleep=sleep,
                max_consecutive_errors=1000, max_consecutive_command_errors=4,
            )
        self.assertEqual(ctx.exception.status, 401)
        self.assertEqual(api.ready.call_count, 4)

    def test_command_errors_are_not_reset_by_successful_snapshot_fetch(self):
        """P0 回归锁定（核心缺陷）：即使每一轮快照拉取（api.me/api.tournament）
        都成功，只要 ready 命令持续失败，命令错误计数器也必须能独立累计到
        阈值并触发熔断——不能被"快照拉取成功"重置为 0。这正是用户在
        tournament.py:246 指出的问题：旧版 consecutive_errors 在每轮循环
        开头因为快照拉取成功就被清零，导致熔断永远不会触发。"""
        clock, sleep = self._virtual_clock()
        api = MagicMock()
        # 快照拉取每次都成功（不抛异常），只有 ready 命令持续失败。
        api.me.return_value = {"tournament_id": "t_r3", "active_games": []}
        api.tournament.return_value = {"status": "stage_open"}
        api.ready.side_effect = ApiError(503, "service unavailable")
        with self.assertRaises(ApiError):
            run_tournament(
                api, max_seconds=None, poll_interval=0.5, clock=clock, sleep=sleep,
                max_consecutive_errors=1000, max_consecutive_command_errors=3,
            )
        self.assertEqual(api.ready.call_count, 3)
        # 快照拉取从未失败，证明熔断确实由独立的命令错误计数器触发，
        # 而不是快照拉取错误计数器（后者应该一直是 0，因为 me/tournament
        # 从未抛过异常）。
        self.assertEqual(api.me.call_count, 3)

    def test_register_500_then_success_retries_until_confirmed(self):
        """P0 端到端回归：register() 第一次返回 500，状态继续为 None，
        必须持续重试 register 直到成功（用户复核报告的最小复现场景）。"""
        clock, sleep = self._virtual_clock()
        api = MagicMock()
        api.me.return_value = {"tournament_id": "t_reg", "active_games": []}
        api.tournament.side_effect = (
            [{"status": None}] * 3
            + [{"status": "registering"}]
            + [{"status": "finished"}]
        )
        api.register.side_effect = [ApiError(500, "server error"), {}]
        api.ready.return_value = {}
        decision = run_tournament(
            api, max_seconds=10000, poll_interval=1.0, clock=clock, sleep=sleep,
        )
        self.assertEqual(decision.action, "done")
        self.assertGreaterEqual(api.register.call_count, 2)

    def test_state_advances_past_registering_without_local_confirm_still_readies(self):
        """状态推进契约：即使本地 confirm_readied 因为异常未被调用，只要服务端
        状态已经离开 registering/stage_open（例如直接进入 running 说明服务端
        已认可出席），也不应卡死在重复 ready，而应正常进入下一阶段。"""
        clock, sleep = self._virtual_clock()
        api = MagicMock()
        api.me.side_effect = [
            {"tournament_id": "t_adv", "active_games": []},
            {"tournament_id": "t_adv", "active_games": [{"game_id": "gA"}]},
            {"tournament_id": "t_adv", "active_games": []},
        ]
        api.tournament.side_effect = [
            {"status": "registering"},
            {"status": "running"},
            {"status": "finished"},
        ]
        api.ready.side_effect = ApiError(500, "server error")
        played = []
        decision = run_tournament(
            api, max_seconds=10000, poll_interval=1.0, clock=clock, sleep=sleep,
            on_play=lambda ids: played.append(ids),
        )
        self.assertEqual(decision.action, "done")
        self.assertEqual(played, [["gA"]])

    def test_no_max_seconds_runs_until_terminal_status(self):
        """默认不设硬截止（用户复核发现的缺陷修复）：max_seconds=None 时，
        状态机应完全依赖服务端终态退出，即使虚拟时钟推进远超过旧版
        3 小时默认值，也不应提前放弃。"""
        clock, sleep = self._virtual_clock()
        api = MagicMock()
        call_count = {"n": 0}

        def me_side_effect():
            call_count["n"] += 1
            return {"tournament_id": "t_nolimit", "active_games": []}

        def tournament_side_effect(tid):
            # 空转超过 5 小时（远超旧版 3 小时硬编码上限）才进入 finished。
            if call_count["n"] < 350:
                return {"status": "running"}
            return {"status": "finished"}

        api.me.side_effect = me_side_effect
        api.tournament.side_effect = tournament_side_effect
        decision = run_tournament(
            api, max_seconds=None, poll_interval=60.0, clock=clock, sleep=sleep,
        )
        self.assertEqual(decision.action, "done")
        self.assertEqual(decision.reason, "finished")
        self.assertGreaterEqual(clock(), 5 * 3600)

    def test_duplicate_game_id_not_submitted_twice_via_game_ledger(self):
        """对局去重（用户复核发现的缺陷修复）：服务端在两次轮询之间仍返回
        同一个 game_id（结算延迟/竞态）时，GameLedger 必须阻止同一局被
        提交给 on_play 两次。"""
        clock, sleep = self._virtual_clock()
        api = MagicMock()
        api.me.side_effect = [
            {"tournament_id": "t_dup", "active_games": [{"game_id": "g1"}]},
            {"tournament_id": "t_dup", "active_games": [{"game_id": "g1"}]},
            {"tournament_id": "t_dup", "active_games": []},
        ]
        api.tournament.side_effect = [
            {"status": "running"},
            {"status": "running"},
            {"status": "finished"},
        ]
        played = []
        run_tournament(
            api, max_seconds=10000, poll_interval=1.0, clock=clock, sleep=sleep,
            on_play=lambda ids: played.append(ids),
        )
        # g1 只应被提交一次，即使服务端连续两轮都在 active_games 里返回它。
        self.assertEqual(played, [["g1"]])

    def test_game_ledger_filter_new_and_forget(self):
        """GameLedger 纯函数行为：filter_new 去重，forget 可显式清除。"""
        ledger = GameLedger()
        self.assertEqual(ledger.filter_new(["a", "b"]), ["a", "b"])
        self.assertEqual(ledger.filter_new(["a", "c"]), ["c"])
        self.assertEqual(ledger.submitted_count, 3)
        ledger.forget("a")
        self.assertEqual(ledger.filter_new(["a"]), ["a"])


if __name__ == "__main__":
    unittest.main()
