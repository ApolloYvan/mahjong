"""离线评测器（arena v2）重建验证：批次独立、座位/庄家均衡、吃碰响应
规则、财神响应限制、两摊吃上限、真正的镜像配对实验（同策略零 delta /
A-B 互换严格反号）、单手 seed 独立、完全可复现、小样本 CI unavailable、
CI 跨零时诚实结论、评测器能力声明诚实化，以及 weight_fit 降级为诊断
工具后空数据不宣称成功。"""
import unittest
from unittest.mock import patch

from mj.arena import (
    RULE_COVERAGE,
    SCHEDULE_PERIOD,
    SeatStrategy,
    derive_hand_seed,
    resolve_claim,
    run_arena,
    run_batch,
    run_paired_batch,
    validate_schedule,
)
from mj.tiles import JOKER
from mj.weight_fit import evaluate_configs


class BatchIndependenceTests(unittest.TestCase):
    def test_run_batch_is_pure_and_order_independent(self):
        """需求一：run_batch() 只依赖显式参数（root_seed/batch_id/...），
        不依赖任何跨批次的隐藏状态——同一 batch_id 单独调用一次，和在
        它之前先跑过其他 batch_id 之后再调用一次，结果必须逐字段一致。"""
        first = run_batch(123, 3, "base", "all", None, "base", "all", None, 2)
        # 先跑若干个其他 batch，制造"曾经存在跨批状态"的干扰。
        for other_batch_id in (0, 1, 2, 4, 5):
            run_batch(123, other_batch_id, "base", "all", None, "base", "all", None, 2)
        second = run_batch(123, 3, "base", "all", None, "base", "all", None, 2)
        self.assertEqual(first, second)


class DealerAndSeatBalanceTests(unittest.TestCase):
    def test_validate_schedule_passes_for_one_period_and_reports_balance(self):
        """需求一.4 + 需求二：一个 SCHEDULE_PERIOD 内初庄按 0/1/2/3
        均衡轮换，且 A/B 座位次数、初庄次数都相等；validate_schedule()
        对合法配置必须返回 ok，不抛异常。"""
        result = validate_schedule(batches=SCHEDULE_PERIOD, hands_per_batch=1,
                                    root_seed=1, policy_a="base", policy_b="base")
        self.assertTrue(result["ok"])
        self.assertEqual(result["batches"], SCHEDULE_PERIOD)

    def test_validate_schedule_rejects_non_multiple_batches(self):
        """batches 不是 SCHEDULE_PERIOD 的整数倍时，座位/庄家均衡无法在
        批次边界处收口，必须显式抛出 ScheduleValidationError。"""
        from mj.arena import ScheduleValidationError
        with self.assertRaises(ScheduleValidationError):
            validate_schedule(batches=3, hands_per_batch=1, root_seed=1,
                               policy_a="base", policy_b="base")


