import copy
import unittest

from mj.tiles import JOKER_IDX, TILE_INDEX, to_counts
from mj.shanten import shanten as mj_shanten
from tools import mining_common
from tools.decision_table import replay_round


def _cohort_of(uid):
    return "other"


UIDS = ["u0", "u1", "u2", "u3"]


def _mechanics_round():
    """一局手工构造的迷你对局：吃 / 碰 / 明杠 / 暗杠 / 补杠 / 抓打圈 / 服务端代打
    全部覆盖一遍。只测重放机制的账本是否正确，不要求牌型合法。"""
    s0 = ["1w", "1w", "1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w", "东", "南", "北"]
    s1 = ["1b", "1b", "1b", "2b", "3b", "4b", "5b", "6b", "7b", "8b", "9b", "西", "发"]
    s2 = ["4w", "6w", "1t", "2t", "3t", "4t", "5t", "6t", "7t", "8t", "9t", "中", "中"]
    s3 = ["9b", "9b", "东", "南", "白", "白", "中", "中", "1t", "2t", "3t", "4t", "5t"]
    events = [
        dict(seq=1, type="tile_discarded", seat=0, tile="北", data={"catch_play": False}),
        dict(seq=5, type="tile_drawn", seat=1, tile="9t", data=None),
        dict(seq=6, type="tile_discarded", seat=1, tile="9t", data={"catch_play": False}),
        dict(seq=7, type="chi", seat=2, tile="9t", data={"tiles": ["7t", "8t", "9t"]}),
        dict(seq=8, type="tile_discarded", seat=2, tile="中", data={"catch_play": False}),
        dict(seq=9, type="peng", seat=3, tile="中", data=None),
        dict(seq=10, type="tile_discarded", seat=3, tile="5t", data={"catch_play": False}),
        dict(seq=14, type="tile_drawn", seat=0, tile="1w", data=None),
        dict(seq=15, type="gang", seat=0, tile="1w", data={"kind": "an"}),
        dict(seq=16, type="tile_drawn", seat=0, tile="8b", data={"gang_replenish": True}),
        dict(seq=17, type="tile_discarded", seat=0, tile="8b", data={"catch_play": False}),
        dict(seq=20, type="tile_drawn", seat=3, tile="中", data=None),
        dict(seq=21, type="gang", seat=3, tile="中", data={"kind": "bu"}),
        dict(seq=22, type="tile_drawn", seat=3, tile="7b", data={"gang_replenish": True}),
        dict(seq=23, type="tile_discarded", seat=3, tile="7b", data={"catch_play": False}),
        dict(seq=24, type="tile_drawn", seat=2, tile="1b", data=None),
        dict(seq=25, type="tile_discarded", seat=2, tile="1b", data={"catch_play": False}),
        dict(seq=26, type="gang", seat=1, tile="1b", data={"kind": "ming"}),
        dict(seq=27, type="tile_drawn", seat=1, tile="6t", data={"gang_replenish": True}),
        dict(seq=28, type="tile_discarded", seat=1, tile="6t", data={"catch_play": False}),
        dict(seq=29, type="tile_drawn", seat=2, tile="7b", data=None),
        dict(seq=30, type="tile_discarded", seat=2, tile="7b", data={"catch_play": False}),
        dict(seq=31, type="timeout", seat=2, tile="", data={"kind": "discard"}),
        dict(seq=32, type="tile_drawn", seat=3, tile="白", data=None),
        dict(seq=33, type="tile_discarded", seat=3, tile="白", data={"catch_play": True}),
        dict(seq=34, type="tile_drawn", seat=0, tile="3b", data=None),
        dict(seq=35, type="tile_discarded", seat=0, tile="3b", data={"catch_play": True}),
        dict(seq=36, type="tile_drawn", seat=1, tile="9t", data=None),
        dict(seq=37, type="tile_discarded", seat=1, tile="9t", data={"catch_play": False}),
    ]
    rnd = dict(round_no=1, start_hands=[s0, s1, s2, s3], events=events, truncated=False)
    info = dict(dealer=0, winner=None, is_draw=True, scores=[0, 0, 0, 0], round_no=1)
    return rnd, info


