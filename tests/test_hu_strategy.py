import unittest

from mj.hu_strategy import choose_hu_or_piao


class HuStrategyTests(unittest.TestCase):
    def test_baotou_white_can_piao(self):
        result = choose_hu_or_piao(
            {"drawn_tile": "白", "chain_count": 0, "piao": 0, "piao_success_rate": 1.0},
            {"baotou": True},
        )
        self.assertEqual(result, {"action": "discard", "tile": "白"})

    def test_high_value_baotou_white_hu(self):
        result = choose_hu_or_piao(
            {"drawn_tile": "白", "chain_count": 0, "piao": 0},
            {"baotou": True, "quads": 1, "fan": 16},
        )
        self.assertEqual(result, {"action": "hu", "tile": "白"})

    def test_normal_hu(self):
        result = choose_hu_or_piao(
            {"drawn_tile": "白", "chain_count": 0, "piao": 0, "piao_success_rate": 1.0},
            {"baotou": True, "fan": 1},
        )
        self.assertEqual(result, {"action": "discard", "tile": "白"})

    # --- 新增：验证链中段(chain_count>=1)在 _payout_factor 修复后可达 ---
    def test_second_chain_step_reachable_after_payout_fix(self):
        # chain_count=1 时 remaining_chain=2；旧 payout_factor(闲家=1) 下无论
        # piao_success_rate 多高都不可能选中 piao（数学上限0.8 < 所需阈值1.25），
        # 这是本次 _payout_factor 修复要打开的场景。
        # 2026-09-23：本用例原先用 0.5，是按「rate 只乘一次」的旧公式标定的。
        # 现在 piao_success_rate 是单步成功率、按 remaining_chain 复利，0.5 两步
        # 只有 0.25，EV = 4番×0.25 = 1.0 恰好等于直接胡的 1.0——此时直接胡才对。
        # 改用 0.8（两步 0.64，EV=2.56 明显占优）继续钉住"第二步可达"这个目的。
        result = choose_hu_or_piao(
            {"drawn_tile": "白", "chain_count": 1, "piao": 0, "piao_success_rate": 0.8},
            {"baotou": True, "fan": 1},
        )
        self.assertEqual(result, {"action": "discard", "tile": "白"})

    def test_high_fan_guard_scales_with_remaining_chain(self):
        """大牌护栏按**剩余步数**区分，而不是一刀切。

        默认单步成功率 0.9（飘完仍爆头的实测值 42/46）。
        - chain=0 还剩 3 步：0.9**3 = 0.729 <= 0.85，护栏保持——"已经 16 番
          就别为了搏 128 番连赌三次"，这是 s47 证伪场钉下来的结论，不许破坏。
        - chain=2 只剩 1 步：0.9 > 0.85，护栏让开——这一步的真实成功率就是
          实测的那 91.3%，再逼它直接胡等于白丢一个 ×2。
        对应 玄武-2346 a_a01a4f04a8ea_r1_b8_t0 第 2 局：他在 chain=2 时继续飘，
        拿到三财飘 32 番 +768；我们旧口径在这里强制直接胡，只值 8 番 192。
        """
        three_left = choose_hu_or_piao(
            {"drawn_tile": "白", "chain_count": 0, "piao": 0},
            {"baotou": True, "quads": 1, "fan": 16},
        )
        self.assertEqual(three_left, {"action": "hu", "tile": "白"})
        one_left = choose_hu_or_piao(
            {"drawn_tile": "白", "chain_count": 2, "piao": 0},
            {"baotou": True, "quads": 1, "fan": 16},
        )
        self.assertEqual(one_left, {"action": "discard", "tile": "白"})

    def test_third_chain_step_needs_high_success_rate(self):
        # chain_count=2 时 remaining_chain=1，即使修复后阈值也高达0.7，
        # 这里用0.75验证修复后确实可达，同时锁住"不能无限续飘"的边界。
        result = choose_hu_or_piao(
            {"drawn_tile": "白", "chain_count": 2, "piao": 0, "piao_success_rate": 0.75},
            {"baotou": True, "fan": 1},
        )
        self.assertEqual(result, {"action": "discard", "tile": "白"})
    # --- 新增结束 ---

    def test_plain_hu_when_not_baotou(self):
        result = choose_hu_or_piao({"drawn_tile": "5w"}, {"baotou": False})
        self.assertEqual(result["action"], "hu")

    def test_baotou_normal_draw_always_hu_never_declare(self):
        # 证伪记录 (s47): 爆头态打白宣言 = 丢掉构成全听的百搭, 5/5 宣言全部无果,
        # 我方爆头赢 1次/局 → 0。白是全听的承重墙, 永不宣言。
        result = choose_hu_or_piao(
            {"drawn_tile": "5w", "chain_count": 0, "piao": 0},
            {"baotou": True, "fan": 2},
        )
        self.assertEqual(result, {"action": "hu", "tile": "5w"})


