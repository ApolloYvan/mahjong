import os
import unittest
from unittest import mock

os.environ.setdefault("MJ_WEIGHTS_NO_FILE", "1")

from mj import responses  # noqa: E402
from mj.fit import load_weights  # noqa: E402
from mj.responses import choose_gang, gang_hurts_shape, response_gang_take  # noqa: E402


def _on():
    w = dict(load_weights())
    w["rule_gang_no_hurt_enabled"] = 1
    return w


class GangGateTests(unittest.TestCase):
    # 3w 用在 234w/345w 顺子里：4 张 3w 暗杠会拆牌
    HURT = ["2w", "3w", "3w", "3w", "3w", "4w", "5w", "7b", "8b", "9b", "1t", "1t", "5t", "6t"]
    # 东 孤立四张：杠了不伤
    FREE = ["东", "东", "东", "东", "2w", "3w", "4w", "7b", "8b", "9b", "1t", "1t", "5t", "6t"]

    def test_shape(self):
        self.assertTrue(gang_hurts_shape(self.HURT, "3w", "an", 0))
        self.assertFalse(gang_hurts_shape(self.FREE, "东", "an", 0))

    def test_switch_off_keeps_old_behavior(self):
        snap = {"my_hand": self.HURT, "drawn_tile": "3w", "wall_remaining": 60, "melds": [], "seat": 0}
        self.assertEqual(choose_gang(snap), {"action": "gang", "tile": "3w"})

    def test_switch_on(self):
        with mock.patch.object(responses, "load_weights", _on):
            hurt = {"my_hand": self.HURT, "drawn_tile": "3w", "wall_remaining": 60, "melds": [], "seat": 0}
            free = {"my_hand": self.FREE, "drawn_tile": "东", "wall_remaining": 60, "melds": [], "seat": 0}
            self.assertIsNone(choose_gang(hurt))
            self.assertEqual(choose_gang(free), {"action": "gang", "tile": "东"})

    def test_ming(self):
        # 3w 同时是 345w 的一部分：明杠拿走三张 3w 会拆掉搭子
        hand = ["1w", "3b", "3t", "3w", "3w", "3w", "4w", "5b", "5t", "5w", "7t", "9t", "9w"]
        snap = {"phase": "response_peng", "seat": 1, "responding_seats": [1], "my_hand": hand,
                "window_tile": "3w", "wall_remaining": 60, "melds": []}
        self.assertEqual(response_gang_take(snap), {"action": "gang", "tile": "3w"})
        with mock.patch.object(responses, "load_weights", _on):
            self.assertIsNone(response_gang_take(snap))


if __name__ == "__main__":
    unittest.main()
