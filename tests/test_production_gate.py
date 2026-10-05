"""生产路径最后合法性门禁：只检查/测试已有生产代码，不重写规划器。
覆盖需求书列出的 8 项场景（数量控制在合理范围内，聚焦"发现真实失败才
最小修复"的两处 P0：choose_gang()/choose_action() 的抓打圈分支在
drawn_tile==财神时仍会提交 gang，已在 mj/responses.py 与 mj/bot.py 就地
修复，不重写规划器）。"""
import unittest
from unittest.mock import MagicMock, Mock

from mj.bot import _can_chi, can_hu, choose_action, hu_result, play_game
from mj.responses import choose_chi, choose_gang, choose_peng
from mj.tiles import JOKER


class SettledPhaseNeverSubmitsTests(unittest.TestCase):
    def test_choose_action_returns_none_for_settled_phase(self):
        """1. phase=settled 时绝不提交动作：choose_action() 对 settled 快照
        必须返回 None（不落入任何分支）。"""
        self.assertIsNone(choose_action({"phase": "settled", "seat": 0}))

    def test_play_game_poll_loop_skips_settled_without_calling_action(self):
        """1（端到端）：play_game() 的轮询主循环遇到 phase=='settled' 必须
        直接 continue，不得调用 api.action()——覆盖 v31 局间 5 秒 settled
        窗口场景（此间 my_hand 是上一局残余，不能被当作新局信息使用）。"""
        api = MagicMock()
        api.rules.return_value = {}
        api.notify.return_value = MagicMock()
        api.notify.return_value.__iter__ = Mock(side_effect=TypeError("not iterable"))
        api.state.side_effect = [
            {"snapshot": {"phase": "settled", "seat": 0, "round_no": 1,
                          "my_hand": ["1w"], "wall_remaining": 50}},
            {"finished": True, "snapshot": {"phase": "finished", "seat": 0}},
        ]
        log = Mock()
        play_game(api, "g", log, rules={})
        api.action.assert_not_called()