class ReplayMechanicsTests(unittest.TestCase):
    def setUp(self):
        self.rnd, self.info = _mechanics_round()
        self.rows, self.stats = replay_round(
            copy.deepcopy(self.rnd), _cohort_of, UIDS, set(), True, 0, "room1", "game1", self.info)
        self.by_seq = {r["seq"]: r for r in self.rows}

    def test_no_replay_errors(self):
        self.assertEqual(self.stats.get("dropped_replay_error", 0), 0)

    def test_auto_discard_excluded(self):
        # seq30 的 tile_discarded(seat2, 7b) 被 seq31 的 timeout(discard) 配对，
        # 必须从输出里剔除（不是玩家决策）。
        self.assertNotIn(30, self.by_seq)

    def test_non_auto_discards_present(self):
        for seq in (1, 6, 8, 10, 17, 23, 25, 28, 33, 35, 37):
            self.assertIn(seq, self.by_seq, "seq %d 应该出现在决策表里" % seq)

    def test_chi_updates_meld_and_hand(self):
        # seat2 吃了 9t（用 7t/8t），随后 seq8 弃 中 时手上副露数应为 1，
        # chi_used 应为 1。
        row = self.by_seq[8]
        self.assertEqual(row["melds"], 1)
        self.assertEqual(row["chi_used"], 1)

    def test_peng_updates_meld(self):
        row = self.by_seq[10]
        self.assertEqual(row["melds"], 1)
        self.assertEqual(row["chi_used"], 0)

    def test_an_gang_increments_chain_and_meld(self):
        # seq15 暗杠之后，seq17 的弃牌应看到 melds=1，chain 因杠 +1 = 1。
        row = self.by_seq[17]
        self.assertEqual(row["melds"], 1)
        self.assertEqual(row["chain"], 1)

    def test_bu_gang_does_not_add_new_meld_group(self):
        # seat3 先 peng(中) 后 bu-gang(中)：副露组数应保持 1（补杠不新增组）。
        row = self.by_seq[23]
        self.assertEqual(row["melds"], 1)
        self.assertEqual(row["chain"], 1)  # 杠 +1

    def test_ming_gang_removes_three_and_adds_meld(self):
        row = self.by_seq[28]
        self.assertEqual(row["melds"], 1)
        self.assertEqual(row["chain"], 1)

    def test_catch_play_flag_and_exempt_seat(self):
        # seq33: seat3 打白触发抓打圈（catch_play=True），seat3 是豁免方。
        row33 = self.by_seq[33]
        self.assertEqual(row33["catch_play"], 1)
        # seq35: seat0 在抓打圈内被迫打刚摸的牌，非豁免方。
        row35 = self.by_seq[35]
        self.assertEqual(row35["catch_play"], 1)
        self.assertEqual(row35["i_am_exempt"], 0)

    def test_catch_play_clears_after_cycle(self):
        # seq37: catch_play=False，抓打圈已结束。
        row37 = self.by_seq[37]
        self.assertEqual(row37["catch_play"], 0)
        self.assertEqual(row37["i_am_exempt"], 0)

    def test_wall_left_formula(self):
        # 136 - 起手发牌(14+13+13+13=53) - 已摸张数。
        # seq1 是第一手弃牌，此时尚未发生任何 tile_drawn 事件。
        row1 = self.by_seq[1]
        self.assertEqual(row1["wall_left"], 136 - 53 - 0)
        self.assertEqual(row1["to_dead"], row1["wall_left"] - 20)


