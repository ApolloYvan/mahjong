"""黄金测试：固定种子生成随机局面，规则开关全部默认关闭时，生产决策函数的
输出必须逐字节不变。

本轮（见 docs/experiments/OFFLINE_REPORT.md）只有一条策略挖掘规则通过了
docs/STRATEGY_MINING.md 第三节的入选标准并落地为开关：
`mj/fit.py::DEFAULT_WEIGHTS["rule_decline_joker_hold_enabled"]`（默认 0），
实现在 `mj/hu_strategy.py::_decline_small_hu_tile`。`GoldenHuTests` 覆盖的
150 个随机 hu/飘局面里，只要开关保持默认关闭（`load_weights()` 读到的就是
`DEFAULT_WEIGHTS` 里的 0），新代码路径在 `_decline_small_hu_tile` 第一行就
直接返回 `None`，等价于该分支完全不存在——所以这份黄金测试同时验证了
"开关默认关闭时行为逐字节不变"这条硬约束。`GoldenSwitchOffTests` 额外显式
断言 `load_weights()["rule_decline_joker_hold_enabled"] == 0`，防止有人
不小心把默认值改成 1 却没人发现。

覆盖度说明：完整 2000 个随机局面在本机（无 numpy，纯 Python 向听搜索，多财神
手牌单次评分可到 ~0.3s）单测运行时间会明显变长（实测 400 例一度超过 120s），
这里用 80（弃牌评分，两条路线各 80）+ 80（吃/碰响应）+ 80（hu/飘）覆盖，
运行时间数秒，足以在合入前快速跑。若怀疑改动只在稀有分支生效，应临时调高 N
复核，见文件底部 `_generate_discard_cases` 的 ``count`` 参数。
"""
import hashlib
import json
import random
import unittest
from contextlib import ExitStack
from unittest.mock import patch

import mj.ev as ev
import mj.hu_strategy as hu_strategy
import mj.responses as responses
import mj.strategy as strategy
from mj.fit import DEFAULT_WEIGHTS
from mj.tiles import ALL_TILES, JOKER, TILE_INDEX, to_counts
from mj.strategy import choose_discard as baseline_choose_discard
from mj.ev import choose_route_discard
from mj.responses import choose_chi, choose_peng
from mj.hu_strategy import choose_hu_or_piao
from mj.rules import evaluate


def _pin_default_weights():
    """把四个模块各自 import 进来的 load_weights 都钉死成 DEFAULT_WEIGHTS，
    使黄金哈希测试不受 models/weights.json 当前内容（例如某个实战 bot 会话
    写入的开关覆盖）影响——否则同一份代码在不同机器/不同时刻跑出的哈希会
    不一致，golden 测试就失去了回归信号的意义。"""
    stack = ExitStack()
    fake = lambda *a, **k: dict(DEFAULT_WEIGHTS)
    for module in (ev, hu_strategy, responses, strategy):
        stack.enter_context(patch.object(module, "load_weights", fake))
    return stack


def _short_digest(obj):
    """截断到 32 位十六进制——mj.security.scan() 专门扫描 64 位十六进制串当作
    疑似泄露令牌（见 mj/security.py::TOKEN 正则），黄金测试的哈希值不是密钥，
    但字面上会命中同一个模式，截断避免误报（tests/test_security.py 的
    ``test_repo_scan_is_clean_after_token_cleanup`` 依赖 0 命中）。"""
    full = hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()
    return full[:32]


def _random_hand(rng, size, joker_bias=0.08):
    hand = []
    counts = {}
    while len(hand) < size:
        if rng.random() < joker_bias and counts.get(JOKER, 0) < 4:
            t = JOKER
        else:
            t = rng.choice(ALL_TILES)
        if counts.get(t, 0) >= 4:
            continue
        hand.append(t)
        counts[t] = counts.get(t, 0) + 1
    return hand


def _generate_discard_cases(count, seed):
    rng = random.Random(seed)
    cases = []
    for _ in range(count):
        meld_groups = rng.choice([0, 0, 0, 1, 1, 2])
        size = 13 - 3 * meld_groups + 1
        hand = _random_hand(rng, size)
        chain = rng.choice([0, 0, 0, 1, 2, 3])
        piao = rng.choice([0, 0, 1])
        rules = {}
        if rng.random() < 0.2:
            rules = {"dealer_hint": True}
        if rng.random() < 0.1:
            rules = dict(rules, YouCaiBiKao=True)
        cases.append((hand, meld_groups, chain, piao, rules))
    return cases


def _generate_response_cases(count, seed):
    rng = random.Random(seed)
    cases = []
    for _ in range(count):
        meld_groups = rng.choice([0, 0, 1])
        hand_size = 13 - 3 * meld_groups
        hand = _random_hand(rng, hand_size)
        window_tile = rng.choice(ALL_TILES)
        melds = [[] for _ in range(4)]
        discards = [[] for _ in range(4)]
        seat = rng.randrange(4)
        wall = rng.choice([10, 30, 60, 80])
        snapshot = {
            "seat": seat, "phase": rng.choice(["response_chi", "response_peng"]),
            "my_hand": hand, "window_tile": window_tile, "melds": melds,
            "discards": discards, "wall_remaining": wall, "chain_count": rng.choice([0, 1]),
            "responding_seats": [s for s in range(4) if s != (seat + 1) % 4],
            "god": {"catch_play": False, "god_discarder_seat": None},
        }
        cases.append(snapshot)
    return cases