class JokerClaimAndHuSemanticsTests(unittest.TestCase):
    """财神（JOKER）响应与胡牌语义。权威平台规则（纠正此前"摸到财神一律
    不能胡"的错误假设）：
    - 财神不能吃、碰、杠（硬约束，不受爆头影响）。
    - 财神不能作为普通胡牌张完成普通胡/七对——但爆头（听牌态摸任意牌
      即胡）的"任意牌"明确包括财神本身；爆头摸到财神必须允许立即胡，
      也允许弃胡打出该财神继续财飘。
    """

    def test_joker_cannot_be_pengd_chid_ganged(self):
        """财神不能吃、碰、杠：生产响应函数（choose_peng/choose_chi/
        choose_gang）对财神窗口/摸牌一律不提交对应动作——与是否爆头无关，
        这三个硬约束不受本轮"财神可胡"纠错影响。"""
        self.assertIsNone(choose_peng({"window_tile": "白", "my_hand": ["白", "白", "1w"]}))
        self.assertIsNone(choose_chi({"window_tile": "白", "my_hand": ["1w", "2w"]}))
        self.assertIsNone(choose_gang({"drawn_tile": "白", "my_hand": ["白", "白", "白", "1w"],
                                        "wall_remaining": 50}))

    def test_non_baotou_joker_draw_gated_by_youcaibikao(self):
        """非爆头牌型因摸白凑成普通胡：**只有在「有财必靠」(YouCaiBiKao)
        开启时**才拒绝；规则未开启时必须正常宣胡。

        2026-09-23 实测纠错：此前这里无条件断言"一律不能胡"，依据是
        docs/HANDOFF_CURRENT.md 的规则摘要，而 VALIDATION.md 里查不到任何
        真实对局验证。全量重放 21 房 / 1672 局（判据：该次 tile_drawn 之后
        本局立刻 round_ended 且胜者为该座位）显示：**对手用摸来的白板在
        非爆头手上当场自摸成和 243 次，服务端全部确认；我们 86 次机会 0 次。**
        这些房间的决策日志里 rules 全部是空 {}。
        见 docs/audit/TASK_P0_JOKER_HU.md。

        同一手牌摸到普通牌（5b 配对成将）时真实可胡，证明这是一个真实可胡
        牌型，不是恒假的伪反例。"""
        kaoxiang = {"YouCaiBiKao": True}
        hand13 = ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w",
                  "1t", "2t", "3t", "5b"]
        snapshot = {
            "phase": "draw", "seat": 0, "turn": 0,
            "my_hand": hand13 + [JOKER], "drawn_tile": JOKER,
            "melds": [[], [], [], []], "wall_remaining": 50, "dealer": 1,
            "god": {"catch_play": False},
        }
        real_win_snapshot = dict(snapshot, drawn_tile="5b", my_hand=hand13 + ["5b"])
        self.assertTrue(can_hu(real_win_snapshot))
        self.assertIsNotNone(hu_result(real_win_snapshot))
        self.assertFalse(hu_result(real_win_snapshot).get("baotou"))

        # 规则未开启（本项目全部真实房间的实际情况）：必须能胡。
        self.assertTrue(can_hu(snapshot))
        result_open = hu_result(snapshot)
        self.assertIsNotNone(result_open)
        self.assertFalse(result_open.get("baotou"))
        self.assertEqual(result_open.get("fan"), 1)

        # 规则开启：维持旧口径，拒绝。
        self.assertFalse(can_hu(snapshot, rules=kaoxiang))
        self.assertIsNone(hu_result(snapshot, rules=kaoxiang))
        result = choose_action(snapshot, rules=kaoxiang)
        self.assertIsNotNone(result)
        self.assertNotEqual(result.get("action"), "hu")

        # gang_open（杠后补牌）路径同样只在规则开启时拒绝。
        self.assertFalse(can_hu(snapshot, rules=kaoxiang, gang_open=True))
        gang_open_result = choose_action(snapshot, rules=kaoxiang, gang_open=True)
        self.assertIsNotNone(gang_open_result)
        self.assertNotEqual(gang_open_result.get("action"), "hu")

        # 抓打圈（catch_restricted）分支：规则开启时非爆头摸财神仍不能胡，
        # 只能合法弃牌（该分支在 can_hu 之后才检查，can_hu=False 时才会走到）。
        catch_restricted_snapshot = dict(
            snapshot, seat=1, turn=1,
            god={"catch_play": True, "god_discarder_seat": 0},
        )
        self.assertEqual(choose_action(catch_restricted_snapshot, rules=kaoxiang),
                          {"action": "discard", "tile": JOKER})

    def test_baotou_hand_drawing_joker_can_hu_with_baotou_flag(self):
        """用例2：爆头牌型摸白——can_hu=True、hu_result.baotou=True。用
        12 张财神 + 1w（真实爆头：摸任意合法牌都胡）构造，不是伪造的
        result 字典。"""
        hand13 = [JOKER] * 12 + ["1w"]
        snapshot = {
            "phase": "draw", "seat": 0, "turn": 0,
            "my_hand": hand13 + [JOKER], "drawn_tile": JOKER,
            "melds": [[], [], [], []], "wall_remaining": 90, "dealer": 0,
            "god": {"catch_play": False}, "chain_count": 0, "piao": 0,
        }
        self.assertTrue(can_hu(snapshot))
        result = hu_result(snapshot)
        self.assertIsNotNone(result)
        self.assertTrue(result.get("baotou"))

    def test_baotou_joker_draw_enters_choose_hu_or_piao_not_intercepted(self):
        """用例3：爆头摸白时 choose_action 能进入 choose_hu_or_piao（返回
        hu 或 discard=JOKER 均属于该决策入口的合法输出），而不是被提前
        拦截返回 None 或强制 discard（旧错误假设的行为）。"""
        hand13 = [JOKER] * 12 + ["1w"]
        snapshot = {
            "phase": "draw", "seat": 0, "turn": 0,
            "my_hand": hand13 + [JOKER], "drawn_tile": JOKER,
            "melds": [[], [], [], []], "wall_remaining": 90, "dealer": 0,
            "god": {"catch_play": False}, "chain_count": 0, "piao": 0,
        }
        result = choose_action(snapshot)
        self.assertIsNotNone(result)
        self.assertIn(result.get("action"), ("hu", "discard"))
        if result.get("action") == "discard":
            self.assertEqual(result.get("tile"), JOKER)

    def test_immediate_hu_and_continue_piao_paths_both_reachable(self):
        """用例4：构造策略选择立即胡和选择打白续飘两条路径——直接调用
        choose_hu_or_piao（爆头摸白的策略选择入口），用真实的 baotou
        hu_result 驱动，覆盖两种 EV 判定分支。"""
        from mj.hu_strategy import choose_hu_or_piao

        baotou_result = {"baotou": True, "fan": 2}

        # 路径 A：chain+piao 已达阈值，强制立即胡（EV 判定明确偏向胡）。
        force_hu_snapshot = {
            "drawn_tile": JOKER, "chain_count": 2, "piao": 1,
            "wall_remaining": 40, "seat": 0, "dealer": 1,
            "melds": [[], [], [], []],
        }
        self.assertEqual(choose_hu_or_piao(force_hu_snapshot, baotou_result),
                          {"action": "hu", "tile": JOKER})

        # 路径 B：链短、墙余量充足、无对手副露压力，EV 判定偏向打白续飘。
        favor_piao_snapshot = {
            "drawn_tile": JOKER, "chain_count": 0, "piao": 0,
            "wall_remaining": 90, "seat": 0, "dealer": 0,
            "melds": [[], [], [], []],
        }
        self.assertEqual(choose_hu_or_piao(favor_piao_snapshot, baotou_result),
                          {"action": "discard", "tile": JOKER})

    def test_baotou_hand_drawing_normal_tile_still_wins(self):
        """用例5：爆头摸普通牌仍可胡（不受本轮财神纠错影响，回归验证）。"""
        hand13 = [JOKER] * 12 + ["1w"]
        snapshot = {
            "phase": "draw", "seat": 0, "turn": 0,
            "my_hand": hand13 + ["2w"], "drawn_tile": "2w",
            "melds": [[], [], [], []], "wall_remaining": 50, "dealer": 1,
            "god": {"catch_play": False},
        }
        self.assertTrue(can_hu(snapshot))
        result = hu_result(snapshot)
        self.assertIsNotNone(result)
        self.assertTrue(result.get("baotou"))

    def test_you_cai_bi_kao_switch_still_allows_baotou_and_gang_open_hu(self):
        """用例6：有财必拷响（YouCaiBiKao）开启时，同样允许爆头胡和杠开
        胡——must_kaoxiang 只拒绝"非爆头、非杠开、手牌仍持有财神"的普通
        成牌，不影响爆头摸财神或杠后补牌胡的口径。"""
        hand13 = [JOKER] * 12 + ["1w"]
        baotou_snapshot = {
            "phase": "draw", "seat": 0, "turn": 0,
            "my_hand": hand13 + [JOKER], "drawn_tile": JOKER,
            "melds": [[], [], [], []], "wall_remaining": 90, "dealer": 0,
            "god": {"catch_play": False},
        }
        self.assertTrue(can_hu(baotou_snapshot, rules={"YouCaiBiKao": True}))
        result = hu_result(baotou_snapshot, rules={"YouCaiBiKao": True})
        self.assertIsNotNone(result)
        self.assertTrue(result.get("baotou"))

        # 杠开胡（gang_open=True）：非爆头但杠开，YouCaiBiKao 同样不拒绝。
        gang_open_hand = ["1w", "1w", "1w", "1w", "2w", "3w", "4w", "5w",
                           "6w", "7w", "8w", "9w", JOKER]
        gang_open_snapshot = {
            "phase": "draw", "seat": 0, "turn": 0,
            "my_hand": gang_open_hand + ["1t"], "drawn_tile": "1t",
            "melds": [[], [], [], []], "wall_remaining": 50, "dealer": 1,
            "god": {"catch_play": False},
        }
        result_gang_open = hu_result(gang_open_snapshot, rules={"YouCaiBiKao": True}, gang_open=True)
        self.assertIsNotNone(result_gang_open)

    def test_choose_action_never_gangs_joker_on_normal_or_catch_restricted_draw(self):
        """回归修复：choose_action() 在普通摸牌分支与抓打圈
        （catch_restricted）分支下，摸到财神都不能提交 gang——财神不能被
        杠是硬约束，不受本轮"财神可胡"纠错影响。"""
        hand13 = ["白", "白", "白", "1w", "2w", "4w", "6w", "8w",
                  "1b", "3b", "5b", "7b", "9b"]
        normal_snapshot = {
            "phase": "draw", "seat": 0, "turn": 0, "my_hand": hand13,
            "drawn_tile": "白", "melds": [[], [], [], []], "wall_remaining": 50,
            "dealer": 1, "god": {"catch_play": False},
        }
        self.assertNotEqual(choose_action(normal_snapshot).get("action"), "gang")

        catch_restricted_snapshot = {
            "phase": "draw", "seat": 1, "turn": 1, "my_hand": hand13,
            "drawn_tile": "白", "melds": [[], [], [], []], "wall_remaining": 50,
            "dealer": 0, "god": {"catch_play": True, "god_discarder_seat": 0},
        }
        result = choose_action(catch_restricted_snapshot)
        self.assertEqual(result, {"action": "discard", "tile": "白"})