class DeclineJokerHoldSwitchTests(unittest.TestCase):
    """rule_decline_joker_hold_enabled（默认关闭）：非爆头可胡时继续做大牌。

    2026-09-24 收窄（第三次修订，用户反馈）：触发条件从「财神>=1 或摸白」
    进一步收窄为按（有效财神数 x 副露数）分格核算后只保留的格子——
    **有效财神>=2 时任意副露数都触发；有效财神==1 时只有副露数==1 才
    触发**（副露 0 或 2+ 都不触发）。"有效财神数" = ``hu_result["counts"]``
    里的财神张数（``rules.evaluate()`` 摸牌后的 counts，已经把摸到的财神
    算进去了，不需要再单独判断"是否摸到白板"）。完整分格数字（top cohort
    10人 + 嘎达嘎达单人对照）见 mj/fit.py::DEFAULT_WEIGHTS 本键旁的注释、
    mj/hu_strategy.py::_decline_small_hu_tile docstring 与
    docs/experiments/OFFLINE_REPORT.md「五问结论」S1（收窄版）。"""

    NON_BAOTOU_TWO_JOKER_RESULT = {
        "hu": True, "baotou": False, "fan": 1, "detail": ["平胡"],
        # JOKER_IDX=33（见 mj/tiles.py ALL_TILES 顺序），设成 2 表示手上两张财神。
        "counts": tuple([0] * 33 + [2]),
    }
    SNAPSHOT = {"drawn_tile": "5w", "chain_count": 0, "piao": 0, "wall_remaining": 60}

    def test_default_off_returns_hu(self):
        result = choose_hu_or_piao(self.SNAPSHOT, self.NON_BAOTOU_TWO_JOKER_RESULT)
        self.assertEqual(result, {"action": "hu", "tile": "5w"})

    def test_enabled_without_hand_info_falls_back_to_hu(self):
        # 2 张财神、无副露门槛（jokers>=2 恒触发）也救不了没有 my_hand 的
        # 情况：_decline_discard_choice 要求"候选弃牌打出后真的转成爆头形"，
        # 没有手牌信息就无法验证，候选集合为空 → 必须返回 None，回落到
        # 直接胡——不能在无法验证的情况下盲目弃胡。
        import mj.fit as fit
        original = fit.DEFAULT_WEIGHTS.get("rule_decline_joker_hold_enabled")
        fit.DEFAULT_WEIGHTS["rule_decline_joker_hold_enabled"] = 1
        try:
            result = choose_hu_or_piao(self.SNAPSHOT, self.NON_BAOTOU_TWO_JOKER_RESULT)
        finally:
            fit.DEFAULT_WEIGHTS["rule_decline_joker_hold_enabled"] = original
        self.assertEqual(result, {"action": "hu", "tile": "5w"})

    def test_enabled_but_baotou_state_untouched(self):
        # 爆头态不受本开关影响，胡/飘取舍仍由 _piao_candidate 负责。
        import mj.fit as fit
        original = fit.DEFAULT_WEIGHTS.get("rule_decline_joker_hold_enabled")
        fit.DEFAULT_WEIGHTS["rule_decline_joker_hold_enabled"] = 1
        try:
            result = choose_hu_or_piao(
                {"drawn_tile": "5w", "chain_count": 0, "piao": 0},
                {"baotou": True, "fan": 2},
            )
        finally:
            fit.DEFAULT_WEIGHTS["rule_decline_joker_hold_enabled"] = original
        self.assertEqual(result, {"action": "hu", "tile": "5w"})

    def test_enabled_zero_joker_no_draw_still_hu(self):
        # 有效财神==0：两档触发条件都不满足，必须仍然直接胡。
        import mj.fit as fit
        original = fit.DEFAULT_WEIGHTS.get("rule_decline_joker_hold_enabled")
        fit.DEFAULT_WEIGHTS["rule_decline_joker_hold_enabled"] = 1
        zero_joker_result = {"hu": True, "baotou": False, "fan": 1,
                             "counts": tuple([0] * 34)}
        try:
            result = choose_hu_or_piao(self.SNAPSHOT, zero_joker_result)
        finally:
            fit.DEFAULT_WEIGHTS["rule_decline_joker_hold_enabled"] = original
        self.assertEqual(result, {"action": "hu", "tile": "5w"})

    # 以下夹具均已用脚本核实过"打掉指定牌之后 rules.baotou() 是否为真"
    # （见 tools/s1_narrow.py 同期的验证脚本，不是手推）。

    # (有效财神=1, 副露=1)：保留的触发格子。一组顺子(1w2w3w) + 一组顺子
    # (4w5w6w) + 1b1b1b 刻子（共 2 组，10 张）+ 财神(1) + 浮张"东"(1) = 11
    # 张，配一个已有的外部副露（7w8w9w）令 meld_groups=1。打掉"东"，剩下
    # 「3 组(含1组外部副露) + 单钓财神」= 摸任意牌均胡。
    HAND_1JOKER_1MELD = ["1w", "2w", "3w", "4w", "5w", "6w",
                        "1b", "1b", "1b", "白", "东"]
    MELDS_1MELD = [[{"kind": "chi", "tiles": ["7w", "8w", "9w"]}], [], [], []]

    # (有效财神=2, 副露=0)：保留的触发格子。三组顺子(9张) + 1b1b 对子(2张)
    # + 2 张财神 + 浮张"东" = 14 张，meld_groups=0。打掉"东"，剩下
    # 「3 组 + 1b1b 将 + 2 张财神」——2 张财神百搭，摸任意一张都能配成第 4 组。
    HAND_2JOKER_0MELD = ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w",
                        "1b", "1b", "白", "白", "东"]
    MELDS_EMPTY = [[], [], [], []]

    def test_kept_bucket_one_joker_one_meld_declines_correct_tile(self):
        """收窄后保留的格子 (财神==1, 副露==1)：必须弃胡且打"东"。"""
        import mj.fit as fit
        original = fit.DEFAULT_WEIGHTS.get("rule_decline_joker_hold_enabled")
        fit.DEFAULT_WEIGHTS["rule_decline_joker_hold_enabled"] = 1
        snapshot = {
            "drawn_tile": "东", "chain_count": 0, "piao": 0, "wall_remaining": 60,
            "my_hand": self.HAND_1JOKER_1MELD, "melds": self.MELDS_1MELD, "seat": 0,
        }
        one_joker_result = {"hu": True, "baotou": False, "fan": 1,
                            "counts": tuple([0] * 33 + [1])}
        try:
            result = choose_hu_or_piao(snapshot, one_joker_result)
        finally:
            fit.DEFAULT_WEIGHTS["rule_decline_joker_hold_enabled"] = original
        self.assertEqual(result, {"action": "discard", "tile": "东"})

    def test_kept_bucket_two_jokers_no_meld_declines_correct_tile(self):
        """收窄后保留的格子 (财神>=2, 副露==0)：必须弃胡且打"东"。"""
        import mj.fit as fit
        original = fit.DEFAULT_WEIGHTS.get("rule_decline_joker_hold_enabled")
        fit.DEFAULT_WEIGHTS["rule_decline_joker_hold_enabled"] = 1
        snapshot = {
            "drawn_tile": "东", "chain_count": 0, "piao": 0, "wall_remaining": 60,
            "my_hand": self.HAND_2JOKER_0MELD, "melds": self.MELDS_EMPTY, "seat": 0,
        }
        two_joker_result = {"hu": True, "baotou": False, "fan": 1,
                            "counts": tuple([0] * 33 + [2])}
        try:
            result = choose_hu_or_piao(snapshot, two_joker_result)
        finally:
            fit.DEFAULT_WEIGHTS["rule_decline_joker_hold_enabled"] = original
        self.assertEqual(result, {"action": "discard", "tile": "东"})

    def test_excluded_bucket_one_joker_no_meld_stays_hu(self):
        """收窄后剔除的格子 (财神==1, 副露==0)：即使这手牌打掉"东"确实能
        转爆头（同样的牌型只是没有外部副露），也必须直接胡——证明这是
        "分格门槛"本身在起作用，不是"凑巧没有能转爆头的牌"。"""
        import mj.fit as fit
        original = fit.DEFAULT_WEIGHTS.get("rule_decline_joker_hold_enabled")
        fit.DEFAULT_WEIGHTS["rule_decline_joker_hold_enabled"] = 1
        hand = ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w",
               "1b", "1b", "1b", "白", "东"]
        snapshot = {
            "drawn_tile": "东", "chain_count": 0, "piao": 0, "wall_remaining": 60,
            "my_hand": hand, "melds": self.MELDS_EMPTY, "seat": 0,
        }
        one_joker_result = {"hu": True, "baotou": False, "fan": 1,
                            "counts": tuple([0] * 33 + [1])}
        try:
            result = choose_hu_or_piao(snapshot, one_joker_result)
        finally:
            fit.DEFAULT_WEIGHTS["rule_decline_joker_hold_enabled"] = original
        self.assertEqual(result, {"action": "hu", "tile": "东"})

    def test_excluded_bucket_one_joker_two_melds_stays_hu(self):
        """收窄后剔除的格子 (财神==1, 副露>=2)：直接胡（这里不提供 my_hand，
        只钉住"分格门槛"这一条路径本身返回 None，能否转爆头由上面
        test_excluded_bucket_one_joker_no_meld_stays_hu 单独验证过）。"""
        import mj.fit as fit
        original = fit.DEFAULT_WEIGHTS.get("rule_decline_joker_hold_enabled")
        fit.DEFAULT_WEIGHTS["rule_decline_joker_hold_enabled"] = 1
        melds_two = [[{"kind": "chi", "tiles": ["1t", "2t", "3t"]}],
                    [{"kind": "chi", "tiles": ["4t", "5t", "6t"]}], [], []]
        snapshot = {
            "drawn_tile": "5w", "chain_count": 0, "piao": 0, "wall_remaining": 60,
            "melds": melds_two, "seat": 0,
        }
        one_joker_result = {"hu": True, "baotou": False, "fan": 1,
                            "counts": tuple([0] * 33 + [1])}
        try:
            result = choose_hu_or_piao(snapshot, one_joker_result)
        finally:
            fit.DEFAULT_WEIGHTS["rule_decline_joker_hold_enabled"] = original
        self.assertEqual(result, {"action": "hu", "tile": "5w"})

    def test_enabled_cannot_reach_baotou_stays_hu(self):
        """correctness 修复第一条（收窄后仍适用）：即使命中保留的格子
        (财神>=2, 副露==0)，不存在"打出后真的转爆头"的非财神牌时也必须
        直接胡。夹具：六组分散的两面搭子（1w2w/4w5w/7w8w/1b2b/4b5b/7b8b）
        + 2 张财神，14 张里任何一张非财神弃牌都无法让剩余 13 张构成
        "摸任意牌均胡"（已用脚本核实：全部候选 baotou()=False）。"""
        import mj.fit as fit
        original = fit.DEFAULT_WEIGHTS.get("rule_decline_joker_hold_enabled")
        fit.DEFAULT_WEIGHTS["rule_decline_joker_hold_enabled"] = 1
        hand = ["1w", "2w", "4w", "5w", "7w", "8w", "1b", "2b", "4b", "5b", "7b", "8b",
               "白", "白"]
        snapshot = {
            "drawn_tile": "白", "chain_count": 0, "piao": 0, "wall_remaining": 60,
            "my_hand": hand, "melds": self.MELDS_EMPTY, "seat": 0,
        }
        two_joker_result = {"hu": True, "baotou": False, "fan": 1,
                            "counts": tuple([0] * 33 + [2])}
        try:
            result = choose_hu_or_piao(snapshot, two_joker_result)
        finally:
            fit.DEFAULT_WEIGHTS["rule_decline_joker_hold_enabled"] = original
        self.assertEqual(result, {"action": "hu", "tile": "白"})

    def test_enabled_wall_tail_gate_forces_hu(self):
        """墙尾闸门（correctness 修复第二条，收窄后仍保留）：即使命中保留的
        格子 (财神>=2, 副露==0) 且存在能转爆头的牌，wall_remaining-20<8
        时也必须直接胡。27-20=7<8，触发闸门。"""
        import mj.fit as fit
        original = fit.DEFAULT_WEIGHTS.get("rule_decline_joker_hold_enabled")
        fit.DEFAULT_WEIGHTS["rule_decline_joker_hold_enabled"] = 1
        snapshot = {
            "drawn_tile": "东", "chain_count": 0, "piao": 0, "wall_remaining": 27,
            "my_hand": self.HAND_2JOKER_0MELD, "melds": self.MELDS_EMPTY, "seat": 0,
        }
        two_joker_result = {"hu": True, "baotou": False, "fan": 1,
                            "counts": tuple([0] * 33 + [2])}
        try:
            result = choose_hu_or_piao(snapshot, two_joker_result)
        finally:
            fit.DEFAULT_WEIGHTS["rule_decline_joker_hold_enabled"] = original
        self.assertEqual(result, {"action": "hu", "tile": "东"})

    def test_wall_tail_gate_boundary_still_declines(self):
        """闸门边界：wall_remaining=28 时 28-20=8，不小于 8，不触发闸门，
        应正常弃胡——防止闸门条件被误写成 <=8。"""
        import mj.fit as fit
        original = fit.DEFAULT_WEIGHTS.get("rule_decline_joker_hold_enabled")
        fit.DEFAULT_WEIGHTS["rule_decline_joker_hold_enabled"] = 1
        snapshot = {
            "drawn_tile": "东", "chain_count": 0, "piao": 0, "wall_remaining": 28,
            "my_hand": self.HAND_2JOKER_0MELD, "melds": self.MELDS_EMPTY, "seat": 0,
        }
        two_joker_result = {"hu": True, "baotou": False, "fan": 1,
                            "counts": tuple([0] * 33 + [2])}
        try:
            result = choose_hu_or_piao(snapshot, two_joker_result)
        finally:
            fit.DEFAULT_WEIGHTS["rule_decline_joker_hold_enabled"] = original
        self.assertEqual(result, {"action": "discard", "tile": "东"})


if __name__ == "__main__":
    unittest.main()

class DeclineSingleJokerAllMeldsTests(unittest.TestCase):
    """rule_decline_single_joker_all_melds_enabled：财神==1、门清时也弃胡转爆头。"""

    def _case(self, extra):
        from mj.hu_strategy import _decline_small_hu_tile
        from mj.tiles import to_counts
        hand = ["1w", "1w", "1w", "2w", "3w", "4w", "5w", "6w", "7w", "3b", "4b", "9t", "白", "5b"]
        snap = {"my_hand": hand, "drawn_tile": "5b", "melds": [[], [], [], []], "seat": 0,
                "wall_remaining": 60}
        hu = {"baotou": False, "counts": tuple(to_counts(hand))}
        return _decline_small_hu_tile(snap, hu, {"rule_decline_joker_hold_enabled": 1, **extra})

    def test_off_keeps_narrowed_behavior(self):
        self.assertIsNone(self._case({}))

    def test_on_declines_and_discards_to_baotou(self):
        self.assertEqual(self._case({"rule_decline_single_joker_all_melds_enabled": 1}), "9t")