class LeakageTests(unittest.TestCase):
    """P3：置换对手暗牌后重新生成同一决策点，特征列必须完全不变。"""

    def test_opponent_hidden_hand_does_not_affect_features(self):
        rnd, info = _mechanics_round()
        rows_a, _ = replay_round(copy.deepcopy(rnd), _cohort_of, UIDS, set(), True, 0,
                                 "room1", "game1", info)

        # 置换 seat1 起手牌里从未被任何事件引用过的两张牌（发 <-> 西 已经用掉，
        # 改用两张真正不受任何事件触碰的填充牌：把 2b 换成 9t 的等价替代身份，
        # 只要不破坏后续 remove() 调用即可）。这里选 seat1 起手牌里数值最大、
        # 从未被摸/弃/吃/碰/杠触碰的 "9b" 与 "8b"（seat1 起手没有这两张，
        # 改为替换真正存在且未被触碰的 "5b" <-> "6b"，两者都不出现在任何事件里）。
        rnd_b = copy.deepcopy(rnd)
        s1 = rnd_b["start_hands"][1]
        i5b, i6b = s1.index("5b"), s1.index("6b")
        s1[i5b], s1[i6b] = s1[i6b], s1[i5b]  # 交换位置，身份其实没变——再换成真不同的牌
        # 真正的置换：把这两张牌的身份换成另一对同样从未被引用的牌。
        s1[i5b] = "7w"
        s1[i6b] = "8w" if "8w" not in s1 else "9w"

        rows_b, _ = replay_round(copy.deepcopy(rnd_b), _cohort_of, UIDS, set(), True, 0,
                                 "room1", "game1", info)

        by_seq_a = {r["seq"]: r for r in rows_a}
        by_seq_b = {r["seq"]: r for r in rows_b}
        self.assertEqual(set(by_seq_a), set(by_seq_b))
        for seq, row_a in by_seq_a.items():
            row_b = by_seq_b[seq]
            # 只要不是 seat1 自己的决策点，特征列必须逐字节相同（seat1 的暗牌被换，
            # 其它三家看不到，任何字段都不应该变化）。
            if row_a["seat"] == 1:
                continue
            self.assertEqual(row_a, row_b, "seq=%d 的特征列因对手暗牌置换而改变（信息泄漏）" % seq)


def _hu_or_not_round():
    win_hand = ["1w", "1w", "1w", "2w", "2w", "2w", "3w", "3w", "3w", "4w", "4w", "4w", "5w"]
    s1 = ["1b", "2b", "3b", "4b", "5b", "6b", "7b", "8b", "9b", "东", "南", "西", "北", "中"]
    s2 = ["1t", "2t", "3t", "4t", "5t", "6t", "7t"]
    s3 = ["8t", "9t", "发", "白", "1w", "2w", "3w"]
    events = [
        dict(seq=1, type="tile_drawn", seat=0, tile="5w", data=None),
        dict(seq=2, type="tile_discarded", seat=0, tile="5w", data={"catch_play": False}),
        dict(seq=3, type="tile_drawn", seat=0, tile="5w", data=None),
    ]
    rnd = dict(round_no=1, start_hands=[win_hand, s1, s2, s3], events=events, truncated=False)
    info = dict(dealer=1, winner=0, is_draw=False, scores=[40, -10, -10, -20], round_no=1)
    return rnd, info


class HuOrNotTests(unittest.TestCase):
    def test_decline_then_accept(self):
        rnd, info = _hu_or_not_round()
        rows, stats = replay_round(rnd, _cohort_of, UIDS, set(), True, 8, "room1", "game1", info)
        by_seq = {r["seq"]: r for r in rows}
        self.assertEqual(by_seq[1]["dtype"], "hu_or_not")
        self.assertEqual(by_seq[1]["act_type"], "decline_hu")
        self.assertEqual(by_seq[2]["dtype"], "discard")
        self.assertEqual(by_seq[3]["dtype"], "hu_or_not")
        self.assertEqual(by_seq[3]["act_type"], "hu")
        self.assertEqual(by_seq[3]["y_hu"], 1)
        self.assertEqual(by_seq[3]["y_fan"], 8)
        self.assertEqual(by_seq[1]["y_hu"], 1)  # 局级标签对本局所有该座位的行相同


