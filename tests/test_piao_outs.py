"""2026-10-09：piao_outs_tiebreak——爆头听 + 2 财神时，在同为爆头听的弃牌里选财飘张数最多的。"""
import os
import unittest

os.environ.setdefault("MJ_WEIGHTS_NO_FILE", "1")

from mj import bot  # noqa: E402
from mj.fit import thread_weights_overlay  # noqa: E402
from mj.rules import baotou  # noqa: E402
from mj.tiles import to_counts  # noqa: E402

# 副露 2 组；打 7t → 123b + 45t白（两面，3t/6t 可换下财神 = 8 张）+ 白 单吊 = 爆头听
#            打 4t → 123b + 57t白（坎张，只有 6t = 4 张）+ 白 单吊 = 爆头听
HAND = ["1b", "2b", "3b", "4t", "5t", "7t", "白", "白"]
MELDS = [[{"kind": "peng", "tiles": ["东", "东", "东"]}, {"kind": "peng", "tiles": ["南", "南", "南"]}], [], [], []]


def _rest(t):
    h = list(HAND)
    h.remove(t)
    return tuple(to_counts(h))


class PiaoOutsTests(unittest.TestCase):
    def test_outs_count(self):
        self.assertTrue(baotou(_rest("7t"), 2) and baotou(_rest("4t"), 2))
        self.assertEqual(bot._piao_outs(_rest("7t"), 2, None), 8)
        self.assertEqual(bot._piao_outs(_rest("4t"), 2, None), 4)

    def test_pick_prefers_wider_substitution(self):
        with thread_weights_overlay({"piao_outs_tiebreak": 1}):
            self.assertEqual(bot._piao_outs_pick(HAND, 2, None, "4t"), "7t")

    def test_off_keeps_choice(self):
        with thread_weights_overlay({"piao_outs_tiebreak": 0}):
            self.assertEqual(bot._piao_outs_pick(HAND, 2, None, "4t"), "4t")

    def test_not_baotou_keeps_choice(self):
        with thread_weights_overlay({"piao_outs_tiebreak": 1}):
            self.assertEqual(bot._piao_outs_pick(HAND, 2, None, "5t"), "5t")

    def test_end_to_end_choose_discard(self):
        snap = {"my_hand": HAND, "seat": 0, "dealer": 1, "melds": MELDS, "discards": [[], [], [], []],
                "wall_remaining": 50, "drawn_tile": "7t", "round_no": 2}
        with thread_weights_overlay({"piao_outs_tiebreak": 1}):
            self.assertEqual(bot.choose_discard(snap)["tile"], "7t")


if __name__ == "__main__":
    unittest.main()