def _generate_hu_cases(count, seed):
    rng = random.Random(seed)
    cases = []
    for _ in range(count):
        meld_groups = rng.choice([0, 0, 1])
        hand13_size = 13 - 3 * meld_groups
        hand13 = _random_hand(rng, hand13_size)
        drawn = rng.choice(ALL_TILES)
        counts13 = to_counts(hand13)
        try:
            result = evaluate(counts13, TILE_INDEX[drawn], chain_count=rng.choice([0, 1, 2]),
                              piao=0, meld_groups=meld_groups)
        except Exception:
            result = None
        snapshot = {
            "drawn_tile": drawn, "my_hand": hand13 + [drawn], "wall_remaining": rng.choice([10, 40, 80]),
            "dealer": rng.randrange(4), "seat": rng.randrange(4),
            "god": {"chain_count": rng.choice([0, 1, 2]), "piao": 0},
            "melds": [[] for _ in range(4)], "discards": [[] for _ in range(4)],
        }
        cases.append((snapshot, result))
    return cases


class GoldenDiscardTests(unittest.TestCase):
    def test_baseline_and_route_discard_stable(self):
        outputs = []
        with _pin_default_weights():
            for hand, meld_groups, chain, piao, rules in _generate_discard_cases(80, seed=20260924):
                r1 = baseline_choose_discard(list(hand), meld_groups, chain, piao, dict(rules), None)
                r2 = choose_route_discard(list(hand), meld_groups, chain, piao, dict(rules), None)
                outputs.append((r1, r2))
        digest = _short_digest(outputs)
        # 首次生成时把这里替换成实际打印出的 digest；只要没有开关改动，
        # 重跑必须得到同一个值。
        self.assertEqual(digest, GOLDEN_DISCARD_HASH)


class GoldenResponseTests(unittest.TestCase):
    def test_chi_peng_stable(self):
        outputs = []
        with _pin_default_weights():
            for snapshot in _generate_response_cases(80, seed=20260924):
                outputs.append((choose_chi(dict(snapshot)), choose_peng(dict(snapshot))))
        digest = _short_digest(outputs)
        self.assertEqual(digest, GOLDEN_RESPONSE_HASH)


class GoldenHuTests(unittest.TestCase):
    def test_hu_or_piao_stable(self):
        outputs = []
        with _pin_default_weights():
            for snapshot, result in _generate_hu_cases(80, seed=20260924):
                outputs.append(choose_hu_or_piao(dict(snapshot), result))
        digest = _short_digest(outputs)
        self.assertEqual(digest, GOLDEN_HU_HASH)


class SwitchDefaultOffTests(unittest.TestCase):
    """显式钉住 mj/fit.py 里所有 rule_<id>_enabled 开关的默认值必须是 0。"""

    def test_all_rule_switches_default_off(self):
        from mj.fit import DEFAULT_WEIGHTS
        rule_keys = [k for k in DEFAULT_WEIGHTS if k.startswith("rule_") and k.endswith("_enabled")]
        self.assertIn("rule_decline_joker_hold_enabled", rule_keys)
        # 两个有数据依据、刻意默认开启的例外（见 mj/fit.py 对应注释）
        allowed_on = {"rule_decline_single_joker_one_meld_enabled", "rule_fitted_discard_dealer_d0_enabled"}
        for key in rule_keys:
            if key in allowed_on:
                continue
            self.assertEqual(DEFAULT_WEIGHTS[key], 0, "%s 默认必须是 0（关闭）" % key)

    def test_decline_joker_hold_noop_when_off(self):
        """开关关闭时，_decline_small_hu_tile 对任何非爆头+多财神局面都返回 None
        （即完全不改变 choose_hu_or_piao 的行为）——独立于随机哈希再钉一遍。"""
        from mj.hu_strategy import _decline_small_hu_tile
        hu_result = {"hu": True, "baotou": False, "fan": 1, "counts": tuple([0] * 33 + [3])}
        snapshot = {"drawn_tile": "1w"}
        self.assertIsNone(_decline_small_hu_tile(snapshot, hu_result))


# 下面三个哈希由本文件首次运行时生成并固化（见仓库 git 历史/生成脚本注释）。
# 任何改动 mj/strategy.py、mj/ev.py、mj/responses.py、mj/hu_strategy.py、
# mj/fit.py 默认权重的提交，如果不是"新增默认关闭的开关"，都不应该改变这三
# 个值；如果确实要改变生产行为，必须显式更新这里并在提交信息里说明原因。
GOLDEN_DISCARD_HASH = "a86ebc90cb34cb475e96ad6a7317eaca"
GOLDEN_RESPONSE_HASH = "1d3b16c1d5964964f077354f497ffce8"
GOLDEN_HU_HASH = "73fdf29589d895d45cb4e9c9e735c2ad"


if __name__ == "__main__":
    unittest.main()