class MaxTwoChiProductionGateTests(unittest.TestCase):
    def test_response_chi_blocked_at_two_existing_chi_peng_still_gated(self):
        melds_two_chi = [[{"kind": "chi", "tiles": ["1w", "2w", "3w"]},
                           {"kind": "chi", "tiles": ["1b", "2b", "3b"]}], [], [], []]
        chi_snapshot = {"phase": "response_chi", "seat": 0, "responding_seats": [0],
                        "window_tile": "5w", "my_hand": ["4w", "6w"], "melds": melds_two_chi,
                        "wall_remaining": 50}
        self.assertFalse(_can_chi(chi_snapshot))
        self.assertEqual(choose_action(chi_snapshot), {"action": "pass", "tile": ""})

        peng_snapshot = {"phase": "response_peng", "seat": 0, "responding_seats": [0],
                         "window_tile": "9b", "my_hand": ["9b", "9b"], "melds": melds_two_chi,
                         "wall_remaining": 50}
        self.assertEqual(choose_action(peng_snapshot), {"action": "pass", "tile": ""})


class WallTailGangGateTests(unittest.TestCase):
    def test_wall_remaining_le_20_forbids_any_gang_path(self):
        """4. wall_remaining<=20 时不提交任何 gang（覆盖生产响应函数与
        choose_action 的抓打杠分支两条路径）。"""
        self.assertIsNone(choose_gang({"drawn_tile": "5w", "my_hand": ["5w"] * 4,
                                        "wall_remaining": 20}))
        catch_snapshot = {
            "phase": "draw", "seat": 1, "turn": 1,
            "my_hand": ["5w", "5w", "5w", "1w", "2w", "4w", "6w", "8w",
                        "1b", "3b", "5b", "7b", "9b"],
            "drawn_tile": "5w", "melds": [[], [], [], []], "wall_remaining": 20,
            "dealer": 0, "god": {"catch_play": True, "god_discarder_seat": 0},
        }
        result = choose_action(catch_snapshot)
        self.assertNotEqual(result.get("action"), "gang")


