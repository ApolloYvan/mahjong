"""弃牌小模型：在同向听层里给每个候选打分（高手模仿学习，训练见 tools/nn_train.py）。

输入 = 每个候选一行特征（下面 FEATURES，线上与 tools/nn_extract.py 共用本文件的 vector()）；
模型 = 两层 ReLU 全连接 → 1 个分数，取最高分的候选。权重在 models/nn_discard.json
（纯数字：均值/方差 + 各层 W、b），这里用纯 Python 做前向，不依赖 torch/numpy。

开关 rule_nn_discard_enabled（默认关）；只管 不在财飘链上（chain=0、piao=0）的弃牌，
与拟合打分的范围一致。模型文件缺失、特征维度对不上或任何异常 → 返回 None，调用方回落到现有打分。
"""
import json
import math
import os

from .discard_features import FD_FEATURES, features, features_joker
from .shanten import route_shanten
from .tiles import JOKER_IDX, TILE_INDEX, to_counts

MODEL_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models", "nn_discard.json")
J_EXTRA = ("bt_after", "bt_dist", "plan_gain", "mq_bt", "disc_joker", "bt_uke")
FEATURES = (FD_FEATURES + J_EXTRA
            + tuple("cnt_%d" % i for i in range(34))
            + ("suit_w", "suit_b", "suit_t", "suit_z") + tuple("rank_%d" % r for r in range(1, 10)) + ("is_joker",)
            + ("sh_0", "sh_1", "sh_2", "sh_3p")
            + ("melds", "jokers", "dealer", "wall", "opp_melds_max", "opp_melds_sum"))

_CACHE = {"mtime": None, "model": None}


def vector(tiles, discard, meld_groups, visible, weights, ctx, rules=None):
    """一个候选的特征向量（list[float]，顺序同 FEATURES）。ctx: wall / dealer / opp_melds（别家副露组数列表）。"""
    jokers = tiles.count("白")
    if jokers:
        f = features_joker(tiles, discard, meld_groups, visible, weights, rules, want_bt_uke=True)
    else:
        f = features(tiles, discard, meld_groups, visible, weights)
    out = [float(f.get(k, 0.0)) for k in FD_FEATURES + J_EXTRA]
    remaining = list(tiles)
    remaining.remove(discard)
    counts = to_counts(remaining)
    out += [float(c) for c in counts]
    idx = TILE_INDEX[discard]
    suit = [0.0] * 4
    suit[min(idx // 9, 3)] = 1.0
    rank = [0.0] * 9
    if idx < 27:
        rank[idx % 9] = 1.0
    out += suit + rank + [float(idx == JOKER_IDX)]
    sh = route_shanten(tuple(counts), meld_groups)
    out += [float(sh == 0), float(sh == 1), float(sh == 2), float(sh >= 3)]
    opp = list(ctx.get("opp_melds") or [])
    out += [float(meld_groups), float(min(jokers, 3)), float(bool(ctx.get("dealer"))),
            float(ctx.get("wall", 60)) / 100.0, float(max(opp) if opp else 0), float(sum(opp))]
    return out


def _load():
    try:
        mtime = os.path.getmtime(MODEL_PATH)
    except OSError:
        return None
    if _CACHE["mtime"] != mtime:
        try:
            with open(MODEL_PATH, encoding="utf-8") as source:
                model = json.load(source)
            if list(model.get("features") or []) != list(FEATURES):
                model = None
        except (OSError, ValueError, AttributeError):     # 写了一半 / 损坏 / 格式不对 → 当作没有模型
            model = None
        _CACHE["mtime"], _CACHE["model"] = mtime, model
    return _CACHE["model"]


def forward(model, x):
    h = [(v - m) / s for v, m, s in zip(x, model["mean"], model["std"])]
    layers = model["layers"]
    for li, layer in enumerate(layers):
        h = [sum(w * v for w, v in zip(row, h)) + b for row, b in zip(layer["W"], layer["b"])]
        if li < len(layers) - 1:
            h = [v if v > 0 else 0.0 for v in h]
    return h[0]


def applies(chain_count, piao, weights):
    return bool(weights.get("rule_nn_discard_enabled", 0)) and not chain_count and not piao and _load() is not None


def choose(tier, tiles, meld_groups, visible, weights, rules=None):
    """在同向听层 tier 里选分最高的弃牌；任何问题返回 None（调用方回落）。"""
    try:
        model = _load()
        if model is None or len(tier) < 1:
            return None
        if len(tier) == 1:
            return tier[0]
        ctx = (rules or {}).get("_ctx") or {}
        best, best_s = None, -math.inf
        for t in tier:
            s = forward(model, vector(tiles, t, meld_groups, visible, weights, ctx, rules))
            if s > best_s:
                best, best_s = t, s
        return best
    except Exception:   # noqa: BLE001 —— 线上兜底：模型出任何问题都不能卡住出牌
        return None
