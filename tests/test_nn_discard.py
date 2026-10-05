import json
import os
import random
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("MJ_WEIGHTS_NO_FILE", "1")

from mj import nn_discard  # noqa: E402
from mj.bot import choose_discard as bot_choose_discard  # noqa: E402
from mj.fit import load_weights  # noqa: E402
from mj.strategy import choose_discard  # noqa: E402

HAND = ["1w", "1w", "2b", "3b", "5t", "9t", "东", "东", "东", "南", "4w", "5w", "6w", "7b"]


def _model(pick_index):
    """线性“模型”：分数 = 某一个特征值（便于断言选择）。"""
    D = len(nn_discard.FEATURES)
    W = [[0.0] * D]
    W[0][pick_index] = 1.0
    return {"features": list(nn_discard.FEATURES), "mean": [0.0] * D, "std": [1.0] * D,
            "layers": [{"W": W, "b": [0.0]}]}


class NNDiscardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        self.tmp.close()
        self.patch = mock.patch.object(nn_discard, "MODEL_PATH", self.tmp.name)
        self.patch.start()
        nn_discard._CACHE.update(mtime=None, model=None)

    def tearDown(self):
        self.patch.stop()
        os.unlink(self.tmp.name)
        nn_discard._CACHE.update(mtime=None, model=None)

    def _write(self, model):
        with open(self.tmp.name, "w") as sink:
            json.dump(model, sink)
        os.utime(self.tmp.name, (random.random() * 1e9, random.random() * 1e9))

    def test_vector_dim(self):
        w = load_weights()
        for hand in (HAND, ["白"] + HAND[1:]):
            v = nn_discard.vector(hand, hand[1], 0, None, w, {"wall": 50, "opp_melds": [1, 0, 2]})
            self.assertEqual(len(v), len(nn_discard.FEATURES))

    def test_switch_off_or_missing_model_is_unchanged(self):
        base = choose_discard(HAND, 0, 0, 0, {}, None)
        on = {"_weights": {"rule_nn_discard_enabled": 1}}
        self.assertEqual(choose_discard(HAND, 0, 0, 0, on, None), base)        # 文件是空的 → 回落
        self._write({"features": ["x"], "mean": [0], "std": [1], "layers": []})
        self.assertEqual(choose_discard(HAND, 0, 0, 0, on, None), base)        # 特征对不上 → 回落

    def test_model_drives_choice(self):
        on = {"_weights": {"rule_nn_discard_enabled": 1}}
        # 只看「打出的是 万 子」的模型：同向听层里有万子时必然选万子，否则回到层内第一个最高分
        self._write(_model(nn_discard.FEATURES.index("suit_w")))
        hand = ["1w", "9w", "1b", "9b", "1t", "9t", "东", "南", "西", "北", "中", "发", "3b", "7t"]
        pick = choose_discard(hand, 0, 0, 0, on, None)
        self.assertTrue(pick.endswith("w"), pick)
        self._write(_model(nn_discard.FEATURES.index("suit_z")))
        self.assertIn(choose_discard(hand, 0, 0, 0, on, None), ("东", "南", "西", "北", "中", "发"))

    def test_bot_passes_context(self):
        seen = {}
        real = nn_discard.choose

        def spy(tier, tiles, meld_groups, visible, weights, rules=None):
            seen.update((rules or {}).get("_ctx") or {})
            return real(tier, tiles, meld_groups, visible, weights, rules)

        snap = {"my_hand": HAND, "seat": 1, "dealer": 0, "wall_remaining": 44, "phase": "draw", "turn": 1,
                "melds": [[{"kind": "peng", "tiles": ["9b"] * 3}], [], [], []], "discards": []}
        self._write(_model(0))
        with mock.patch.object(nn_discard, "choose", spy), \
                mock.patch("mj.strategy.load_weights", lambda: {**load_weights(), "rule_nn_discard_enabled": 1}):
            bot_choose_discard(snap)
        self.assertEqual(seen.get("wall"), 44)
        self.assertEqual(seen.get("opp_melds"), [1, 0, 0])
        self.assertFalse(seen.get("dealer"))


if __name__ == "__main__":
    unittest.main()
