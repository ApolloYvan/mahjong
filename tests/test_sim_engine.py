"""G1（docs/SIM_FOUNDATION_REPORT.md）：mj.sim.engine 的基础状态机测试。

范围说明：这些测试只验证"引擎自己发牌、自己推进"这条路径里最基础的机制
（发牌张数、摸打轮转、非法动作检测、抓打圈触发、墙尾禁杠、流局阈值），
不是 G1 真正的验收证据——真正的验收是 tools/sim_replay_check.py 对真实
日志的强制回放（不一致数=0），这个工具本身没有执行环境验证（bot 一直在
跑局，见交付报告）。这里的单元测试只用手工摆好的、互不冲突的手牌，跟
mj.rules 已经在别处测过的判胡逻辑正交，避免重复造一遍"这副牌到底算不算
胡"的判断。

``_no_claim_hands()`` 刻意不用任何字牌/财神——4 家全部由数牌组成，7 种
字牌 + 财神全部留在牌堆里不发给任何人，需要"摸一张、打出去、保证没人能
碰/吃"的测试就摸字牌（"北"/"中"/"东"/"南"/"西"），天然安全：没有人手里
有字牌，碰不可能成立；字牌也不可能被吃（吃只对同花色数牌成立，见
``mj.sim.engine._chi_options`` 对 ``idx>=27`` 的处理）。涉及碰/吃/杠的测试
另外在这个基础手牌上做局部覆盖，并在注释里说明为什么不会有意外的额外
响应者。
"""
import os
import unittest

os.environ.setdefault("MJ_WEIGHTS_NO_FILE", "1")

from mj.rules import evaluate  # noqa: E402
from mj.sim.engine import (GANG_FORBIDDEN_REMAINING, WALL_EXHAUST_REMAINING,  # noqa: E402
                           IllegalActionError, RoundEngine, build_wall)
from mj.tiles import JOKER, TILE_INDEX, to_counts  # noqa: E402


def _fresh_engine(dealer=0):
    return RoundEngine(build_wall(seed=1), dealer=dealer, round_no=1)


def _pass_through_response_windows(engine):
    """服务端总是无条件开碰窗口（问全部非打牌方）、再开吃窗口（问下家），
    不看资格够不够——见 mj.sim.engine 模块 docstring 的实证。测试用的
    "没人能响应"手牌摆好之后，仍然要真的把这些窗口一一 pass 掉，引擎才会
    推进到下一位的摸牌，不是弃牌一落地就自动跳过。"""
    while engine.phase == "response":
        engine.apply_pass(engine.responding_seats[0])


def _no_claim_hands():
    return [
        ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w", "1b", "2b", "3b", "4b"],
        ["5b", "6b", "7b", "8b", "9b", "1t", "2t", "3t", "4t", "5t", "6t", "7t", "8t"],
        ["9t", "1t", "2t", "3t", "4t", "5t", "6t", "7t", "8t", "1w", "2w", "3w", "4w"],
        ["5w", "6w", "7w", "8w", "9w", "1b", "2b", "3b", "4b", "5b", "6b", "7b", "8b"],
    ]


class DealTests(unittest.TestCase):
    def test_deal_gives_thirteen_each_and_dealer_starts(self):
        engine = _fresh_engine()
        for seat in range(4):
            self.assertEqual(len(engine.hands[seat]), 13)
        self.assertEqual(engine.turn, 0)
        self.assertEqual(engine.phase, "draw")
        self.assertEqual(engine.wall_remaining(), 136 - 13 * 4)

    def test_clone_is_independent(self):
        engine = _fresh_engine()
        clone = engine.clone()
        clone.hands[0] = clone.hands[0][:-1]
        self.assertNotEqual(len(engine.hands[0]), len(clone.hands[0]))


class TurnFlowTests(unittest.TestCase):
    def test_draw_then_uncontested_discard_advances_turn(self):
        engine = _fresh_engine()
        engine.hands = _no_claim_hands()
        tile = engine.step_draw(forced_tile="北")   # 没人手里有字牌，摸完打出去不会被任何人碰/吃
        self.assertEqual(tile, "北")
        self.assertEqual(engine.wall_remaining(), 136 - 13 * 4 - 1)
        engine.apply_discard(0, "北")
        _pass_through_response_windows(engine)
        self.assertEqual(engine.phase, "draw")
        self.assertEqual(engine.turn, 1)

    def test_discard_tile_not_in_hand_is_illegal(self):
        engine = _fresh_engine()
        engine.hands = _no_claim_hands()
        engine.step_draw(forced_tile="北")
        with self.assertRaises(IllegalActionError):
            engine.apply_discard(0, "中")   # 座位0手里没有"中"

    def test_discard_out_of_turn_is_illegal(self):
        engine = _fresh_engine()
        engine.hands = _no_claim_hands()
        engine.step_draw(forced_tile="北")
        with self.assertRaises(IllegalActionError):
            engine.apply_discard(1, engine.hands[1][0])