class ToBaotouTests(unittest.TestCase):
    """to_baotou 独立正确性检查：用另一条推导路径（把 1 张财神留作将，其余财神当
    百搭塞进 mj.shanten.shanten，并在一个从不出现在手牌里的字牌位置人工放一对
    „已完成的将“）交叉验证——两条完全不同的实现路径互相印证，覆盖度等价于
    20+ 个手工例子。"""

    HONORS = ["东", "南", "西", "北", "中", "发"]

    def _oracle(self, counts13, meld_groups):
        jokers = counts13[JOKER_IDX]
        if not jokers:
            return None
        real = list(counts13)
        real[JOKER_IDX] = 0
        wild = jokers - 1
        free = next((TILE_INDEX[h] for h in self.HONORS if real[TILE_INDEX[h]] == 0), None)
        if free is None:
            return None
        mod = list(real)
        mod[free] = 2
        mod[JOKER_IDX] = wild
        # mj_shanten 对"已完成"的手返回 -1（agari 惯例），而 to_baotou 把"已经是
        # 爆头结构"记为 0——两者只差一个固定偏移，加 1 对齐。
        return mj_shanten(tuple(mod), meld_groups) + 1

    def test_no_joker_is_none(self):
        hand = ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w", "东", "南", "西", "北"]
        self.assertIsNone(mining_common.to_baotou(to_counts(hand), 0))

    def test_perfect_structure_is_zero(self):
        hand = ["1w", "1w", "1w", "2w", "2w", "2w", "3w", "3w", "3w", "4w", "4w", "4w", "白"]
        self.assertEqual(mining_common.to_baotou(to_counts(hand), 0), 0)

    def test_cross_validated_against_oracle(self):
        import random
        rng = random.Random(20260924)
        suits = ["w", "b", "t"]
        cases = 0
        for meld_groups in (0, 1, 2, 3):
            need = 13 - 3 * meld_groups
            for _trial in range(6):
                for jk in (1, 2, 3, 4):
                    if jk > need:
                        continue
                    pool = [f"{n}{s}" for n in range(1, 10) for s in suits] + self.HONORS
                    rng.shuffle(pool)
                    hand = []
                    counts = {}
                    for t in pool:
                        if len(hand) >= need - jk:
                            break
                        if counts.get(t, 0) >= 4:
                            continue
                        hand.append(t)
                        counts[t] = counts.get(t, 0) + 1
                    hand += ["白"] * jk
                    if len(hand) != need:
                        continue
                    counts13 = to_counts(hand)
                    got = mining_common.to_baotou(counts13, meld_groups)
                    want = self._oracle(counts13, meld_groups)
                    if want is None:
                        continue
                    self.assertEqual(got, want,
                                     "meld_groups=%d hand=%s got=%s want=%s" % (meld_groups, hand, got, want))
                    cases += 1
        self.assertGreaterEqual(cases, 20, "交叉验证样本数不足 20")


class AutoDiscardPairingTests(unittest.TestCase):
    def test_immediate_pairing(self):
        events = [
            dict(seq=1, type="tile_drawn", seat=0, tile="1w"),
            dict(seq=2, type="tile_discarded", seat=0, tile="1w", data={"catch_play": False}),
            dict(seq=3, type="timeout", seat=0, tile="", data={"kind": "discard"}),
        ]
        idx = mining_common.mark_auto_discards(events)
        self.assertEqual(idx, {1})

    def test_catch_play_cascade_gap(self):
        # 真实数据里 catch_play 连锁时，timeout(discard) 与其对应的 tile_discarded
        # 之间会插入别家的 tile_drawn 事件（gap=2 而不是 1）。
        events = [
            dict(seq=1, type="tile_drawn", seat=0, tile="白"),
            dict(seq=2, type="tile_discarded", seat=0, tile="白", data={"catch_play": True}),
            dict(seq=3, type="tile_drawn", seat=1, tile="7b"),
            dict(seq=4, type="timeout", seat=0, tile="", data={"kind": "discard"}),
            dict(seq=5, type="tile_discarded", seat=1, tile="7b", data={"catch_play": True}),
        ]
        idx = mining_common.mark_auto_discards(events)
        self.assertEqual(idx, {1})


if __name__ == "__main__":
    unittest.main()