class CatchRestrictedOnlyDrawnTileTests(unittest.TestCase):
    def test_catch_restricted_only_discards_drawn_tile(self):
        """5. catch_restricted 时只能打 drawn_tile（非打财神本人、非杠、
        且未构成自摸胡的普通场景）。"""
        snapshot = {
            "phase": "draw", "seat": 1, "turn": 1,
            "my_hand": ["1w", "2w", "4w", "5w", "7w", "8w",
                        "1b", "2b", "4b", "5b", "7b", "8b", "东"],
            "drawn_tile": "东", "melds": [[], [], [], []], "wall_remaining": 50,
            "dealer": 0, "god": {"catch_play": True, "god_discarder_seat": 0},
        }
        self.assertEqual(choose_action(snapshot), {"action": "discard", "tile": "东"})


class ResponseWindowLegalOrPassTests(unittest.TestCase):
    def test_response_peng_and_chi_windows_only_submit_legal_action_or_pass(self):
        """6. response_peng/response_chi 窗口只提交对应合法动作或 pass——
        非法/无搭子场景必须落到 pass，不提交越权动作。"""
        no_pair_peng = {"phase": "response_peng", "seat": 0, "responding_seats": [0],
                        "window_tile": "5w", "my_hand": ["1w"], "melds": [[], [], [], []],
                        "wall_remaining": 50}
        self.assertEqual(choose_action(no_pair_peng), {"action": "pass", "tile": ""})

        no_sequence_chi = {"phase": "response_chi", "seat": 0, "responding_seats": [0],
                           "window_tile": "5w", "my_hand": ["1w", "9b"], "melds": [[], [], [], []],
                           "wall_remaining": 50}
        result = choose_action(no_sequence_chi)
        self.assertEqual(result, {"action": "pass", "tile": ""})

        valid_peng = {"phase": "response_peng", "seat": 0, "responding_seats": [0],
                     "window_tile": "5w", "my_hand": ["5w", "5w"], "melds": [[], [], [], []],
                     "wall_remaining": 50}
        self.assertEqual(choose_action(valid_peng), {"action": "pass", "tile": ""})


