"""2026-10-05 外部评审 P0：choose_action 抛异常时 play_game 改用 _safe_fallback，对局线程不退出。"""
import os
import unittest

os.environ.setdefault("MJ_WEIGHTS_NO_FILE", "1")

from mj.bot import _safe_fallback  # noqa: E402


def _snap(**kw):
    base = {"seat": 0, "turn": 0, "phase": "draw", "dealer": 0, "wall_remaining": 50, "melds": [[], [], [], []],
            "discards": [[], [], [], []], "god": {}, "drawn_tile": "9t",
            "my_hand": ["1w", "4w", "7w", "2t", "5t", "8t", "3b", "6b", "9b", "东", "南", "西", "北", "9t"]}
    base.update(kw)
    return base


class SafeFallbackTests(unittest.TestCase):
    def test_draw_discards_drawn_tile(self):
        self.assertEqual(_safe_fallback(_snap()), {"action": "discard", "tile": "9t"})

    def test_draw_hu_when_winning(self):
        hand = ["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w", "1t", "2t", "3t", "5b", "5b"]
        self.assertEqual(_safe_fallback(_snap(my_hand=hand, drawn_tile="5b"))["action"], "hu")

    def test_response_passes(self):
        self.assertEqual(_safe_fallback(_snap(phase="response_peng", turn=1, drawn_tile=""))["action"], "pass")
        self.assertEqual(_safe_fallback(_snap(phase="response_chi", turn=1, drawn_tile=""))["action"], "pass")

    def test_not_my_turn_or_broken_snapshot(self):
        self.assertIsNone(_safe_fallback(_snap(turn=2)))
        self.assertIsNone(_safe_fallback({}))


if __name__ == "__main__":
    unittest.main()
