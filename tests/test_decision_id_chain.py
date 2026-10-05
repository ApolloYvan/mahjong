"""B1 决策关联链契约测试：decision_id 必须在调用 api.action() 之前创建，
并在成功/超时/409/fallback/hu 各路径下与最终 log.action(...) 共享同一个 ID
（不能在 API 成功后才生成——历史 bug：旧版 decision_id 生成时机在 API
调用成功之后，导致 action_rejected/fallback_sent 记录不到关联 ID）。

P0-1 返修：新增对 decision_attempt/decision_attempt_outcome 两段式记录的
契约测试——调用 api.action() 之前必须先有一条 outcome='pending' 的
attempt 记录，之后无论走哪个分支都必须用同一个 decision_id 推进到明确的
outcome，尤其是 TimeoutError 路径（旧版明确放弃了这一点，本次禁止再放弃）。
"""
import unittest
from unittest.mock import MagicMock, Mock, patch

from mj.api import ApiError
from mj.bot import play_game


class DecisionIdConsistencyTests(unittest.TestCase):
    def _api(self, states):
        api = MagicMock()
        api.rules.return_value = {}
        api.notify.return_value = MagicMock()
        api.notify.return_value.__iter__ = Mock(side_effect=TypeError("not iterable"))
        api.state.side_effect = states
        return api

    def test_hu_path_shares_decision_id_between_hu_detail_and_action_log(self):
        snapshot = {
            "phase": "draw", "seat": 0, "turn": 0, "round_no": 1,
            "my_hand": ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w",
                        "1t", "1t", "1t", "西", "西"],
            "drawn_tile": "西", "melds": [[], [], [], []], "wall_remaining": 50,
        }
        api = self._api([
            {"snapshot": snapshot},
            {"finished": True, "snapshot": {"phase": "finished", "seat": 0}},
        ])
        log = Mock()
        play_game(api, "g", log, rules={})
        # log.append("hu_detail", {...}) 与 log.action(...) 里的 decision_id
        # 必须是同一个值。
        hu_detail_call = [c for c in log.append.call_args_list if c.args[0] == "hu_detail"]
        self.assertEqual(len(hu_detail_call), 1)
        hu_decision_id = hu_detail_call[0].args[1]["decision_id"]
        action_call = log.action.call_args
        action_decision_id = action_call.kwargs.get("decision_id")
        self.assertIsNotNone(hu_decision_id)
        self.assertEqual(hu_decision_id, action_decision_id)
        # P0-1: decision_attempt 必须先于 api.action() 记录，且 outcome="hu"
        # 用同一个 decision_id 推进。
        attempt_call = log.decision_attempt.call_args
        self.assertEqual(attempt_call.kwargs.get("decision_id"), hu_decision_id)
        outcome_call = log.decision_attempt_outcome.call_args
        self.assertEqual(outcome_call.kwargs.get("decision_id"), hu_decision_id)
        self.assertEqual(outcome_call.kwargs.get("outcome"), "hu")

    def test_409_rejected_then_fallback_shares_same_decision_id(self):
        """独立复核 P0-3 返修后的新语义：fallback 决策不再在触发它的那次
        409 异常处理里就地对旧 snapshot 发起（避免规则6禁止的"基于陈旧
        snapshot 重复策略动作"）。fallback 是下一次循环迭代基于全新权威
        snapshot 做出的**新决策**，拥有自己独立的 decision_id；若这次
        fallback 尝试本身又被 409，则该次 iteration 的 action_rejected 与
        fallback_sent 共享同一个（这次 iteration 的）decision_id——两者
        都属于同一次 api.action() 调用的结果，不是跨 iteration 共享。"""
        snapshot = {
            "phase": "draw", "seat": 0, "turn": 0, "round_no": 1,
            "my_hand": ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w",
                        "1t", "2t", "3t", "南"],
            "drawn_tile": "西", "melds": [[], [], [], []], "wall_remaining": 40,
        }
        api = self._api([{"snapshot": snapshot} for _ in range(3)] +
                        [{"finished": True, "snapshot": {"phase": "finished", "seat": 0}}])
        api.action.side_effect = ApiError(409, "INVALID_ACTION")
        log = Mock()
        with patch("mj.bot.choose_discard", return_value={"action": "discard", "tile": "1w"}):
            play_game(api, "g", log, rules={})
        # 3 次迭代消费 3 份相同的 snapshot：前两次是正常 choose_discard
        # 决策（rejections 0->1->2），第三次达到 fallback 门禁
        # （rejections>=2 且 fallback_attempts<2）并尝试 fallback，同样被
        # 409 拒绝。
        rejected_ids = [c.kwargs.get("decision_id") for c in log.action_rejected.call_args_list]
        fallback_ids = [c.kwargs.get("decision_id") for c in log.fallback_sent.call_args_list]
        self.assertEqual(len(rejected_ids), 3)
        self.assertEqual(len(fallback_ids), 1)
        self.assertTrue(all(rid is not None for rid in rejected_ids))
        self.assertTrue(all(fid is not None for fid in fallback_ids))
        # 三次 rejected 的 decision_id 各不相同（每次迭代都是一次全新决策，
        # 不是同一个决策反复重试）。
        self.assertEqual(len(set(rejected_ids)), 3)
        # fallback 尝试发生在第三次迭代，与该次的 action_rejected 共享
        # 同一个 decision_id（同一次 api.action() 调用的两条日志）。
        self.assertEqual(fallback_ids[0], rejected_ids[2])
        # P0-1: 每一次 409 都必须用同一个 decision_id 推进 attempt outcome
        # 为 "conflict_409"；fallback 尝试也必须用同一个 decision_id 推进
        # 为 "fallback_sent"（与其 action_rejected 相同的 decision_id）。
        outcome_calls = log.decision_attempt_outcome.call_args_list
        conflict_outcomes = [c for c in outcome_calls if c.kwargs.get("outcome") == "conflict_409"]
        self.assertEqual(len(conflict_outcomes), 3)
        for c, rid in zip(conflict_outcomes, rejected_ids):
            self.assertEqual(c.kwargs.get("decision_id"), rid)
        fallback_outcomes = [c for c in outcome_calls if c.kwargs.get("outcome") == "fallback_sent"]
        self.assertEqual(len(fallback_outcomes), 1)
        self.assertEqual(fallback_outcomes[0].kwargs.get("decision_id"), fallback_ids[0])

    def test_409_response_rejected_still_has_decision_id(self):
        snapshot = {
            "phase": "response_peng", "seat": 1, "responding_seats": [1], "round_no": 1,
            "my_hand": [], "window_tile": "5w", "melds": [[], [], [], []],
            "wall_remaining": 40,
        }
        api = self._api([{"snapshot": snapshot} for _ in range(3)] +
                        [{"finished": True, "snapshot": {"phase": "finished", "seat": 1}}])
        api.action.side_effect = ApiError(409, "INVALID_ACTION")
        log = Mock()
        with patch("mj.bot.choose_action", return_value={"action": "peng", "tile": "5w"}):
            play_game(api, "g", log, rules={})
        rejected_ids = [c.kwargs.get("decision_id") for c in log.action_rejected.call_args_list]
        self.assertTrue(all(rid is not None for rid in rejected_ids))
        log.fallback_sent.assert_not_called()

    def test_timeout_error_preserves_decision_id_chain_and_reconstructable_attempt(self):
        """P0-1 硬性要求：TimeoutError 路径必须与其它分支一样，用同一个
        decision_id 关联 decision_attempt（记录了尝试的 action/tile）和
        decision_attempt_outcome（outcome='timeout'）——禁止"超时不关联
        decision_id"作为既有行为被保留。"""
        snapshot = {
            "phase": "draw", "seat": 0, "turn": 0, "round_no": 1,
            "my_hand": ["1w"] * 13 + ["2w"], "drawn_tile": "2w",
            "melds": [[], [], [], []], "wall_remaining": 40,
        }
        api = self._api([{"snapshot": snapshot},
                        {"finished": True, "snapshot": {"phase": "finished", "seat": 0}}])
        api.action.side_effect = TimeoutError()
        log = Mock()
        with patch("mj.bot.choose_discard", return_value={"action": "discard", "tile": "1w"}):
            play_game(api, "g", log, rules={})
        log.error.assert_called()

        # decision_attempt 必须先于 api.action() 被调用，且包含尝试的
        # action/tile 及 policy_version/schema_version/config_hash/
        # build_hash 等字段。
        self.assertEqual(log.decision_attempt.call_count, 1)
        attempt_call = log.decision_attempt.call_args
        attempted_decision_id = attempt_call.kwargs.get("decision_id")
        self.assertIsNotNone(attempted_decision_id)
        self.assertEqual(attempt_call.kwargs.get("action"), "discard")
        self.assertEqual(attempt_call.kwargs.get("tile"), "1w")
        self.assertIsNotNone(attempt_call.kwargs.get("policy_version"))
        self.assertIsNotNone(attempt_call.kwargs.get("schema_version"))
        self.assertIsNotNone(attempt_call.kwargs.get("config_hash"))
        self.assertIsNotNone(attempt_call.kwargs.get("build_hash"))

        # decision_attempt_outcome 必须用同一个 decision_id，outcome="timeout"。
        self.assertEqual(log.decision_attempt_outcome.call_count, 1)
        outcome_call = log.decision_attempt_outcome.call_args
        self.assertEqual(outcome_call.kwargs.get("decision_id"), attempted_decision_id)
        self.assertEqual(outcome_call.kwargs.get("outcome"), "timeout")


if __name__ == "__main__":
    unittest.main()
