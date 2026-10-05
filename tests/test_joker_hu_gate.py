"""摸到财神时的拒胡门禁：只在「有财必靠」(YouCaiBiKao) 开启时生效。

2026-09-23 实测纠错。全量重放 21 房 / 1672 局（判据：该次 tile_drawn 之后
本局立刻 round_ended 且胜者为该座位）：对手用摸来的白板在非爆头手上当场
自摸成和 243 次，服务端全部确认；我们 86 次机会 0 次。这些房间的决策日志
里 rules 全部是空 {}。依据与回滚条件见 docs/audit/TASK_P0_JOKER_HU.md。

本文件只读本地 logs/，不联网。
"""
import glob
import json
import os
import unittest

from mj.bot import can_hu, hu_result
from mj.rules import baotou, seven_pairs, win_standard
from mj.tiles import JOKER, TILE_INDEX, to_counts

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KAOXIANG = {"YouCaiBiKao": True}

# 取自生产日志 logs/2026-09-2*.jsonl 的真实手牌：两副露 + 摸白凑成平胡。
REAL_HAND = ["1b", "3b", "4b", "4b", "6t", "6t", JOKER, JOKER]


def _snapshot(hand, meld_groups, drawn=JOKER):
    melds = [[{"kind": "peng"} for _ in range(meld_groups)], [], [], []]
    return {"phase": "draw", "seat": 0, "turn": 0, "my_hand": list(hand),
            "drawn_tile": drawn, "melds": melds, "wall_remaining": 50,
            "dealer": 1, "god": {}}


class JokerHuGateTests(unittest.TestCase):
    def test_non_baotou_joker_draw_wins_when_rule_off(self):
        result = hu_result(_snapshot(REAL_HAND, 2))
        self.assertIsNotNone(result)
        self.assertEqual(result["fan"], 1)
        self.assertFalse(result["baotou"])
        self.assertTrue(can_hu(_snapshot(REAL_HAND, 2)))

    def test_non_baotou_joker_draw_refused_when_rule_on(self):
        self.assertIsNone(hu_result(_snapshot(REAL_HAND, 2), rules=KAOXIANG))
        self.assertFalse(can_hu(_snapshot(REAL_HAND, 2), rules=KAOXIANG))

    def test_non_joker_draw_unaffected_by_rule(self):
        """摸普通牌的路径不受本门禁影响：两种 rules 下结果一致。"""
        hand13 = ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w",
                  "1t", "2t", "3t", "5b"]
        snap = _snapshot(hand13 + ["5b"], 0, drawn="5b")
        off, on = hu_result(snap), hu_result(snap, rules=KAOXIANG)
        self.assertIsNotNone(off)
        self.assertIsNotNone(on)
        self.assertEqual(off["fan"], on["fan"])


class HistoricalRefusedWinsTests(unittest.TestCase):
    """生产日志回归：真实发生过的"摸白弃牌但手牌可胡"不得再被拒绝。"""

    @classmethod
    def setUpClass(cls):
        cls.cases = []
        for path in sorted(glob.glob(os.path.join(ROOT, "logs", "*.jsonl"))):
            with open(path, encoding="utf-8", errors="replace") as source:
                for line in source:
                    if '"drawn_tile": "%s"' % JOKER not in line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if record.get("kind") != "decision":
                        continue
                    payload = record["payload"]
                    decision = payload.get("decision") or {}
                    if payload.get("phase") != "draw" or decision.get("action") != "discard":
                        continue
                    hand = payload.get("hand") or []
                    melds = payload.get("melds") or []
                    seat = payload.get("seat")
                    groups = len(melds[seat]) if isinstance(melds, list) and len(melds) == 4 else 0
                    if len(hand) + 3 * groups != 14:
                        continue
                    counts = tuple(to_counts(hand))
                    if win_standard(counts, groups) or (groups == 0 and seven_pairs(counts) is not None):
                        counts13 = list(counts)
                        counts13[TILE_INDEX[JOKER]] -= 1
                        cls.cases.append((hand, groups, baotou(tuple(counts13), groups)))

    def test_corpus_contains_the_historical_refusals(self):
        self.assertGreaterEqual(len(self.cases), 90)

    def test_historical_refused_wins_are_now_accepted(self):
        accepted = sum(1 for hand, groups, _ in self.cases
                       if hu_result(_snapshot(hand, groups)) is not None)
        self.assertGreaterEqual(accepted, int(0.95 * len(self.cases)))

    def test_non_baotou_refusals_blocked_under_kaoxiang(self):
        """「有财必靠」开启时，**非爆头**的摸财神成牌必须全部被拒。

        2026-09-23 勘误：本条原先断言"全部案例都被拦"，那是错的——爆头的定义
        就是"摸任意合法牌都胡"，财神也在"任意牌"范围内，所以爆头手摸到财神
        无论规则开关都该允许胡（mj/rules.py::evaluate 的 must_kaoxiang 分支
        本来就有 `and not is_baotou`）。写下那条断言时日志里恰好一个爆头样本
        都没有，日志增长后就暴露了。错在断言，不在实现。"""
        non_baotou = [(h, g) for h, g, bt in self.cases if not bt]
        self.assertGreater(len(non_baotou), 50, "样本太少，无法验证")
        blocked = sum(1 for h, g in non_baotou
                      if hu_result(_snapshot(h, g), rules=KAOXIANG) is None)
        self.assertEqual(blocked, len(non_baotou))

    def test_baotou_refusals_remain_winnable_under_kaoxiang(self):
        """爆头手摸财神：两种 rules 下都必须能胡。"""
        for hand, groups, bt in self.cases:
            if not bt:
                continue
            self.assertIsNotNone(hu_result(_snapshot(hand, groups), rules=KAOXIANG))
            self.assertIsNotNone(hu_result(_snapshot(hand, groups)))