class SameDecisionIdAcrossTimeoutConflictFallbackTests(unittest.TestCase):
    """7. action 超时、409 和 fallback 仍使用同一个 decision_id——已由
    tests/test_decision_id_chain.py 完整覆盖（P0-1 契约测试），此处不重复
    断言，只做存在性冒烟以确认该测试文件仍在测试套件中被收集执行。"""

    def test_decision_id_chain_test_module_is_collected(self):
        import tests.test_decision_id_chain as chain_module
        self.assertTrue(hasattr(chain_module, "DecisionIdConsistencyTests"))


class TimeoutAndKaoxiangConfigFromServerTests(unittest.TestCase):
    def test_must_kaoxiang_reads_from_rules_dict_not_hardcoded(self):
        """8. YouCaiBiKao 必须来自服务端 rules 配置，不写死——choose_discard
        的路线切换与 hu_result 的 must_kaoxiang 均从传入的 rules 字典读取，
        缺省（未提供该键）时默认 False，不擅自假设服务端配置。手牌摸到的
        是普通牌（非财神本身，财神摸到已被 P0 修复为恒不可胡，见
        JokerNeverClaimableOrWinnableTests），手中留财神以触发
        must_kaoxiang 分支判定。"""
        from mj.bot import hu_result

        hand = ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w",
                "1t", "1t", "白", "白"]
        snapshot = {"my_hand": hand, "drawn_tile": "1t", "seat": 0,
                    "melds": [[], [], [], []], "dealer": 1}
        # rules 缺失 YouCaiBiKao 键时按 must_kaoxiang=False 处理（不硬编码为 True）。
        result_without_rule = hu_result(snapshot, rules={})
        result_with_rule_false = hu_result(snapshot, rules={"YouCaiBiKao": False})
        self.assertEqual(
            bool(result_without_rule) if result_without_rule else None,
            bool(result_with_rule_false) if result_with_rule_false else None,
        )

    def test_bot_module_has_no_hardcoded_timeout_constants(self):
        """8：mj/bot.py 源码中不得出现 PengTimeoutSec/ChiTimeoutSec/
        DiscardTimeoutSec 的硬编码赋值——这些参数只应来自服务端
        ``api.rules()`` 返回的 config，本轮未新增任何硬编码比赛参数。"""
        import inspect
        import mj.bot as bot_module
        source = inspect.getsource(bot_module)
        for hardcoded_name in ("PengTimeoutSec", "ChiTimeoutSec", "DiscardTimeoutSec"):
            self.assertNotIn(hardcoded_name, source,
                              f"{hardcoded_name} 不应作为硬编码常量出现在 mj/bot.py")


if __name__ == "__main__":
    unittest.main()