class ChiPengResponseRuleTests(unittest.TestCase):
    """问题一：吃碰响应规则——peng 优先于 chi；只有出牌者下家能 chi；
    响应者收到的 meld_groups 是自己的，不是出牌者的。三个断言合并进一个
    测试类以贴合"新增测试不超过 6 项"的用例清单（用例 1/2/3）。"""

    def test_only_next_seat_can_chi(self):
        """对家（offset=2，非下家）持有可吃组合，也绝不会被检查/允许吃；
        只有下家（offset=1）在无人 peng 时可以吃（同一测试合并断言两种
        座位，贴合"新增测试不超过 6 项"的用例清单——用例 1）。"""
        discard = "5w"
        # seat2(对家) 与 seat1(下家) 都持有可组成 4w+5w+6w 的搭子。
        hands_opposite_holds = [[], ["9b"], ["4w", "6w"], ["9t"]]
        melds = [[], [], [], []]
        strategies = [
            None,
            SeatStrategy("base", claims="all"),
            SeatStrategy("base", claims="all"),
            SeatStrategy("base", claims="all"),
        ]
        claimed, kind, tiles = resolve_claim(0, discard, hands_opposite_holds, melds, strategies, 50)
        self.assertIsNone(claimed)
        self.assertIsNone(kind)
        self.assertIsNone(tiles)

        hands_next_holds = [[], ["4w", "6w"], ["9b"], ["9t"]]
        claimed, kind, tiles = resolve_claim(0, discard, hands_next_holds, melds, strategies, 50)
        self.assertEqual(claimed, 1)
        self.assertEqual(kind, "chi")
        self.assertEqual(sorted(tiles), ["4w", "6w"])

    def test_peng_beats_chi_when_both_available(self):
        """下家可吃、上家（offset=3）可碰同时成立时，peng 优先，响应竞争
        直接结束，chi 完全不会被采用。"""
        discard = "5w"
        hands = [[], ["4w", "6w"], ["9b"], ["5w", "5w"]]
        melds = [[], [], [], []]
        strategies = [
            None,
            SeatStrategy("base", claims="all"),
            SeatStrategy("base", claims="all"),
            SeatStrategy("base", claims="all"),
        ]
        claimed, kind, tiles = resolve_claim(0, discard, hands, melds, strategies, 50)
        self.assertEqual(claimed, 3)
        self.assertEqual(kind, "peng")
        self.assertEqual(sorted(tiles), ["5w", "5w"])

    def test_responder_receives_own_meld_groups_not_discarders(self):
        """响应者（下家）调用 chi_take 时收到的 meld_groups 必须是
        len(melds[responder])，不是出牌者的 len(melds[discarder])。"""

        class RecordingStrategy:
            def __init__(self):
                self.chi_meld_groups = "not_called"

            def peng_take(self, hand, tile, meld_groups=0, melds_all=None, seat=0, wall=50):
                return None

            def chi_take(self, hand, tile, meld_groups=0, melds_all=None, seat=0, wall=50):
                self.chi_meld_groups = meld_groups
                return None

        discard = "5w"
        hands = [[], ["4w", "6w"], [], []]
        # 出牌者(座位0) meld_groups=1，下家(座位1) meld_groups=3——两者
        # 故意设为不同的值，断言响应者收到 3 而不是 1。
        melds = [
            [{"kind": "chi", "tiles": ["1w", "2w", "3w"]}],
            [{"kind": "peng", "tiles": ["9b", "9b", "9b"]}] * 3,
            [],
            [],
        ]
        recorder = RecordingStrategy()
        strategies = [None, recorder, SeatStrategy("base", claims="none"),
                      SeatStrategy("base", claims="none")]
        claimed, kind, tiles = resolve_claim(0, discard, hands, melds, strategies, 50)
        self.assertIsNone(claimed)  # RecordingStrategy 不声明吃碰，无人响应
        self.assertEqual(recorder.chi_meld_groups, 3)


class JokerClaimRestrictionTests(unittest.TestCase):
    def test_joker_never_pengd_or_chid_under_any_claims_mode(self):
        """用例1：claims=all/ev/loose/real 下财神都不能被碰；real 模式下
        即使把生产响应函数（mj.responses.choose_peng/choose_chi）打补丁
        成"总是同意"，SeatStrategy 仍必须在调用它们之前就拦截财神——
        证明限制不依赖 choose_peng/choose_chi 内部的 "白" 门控，不会被
        绕过。吃牌本身不适用于字牌（无顺子概念），此处一并确认 chi_take
        对财神也返回 None。"""
        for claims in ("all", "ev", "loose", "real"):
            strategy = SeatStrategy("base", claims=claims)
            hand = [JOKER, JOKER, "1w"]
            self.assertIsNone(
                strategy.peng_take(hand, JOKER, 0),
                f"claims={claims} 下财神仍可被碰",
            )
            self.assertIsNone(
                strategy.chi_take(["1w", "2w"], JOKER, 0),
                f"claims={claims} 下财神仍可被吃",
            )

        # real 模式：把生产响应函数打补丁成"总是同意"，确认限制发生在
        # SeatStrategy 内部拦截点，而不是依赖 choose_peng/choose_chi 内部
        # 的 "白" 门控——真正杜绝"绕过规则的生产响应函数"路径。
        real_strategy = SeatStrategy("base", claims="real")
        with patch("mj.responses.choose_peng", return_value={"action": "peng", "tile": JOKER}), \
             patch("mj.responses.choose_chi", return_value={"action": "chi", "tile": JOKER, "tiles": ["1w", "2w"]}):
            self.assertIsNone(real_strategy.peng_take([JOKER, JOKER, "1w"], JOKER, 0, [], 0, 50))
            self.assertIsNone(real_strategy.chi_take(["1w", "2w"], JOKER, 0, [], 0, 50))