class ClaimTests(unittest.TestCase):
    def test_peng_updates_hand_and_meld_and_turn(self):
        engine = _fresh_engine()
        engine.hands = _no_claim_hands()
        # 座位1手里塞两张 5b；其余座位里座位3原本就有 1 张 5b（不够碰），
        # 不影响"座位1能碰"这个结论。
        engine.hands[1] = ["5b", "5b"] + engine.hands[1][2:]
        engine.step_draw(forced_tile="5b")   # 座位0手里没有 5b
        engine.apply_discard(0, "5b")
        self.assertEqual(engine.phase, "response")
        self.assertIn(1, engine.responding_seats)
        engine.apply_claim(1, "peng", None)
        self.assertEqual(engine.melds[1], [{"kind": "peng", "tiles": ["5b", "5b", "5b"]}])
        self.assertEqual(engine.hands[1].count("5b"), 0)
        self.assertEqual(engine.turn, 1)
        self.assertEqual(engine.phase, "draw")
        self.assertIsNone(engine.drawn_tile)

    def test_joker_cannot_be_claimed(self):
        engine = _fresh_engine()
        engine.hands = _no_claim_hands()
        engine.hands[0] = engine.hands[0][:-1] + [JOKER]
        engine.hands[1] = [JOKER] + engine.hands[1][1:]   # 座位1也持有财神，看会不会被误判成"能碰"
        engine.step_draw(forced_tile=JOKER)
        engine.apply_discard(0, JOKER)
        # 财神打出直接跳过响应窗口，不给任何人碰/吃的机会。
        self.assertEqual(engine.phase, "draw")
        self.assertEqual(engine.turn, 1)

    def test_chi_only_next_seat(self):
        engine = _fresh_engine()
        engine.hands = _no_claim_hands()
        # 座位2手里塞 4w5w，能配合吃 6w，但座位2不是座位0的下家（下家是
        # 座位1，座位1手里没有任何 w 花色的牌，也没有 6w，碰不了）——
        # 座位3原本就有 1 张 6w，不够碰。座位0摸到 6w 后打出去，正确结果是
        # 没有任何人能响应（既不能碰也不能吃）。
        engine.hands[2] = ["4w", "5w"] + engine.hands[2][2:]
        engine.step_draw(forced_tile="6w")
        engine.apply_discard(0, "6w")
        # 碰窗口仍然会打开问全部非打牌方（不看资格），只是没人真的能碰/吃。
        self.assertEqual(sorted(engine.responding_seats), [1, 2, 3])
        _pass_through_response_windows(engine)
        self.assertEqual(engine.phase, "draw")
        self.assertEqual(engine.turn, 1)


class CatchPlayTests(unittest.TestCase):
    def test_joker_discard_triggers_catch_play_and_restricts_next_discards(self):
        engine = _fresh_engine()
        engine.hands = _no_claim_hands()
        engine.hands[0] = engine.hands[0][:-1] + [JOKER]
        engine.step_draw(forced_tile=JOKER)
        engine.apply_discard(0, JOKER)
        self.assertTrue(engine.catch_play)
        self.assertEqual(engine.god_discarder_seat, 0)
        drawn = engine.step_draw(forced_tile="东")   # 没人手里有字牌
        with self.assertRaises(IllegalActionError):
            engine.apply_discard(1, engine.hands[1][0])   # 不是刚摸到的那张
        engine.apply_discard(1, drawn)   # 只能打刚摸到的这张，这次应该成功

    def test_catch_play_clears_when_god_discarder_returns_without_repiao(self):
        # 解除条件是"轮回到当前豁免者（god_discarder_seat）自己、且这次不是
        # 再飘"，不是"随便数满 3 次别家弃牌"——见 mj.sim.engine 模块
        # docstring 的实证（这条曾经的错误假设已经被真实日志证伪过一次）。
        # 所以这里要让 seat0 真的轮到自己再弃一次牌，不能只走完 1/2/3 就完事。
        engine = _fresh_engine()
        engine.hands = _no_claim_hands()
        engine.hands[0] = engine.hands[0][:-1] + [JOKER]
        engine.step_draw(forced_tile=JOKER)
        engine.apply_discard(0, JOKER)
        for seat, filler in zip((1, 2, 3), ("东", "南", "西")):
            drawn = engine.step_draw(forced_tile=filler)
            engine.apply_discard(seat, drawn)
            _pass_through_response_windows(engine)
        self.assertTrue(engine.catch_play)   # 还没轮回到 seat0 自己，豁免应该还在
        drawn = engine.step_draw(forced_tile="北")
        engine.apply_discard(0, drawn)   # seat0 自己的回合，不是再飘 -> 解除
        self.assertFalse(engine.catch_play)


