"""2026-10-07：E2（差一步转爆头的弃胡）早巡收窄——墙剩 −20 ≥ s1_one_step_wall_min 且副露 ≤ s1_one_step_max_melds 才弃胡。"""
import os
import unittest

os.environ.setdefault("MJ_WEIGHTS_NO_FILE", "1")

from mj import bot, hu_strategy  # noqa: E402
from mj.fit import load_weights  # noqa: E402

# 高手真实弃胡局面（a_114ad564d476 第 3 局，2 财神 1 露，摸白能胡平胡、不能直接转爆头）
HAND = "2b 3b 4t 5b 5t 5w 5w 6b 7b 白 白".split()


def _snap(wall):
    return {"my_hand": list(HAND), "drawn_tile": "白", "seat": 0, "dealer": 1, "round_no": 2, "phase": "draw",
            "melds": [[{"kind": "peng", "tiles": ["9w"] * 3}], [], [], []], "wall_remaining": wall}


class OneStepEarly(unittest.TestCase):
    def _decline(self, wall, **extra):
        w = {**load_weights(), "rule_decline_joker_hold_enabled": 1, "s1_one_step_enabled": 1, **extra}
        snap = _snap(wall)
        return hu_strategy._decline_small_hu_tile(snap, bot.hu_result(snap), w)

    def test_unrestricted_e2_declines(self):
        self.assertIsNotNone(self._decline(64))

    def test_early_wall_gate(self):
        self.assertIsNotNone(self._decline(64, s1_one_step_wall_min=34))
        self.assertIsNone(self._decline(50, s1_one_step_wall_min=34))

    def test_meld_gate(self):
        self.assertIsNone(self._decline(64, s1_one_step_max_melds=0))
        self.assertIsNotNone(self._decline(64, s1_one_step_max_melds=1))


if __name__ == "__main__":
    unittest.main()