class MaxTwoChiLimitTests(unittest.TestCase):
    def test_two_existing_chi_blocks_third_chi_but_allows_peng(self):
        """用例2+3：响应者已有两组 kind=='chi' 时，即使持有合法搭子，
        resolve_claim() 也绝不能让其第三次吃；但同一限制不误伤 peng——
        已有两摊吃、总副露<4 时，合法普通牌仍可正常碰。"""
        melds_two_chi = [[], [
            {"kind": "chi", "tiles": ["1w", "2w", "3w"]},
            {"kind": "chi", "tiles": ["1b", "2b", "3b"]},
        ], [], []]
        strategies = [None, SeatStrategy("base", claims="all"),
                      SeatStrategy("base", claims="all"), SeatStrategy("base", claims="all")]

        # 第三次吃：下家持有合法搭子 4w+6w，也不得被采用。
        hands_chi_attempt = [[], ["4w", "6w"], [], []]
        claimed, kind, tiles = resolve_claim(0, "5w", hands_chi_attempt, melds_two_chi, strategies, 50)
        self.assertIsNone(claimed)
        self.assertIsNone(kind)
        self.assertIsNone(tiles)

        # 同一响应者已有两摊吃、总副露数(2)<4 时，合法碰普通牌仍应放行。
        hands_peng_attempt = [[], ["9b", "9b"], [], []]
        claimed, kind, tiles = resolve_claim(0, "9b", hands_peng_attempt, melds_two_chi, strategies, 50)
        self.assertEqual(claimed, 1)
        self.assertEqual(kind, "peng")
        self.assertEqual(sorted(tiles), ["9b", "9b"])

    def test_second_chi_still_allowed_with_one_existing_chi(self):
        """用例4：响应者只有一组已有 chi 时，合法的第二次吃仍应正常发生
        （确认两摊上限不会误伤第二次合法吃）。"""
        melds_one_chi = [[], [{"kind": "chi", "tiles": ["1w", "2w", "3w"]}], [], []]
        hands = [[], ["4w", "6w"], [], []]
        strategies = [None, SeatStrategy("base", claims="all"),
                      SeatStrategy("base", claims="all"), SeatStrategy("base", claims="all")]
        claimed, kind, tiles = resolve_claim(0, "5w", hands, melds_one_chi, strategies, 50)
        self.assertEqual(claimed, 1)
        self.assertEqual(kind, "chi")
        self.assertEqual(sorted(tiles), ["4w", "6w"])

    def test_malformed_meld_kind_is_treated_conservatively_without_crashing(self):
        """B.4：对缺失/异常 kind 的历史 fixture 做保守处理——非 dict 条目、
        缺失 "kind" 字段的条目都不计入 chi 计数，也不能让 resolve_claim()
        崩溃。"""
        from mj.arena import _chi_count
        self.assertEqual(_chi_count(None), 0)
        self.assertEqual(_chi_count("not_a_list"), 0)
        self.assertEqual(_chi_count([{"tiles": ["1w"]}, "garbage", None, {"kind": "chi", "tiles": ["1w", "2w", "3w"]}]), 1)

        malformed_melds = [[], [{"tiles": ["1w", "2w", "3w"]}, "garbage"], [], []]
        strategies = [None, SeatStrategy("base", claims="all"),
                      SeatStrategy("base", claims="all"), SeatStrategy("base", claims="all")]
        hands = [[], ["4w", "6w"], [], []]
        claimed, kind, tiles = resolve_claim(0, "5w", hands, malformed_melds, strategies, 50)
        self.assertEqual((claimed, kind, tiles), (1, "chi", ["4w", "6w"]))