class GangTests(unittest.TestCase):
    def test_gang_forbidden_threshold_matches_verified_conversion(self):
        # 见 mj/sim/engine.py 模块 docstring 的 3c 实证：引擎 wall_remaining
        # 恒等于服务端 wall_remaining + 1（37 万条真实决策快照核对出来的
        # 全局固定偏移）。服务端原始规则是 "wall_remaining<=20 禁杠"，换算
        # 成引擎口径是 "<=21 禁杠"——之前这里直接抄服务端数字 20，没做
        # 换算，是真实 bug。tools/gang_threshold_check.py 对全量 3298 个
        # 文件 5769 次真实杠事件实测，发生杠时引擎 wall_remaining 最小值是
        # 24，全部 >=22，支持这条换算（不是证伪）。
        self.assertEqual(GANG_FORBIDDEN_REMAINING, 21)

    def test_an_gang_forbidden_near_wall_tail(self):
        # GANG_FORBIDDEN_REMAINING 现在和 WALL_EXHAUST_REMAINING 恰好都是
        # 21（服务端换算后的巧合，不是必然相等）——drawn_count 要按"摸这张
        # 之前 wall 还没到流局阈值、摸完之后正好落进禁杠区"来摆，不能直接用
        # GANG_FORBIDDEN_REMAINING 当摸牌前的 wall，否则 step_draw 会先判
        # 流局，摸不到这张牌。
        engine = _fresh_engine()
        engine.hands = _no_claim_hands()
        engine.hands[0] = ["7b", "7b", "7b"] + engine.hands[0][3:]
        engine.drawn_count = 136 - 13 * 4 - GANG_FORBIDDEN_REMAINING - 1   # 摸前 wall=22，摸完落到 21
        engine.step_draw(forced_tile="7b")
        self.assertEqual(engine.wall_remaining(), GANG_FORBIDDEN_REMAINING)
        with self.assertRaises(IllegalActionError):
            engine.apply_gang(0, "7b", "an", auto_draw=False)

    def test_an_gang_allowed_just_above_wall_tail(self):
        engine = _fresh_engine()
        engine.hands = _no_claim_hands()
        engine.hands[0] = ["7b", "7b", "7b"] + engine.hands[0][3:]
        engine.drawn_count = 136 - 13 * 4 - GANG_FORBIDDEN_REMAINING - 2   # 摸前 wall=23，摸完落到 22
        engine.step_draw(forced_tile="7b")
        self.assertEqual(engine.wall_remaining(), GANG_FORBIDDEN_REMAINING + 1)
        engine.apply_gang(0, "7b", "an", auto_draw=False)   # 不应该抛异常

    def test_joker_cannot_be_ganged(self):
        engine = _fresh_engine()
        engine.hands = _no_claim_hands()
        engine.hands[0] = [JOKER, JOKER, JOKER] + engine.hands[0][3:]
        engine.step_draw(forced_tile=JOKER)
        with self.assertRaises(IllegalActionError):
            engine.apply_gang(0, JOKER, "an", auto_draw=False)


class WallExhaustionTests(unittest.TestCase):
    def test_exhaustion_threshold_matches_verified_fact(self):
        # 见 mj/sim/engine.py 模块 docstring 的实证记录：n=6 个真实流局局，
        # 全部在 wall_remaining=21 时停摸，不是 20 或 0。
        self.assertEqual(WALL_EXHAUST_REMAINING, 21)

    def test_uncontested_discard_settles_draw_at_threshold(self):
        engine = _fresh_engine()
        engine.hands = _no_claim_hands()
        engine.drawn_count = 136 - 13 * 4 - WALL_EXHAUST_REMAINING - 1
        engine.step_draw(forced_tile="北")
        self.assertEqual(engine.wall_remaining(), WALL_EXHAUST_REMAINING)
        engine.apply_discard(0, "北")
        _pass_through_response_windows(engine)
        self.assertTrue(engine.finished)
        self.assertTrue(engine.is_draw)


class HuAndSettlementTests(unittest.TestCase):
    def test_dealer_self_draw_settlement_matches_spec(self):
        """庄家自摸 fan：胡者 +24xfan，其余每家 -8xfan——只验证结算算术，
        胡牌判定本身信 mj.rules.evaluate（已有测试覆盖）。"""
        engine = _fresh_engine(dealer=0)
        engine.hands = _no_claim_hands()
        hand13 = ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w",
                 "1b", "1b", "1b", "9t"]
        result = evaluate(to_counts(hand13), TILE_INDEX["9t"], 0, 0, 0)
        self.assertIsNotNone(result, "测试构造的手牌本身不能自摸，先修手牌")
        engine.hands[0] = list(hand13)
        engine.step_draw(forced_tile="9t")
        before = list(engine.scores)
        r = engine.apply_hu(0)
        fan = r["fan"]
        self.assertEqual(engine.scores[0], before[0] + 24 * fan)
        for s in (1, 2, 3):
            self.assertEqual(engine.scores[s], before[s] - 8 * fan)


if __name__ == "__main__":
    unittest.main()
