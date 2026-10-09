import os
import unittest
from unittest import mock

os.environ.setdefault("MJ_WEIGHTS_NO_FILE", "1")

from mj import bot as bot_module  # noqa: E402
from mj.fit import load_weights  # noqa: E402


def _w(**kw):
    w = dict(load_weights())
    w["rule_baotou_gang_open_enabled"] = 1
    w.update(kw)
    return lambda: w


def _snap(hand, drawn, wall=50):
    return {"phase": "draw", "turn": 0, "seat": 0, "dealer": 1, "round_no": 2, "my_hand": hand,
            "drawn_tile": drawn, "wall_remaining": wall, "discards": [[], [], [], []],
            "melds": [[{"kind": "peng", "tiles": ["南", "南", "南"]}], [], [], []]}


# 摸到碰牌 南 的第 4 张；手里有财神、还没成形
HOLD = ["南", "白", "2b", "3b", "5t", "6t", "8w", "9w", "1t", "4w", "东"]
# 留着的 南 和 白 凑对已能胡；补杠 南 后剩下 123b 456t 789w 白 = 爆头听
DONE = ["南", "白", "1b", "2b", "3b", "4t", "5t", "6t", "7w", "8w", "9w"]


class BuDeferTests(unittest.TestCase):
    def _act(self, snap, **kw):
        with mock.patch.object(bot_module, "load_weights", _w(**kw)):
            return bot_module._choose_action_production(snap)

    def test_off_bu_gang_now(self):
        self.assertEqual(self._act(_snap(HOLD, "南")), {"action": "gang", "tile": "南"})

    def test_on_keeps_fourth_tile(self):
        act = self._act(_snap(HOLD, "南"), bu_defer_enabled=1)
        self.assertEqual(act["action"], "discard")
        self.assertNotEqual(act["tile"], "南")

    def test_on_later_draw_still_keeps(self):
        hand = ["南", "白", "2b", "3b", "5t", "6t", "8w", "9w", "1t", "4w", "7b"]
        act = self._act(_snap(hand, "7b"), bu_defer_enabled=1)
        self.assertEqual(act["action"], "discard")
        self.assertNotEqual(act["tile"], "南")

    def test_on_converts_to_gangkai_baotou(self):
        self.assertEqual(self._act(_snap(DONE, "7w"), bu_defer_enabled=1), {"action": "gang", "tile": "南"})

    def test_wall_deadline_bu_now(self):
        self.assertEqual(self._act(_snap(HOLD, "南", wall=24), bu_defer_enabled=1), {"action": "gang", "tile": "南"})

    def test_no_joker_bu_now(self):
        hand = ["南", "7b", "2b", "3b", "5t", "6t", "8w", "9w", "1t", "4w", "东"]
        self.assertEqual(self._act(_snap(hand, "南"), bu_defer_enabled=1), {"action": "gang", "tile": "南"})


if __name__ == "__main__":
    unittest.main()