class EvaluatorCapabilityDisclosureTests(unittest.TestCase):
    def test_run_arena_report_declares_partial_rule_coverage(self):
        """用例5：run_arena 报告必须包含准确的能力边界声明——
        simulation_scope="partial_rules"、official_fidelity=False、
        rule_coverage 逐项列出且已实现项为 True/未实现项为 False、
        conclusion_scope 明确结论只适用于已模拟规则。"""
        report = run_arena("base", "base", hands_per_batch=1, batches=SCHEDULE_PERIOD,
                            root_seed=3, claims_a="all", claims_b="all", validate=False)
        self.assertEqual(report["simulation_scope"], "partial_rules")
        self.assertFalse(report["official_fidelity"])
        expected_coverage = {
            "self_draw": True, "chi_peng_priority": True,
            "joker_claim_restriction": True, "max_two_chi": True,
            "gang": False, "catch_play_circle": False, "chain_piao": "partial",
        }
        self.assertEqual(report["rule_coverage"], expected_coverage)
        self.assertEqual(RULE_COVERAGE, expected_coverage)
        self.assertIn("conclusion_scope", report)
        self.assertIsInstance(report["conclusion_scope"], str)
        self.assertTrue(len(report["conclusion_scope"]) > 0)
        self.assertIn("conclusion", report)


class MirrorPairingTests(unittest.TestCase):
    """问题二：真正的镜像配对实验——同策略自对战 delta 严格为 0；
    交换 A/B 后逐批 delta 与聚合 score_delta 严格等幅反号（用例 6）。"""

    def test_identical_policies_produce_exact_zero_paired_delta(self):
        """同一策略对战（kind_a==kind_b, claims 相同）时，session_forward
        与 session_mirror 共享同一牌墙 seed 序列，唯一差异只是"策略贴在
        哪两个物理座位"——paired_delta 必须严格等于 0（不是"接近 0"）。"""
        for batch_id in (0, 1, 5, 9):
            result = run_paired_batch(
                root_seed=2026, batch_id=batch_id,
                kind_a="base", claims_a="all", weights_a=None,
                kind_b="base", claims_b="all", weights_b=None,
                hands_per_batch=3,
            )
            self.assertEqual(result["paired_delta"], 0,
                              f"batch_id={batch_id} paired_delta 应严格为 0")

    def test_swapping_a_b_produces_exact_negated_deltas(self):
        """交换 policy_a/policy_b（claims=all vs claims=none 的强弱差异）
        后：batch_score_delta 逐项严格等幅反号，聚合 score_delta_mean
        也严格等幅反号——而不仅仅是符号相反。"""
        ab = run_arena("base", "base", hands_per_batch=1, batches=SCHEDULE_PERIOD,
                        root_seed=7, claims_a="all", claims_b="none", validate=False)
        ba = run_arena("base", "base", hands_per_batch=1, batches=SCHEDULE_PERIOD,
                        root_seed=7, claims_a="none", claims_b="all", validate=False)
        self.assertEqual(len(ab["batch_score_delta"]), len(ba["batch_score_delta"]))
        for i, (d_ab, d_ba) in enumerate(zip(ab["batch_score_delta"], ba["batch_score_delta"])):
            self.assertEqual(d_ab, -d_ba, f"paired batch {i} 未严格等幅反号: {d_ab} vs {d_ba}")
        self.assertEqual(ab["score_delta_mean"], -ba["score_delta_mean"])
        # 至少存在非零差异，证明这不是"两边都恰好是 0"的退化场景。
        self.assertTrue(any(d != 0 for d in ab["batch_score_delta"]))


class HonestConclusionTests(unittest.TestCase):
    def test_ci_straddling_zero_yields_inconclusive_conclusion(self):
        """问题四/用例 6：CI 跨 0（下界<=0<=上界）时 conclusion 必须是
        inconclusive，绝不能仅凭 score_delta 的符号宣称某策略稳定胜出。"""
        from mj.arena import _conclusion_from_ci, _mean_ci95
        ci = _mean_ci95([10, -10, 8, -6])  # 均值不为 0 但方差大，CI 跨 0
        self.assertEqual(ci["ci95"]["status"], "ok")
        self.assertLessEqual(ci["ci95"]["low"], 0)
        self.assertGreaterEqual(ci["ci95"]["high"], 0)
        self.assertEqual(_conclusion_from_ci(ci), "inconclusive")
        # unavailable（样本不足）必须映射为 insufficient_samples，不是 inconclusive。
        ci_unavailable = _mean_ci95([5])
        self.assertEqual(_conclusion_from_ci(ci_unavailable), "insufficient_samples")


class SingleHandSeedIndependenceTests(unittest.TestCase):
    def test_hand_seed_unaffected_by_previous_hand_rng_consumption(self):
        """需求三.2：derive_hand_seed() 是纯函数，只依赖
        (root_seed, batch_id, hand_id, schedule_id)——某一手随机数被消耗
        多少次都不会影响下一手（hand_id+1）的派生 seed。"""
        root_seed, batch_id, schedule_id = 99, 0, 0
        seed_before = derive_hand_seed(root_seed, batch_id, 1, schedule_id)
        noise = derive_hand_seed(root_seed, batch_id, 0, schedule_id)
        import random
        rng = random.Random(noise)
        for _ in range(500):
            rng.random()
        seed_after = derive_hand_seed(root_seed, batch_id, 1, schedule_id)
        self.assertEqual(seed_before, seed_after)


class FullReproducibilityTests(unittest.TestCase):
    def test_same_root_seed_produces_field_by_field_identical_report(self):
        """需求三.3：相同配置 + 相同 root_seed 重复运行 run_arena()，
        完整结果逐字段一致（含每批 score_delta、fan 分布、CI 等）。"""
        r1 = run_arena("base", "base", hands_per_batch=1, batches=SCHEDULE_PERIOD,
                        root_seed=42, claims_a="all", claims_b="all", validate=False)
        r2 = run_arena("base", "base", hands_per_batch=1, batches=SCHEDULE_PERIOD,
                        root_seed=42, claims_a="all", claims_b="all", validate=False)
        self.assertEqual(r1, r2)


class SmallSampleCiUnavailableTests(unittest.TestCase):
    def test_single_batch_ci_reports_unavailable_with_reason(self):
        """需求四：样本不足（仅 1 个独立 batch，无法估计方差）时必须显式
        返回 unavailable + reason，不得输出伪精度的置信区间。"""
        report = run_arena("base", "base", hands_per_batch=1, batches=SCHEDULE_PERIOD,
                            root_seed=5, claims_a="all", claims_b="all", validate=False)
        # 人为截断成单 batch 场景，复用报告构造逻辑做最小验证。
        from mj.arena import _mean_ci95
        ci = _mean_ci95(report["batch_score_delta"][:1])
        self.assertEqual(ci["ci95"]["status"], "unavailable")
        self.assertIn("reason", ci["ci95"])
        self.assertIsNone(ci["se"])


class WeightFitDiagnosticNoClaimTests(unittest.TestCase):
    def test_empty_logs_returns_insufficient_data_not_a_winner(self):
        """需求六：无真实日志时必须返回 insufficient_data，不能用空数据
        宣称某组权重"更好"（不得返回一个看似正常的空列表报告）。"""
        result = evaluate_configs(batches_filter=["__no_such_room_prefix__"])
        self.assertEqual(result["status"], "insufficient_data")
        self.assertEqual(result["source"], "historical_replay_diagnostic")


if __name__ == "__main__":
    unittest.main()
