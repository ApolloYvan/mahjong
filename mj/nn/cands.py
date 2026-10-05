"""S2'：生产出牌的前 K 个候选（只读）+ 逐候选特征。纯标准库，实战要用。

生产出牌是一串叠加覆盖（``mj/strategy.py::choose_discard``：同向听层里按拟合特征/财神计划/基础分选；route EV、速度前瞻
再叠加覆盖），没有"每张牌一个总分"。这里给出一个**只读**的候选排序（不改任何生产行为）：

- 候选池 = 手牌里的各种牌；打出后 ``route_shanten``（生产分层用的同一个向听口径）最小的那一层（tier）按生产的基础打分函数
  排序（``discard_features.score`` / ``score_joker`` / ``strategy._discard_score``，选哪个的条件同 ``strategy.choose_discard._original``）；
- 层外的牌用"向听差 x 1000 + 一个廉价的孤立度"排在后面；
- **生产实际选择（P）强制排第一**（覆盖逻辑选出的牌可能不在 tier 里，所以不能只靠基础打分排序）；
- 返回前 K 个：``[{"tile","score","rank","shanten","in_tier"}]``，``rank==0`` 一定是 P。
``score`` 是"相对分"（越大越好，P 比第二名至少高 1），不同局面间不可比，特征里用的是差值。

``candidate_features`` 给每个候选一个定长向量（见 ``FEATURE_NAMES``）：生产排名/分差、``mj.nn_discard.vector``（生产已有的弃牌特征，
76 维）、打出后的离胡距离/是否爆头/有效进张（种数和按场上剩余张数加权）、是否打财神、该牌可见张数、以及局面特征（财神数、副露数、
墙剩余、庄闲、局号、分数差、各对手副露数和已打牌数=出牌速度）。
"""
from .. import discard_features, nn_discard, strategy
from ..fit import load_weights
from ..mc.fast import hu_distance, is_baotou
from ..shanten import route_shanten, ukeire
from ..tiles import INDEX_TILE, JOKER, JOKER_IDX, NSUITS, TILE_INDEX, to_counts

K_DEFAULT = 5
TIER_GAP = 1000.0
_EXTRA = ("rank0", "rank1", "rank2", "rank3", "rank4", "gap_to_best", "is_prod", "in_tier", "shanten_after",
          "dist_after", "baotou_after", "imp_types", "imp_live", "is_joker", "tile_visible",
          "ctx_jokers", "ctx_melds", "ctx_wall", "ctx_dealer", "ctx_round", "ctx_score_diff",
          "ctx_opp_meld0", "ctx_opp_meld1", "ctx_opp_meld2", "ctx_opp_disc0", "ctx_opp_disc1", "ctx_opp_disc2")
FEATURE_NAMES = _EXTRA + tuple("nd_%s" % f for f in nn_discard.FEATURES)
FEATURE_DIM = len(FEATURE_NAMES)


def _base_scorer(tiles, meld_groups, chain_count, piao, rules, weights, visible):
    if discard_features.applies(tiles, chain_count, piao, weights):
        return lambda t: discard_features.score(tiles, t, meld_groups, visible, weights, rules)
    if discard_features.applies_joker(tiles, chain_count, piao, weights):
        return lambda t: discard_features.score_joker(tiles, t, meld_groups, visible, weights, rules)
    return lambda t: strategy._discard_score(tiles, t, meld_groups, chain_count, piao, rules, weights, visible)


def production_candidates(snapshot, prod_tile=None, k=K_DEFAULT, rules=None):
    """前 K 个候选；``prod_tile`` = 生产已经选好的弃牌（给了就不重算）。没有手牌/不是摸牌出牌时返回 []。"""
    from .. import bot   # 延迟导入：bot 在开关打开时才 import 本模块
    inp = bot._discard_inputs(snapshot, rules)
    if inp is None:
        return []
    hand, meld_groups, chain_count, piao, rules2, visible, _use_route = inp
    if prod_tile is None:
        act = bot.choose_discard(snapshot, rules)
        prod_tile = act["tile"] if act else None
    if prod_tile is None or prod_tile not in hand:
        return []
    weights = strategy._effective_weights(rules2)
    unique = sorted(set(hand))
    shan = {}
    for t in unique:
        rem = list(hand)
        rem.remove(t)
        shan[t] = route_shanten(to_counts(rem), meld_groups)
    best = min(shan.values())
    tier = [t for t in unique if shan[t] == best]
    scorer = _base_scorer(hand, meld_groups, chain_count, piao, rules2, weights, visible)
    scores = {t: scorer(t) for t in tier}
    floor = min(scores.values())
    for t in unique:
        if t not in scores:
            cnt = hand.count(t)
            scores[t] = floor - TIER_GAP * (shan[t] - best) - 10.0 * cnt   # 层外：向听差为主，成对/成刻的牌更不该打
    top = max(scores.values())
    if scores.get(prod_tile, -1e18) < top + 1.0:
        scores[prod_tile] = top + 1.0      # 生产实际选择排第一
    order = sorted(unique, key=lambda t: -scores[t])[:k]
    return [{"tile": t, "score": scores[t], "rank": i, "shanten": shan[t], "in_tier": t in tier}
            for i, t in enumerate(order)]


def _improving(counts13, mg, visible):
    """有效进张：``mj.shanten.ukeire``（生产在用的、会降低向听的牌种集合，带缓存）：种数 + 按场上剩余张数加权。"""
    waits = [i for i in ukeire(counts13, mg) if i != JOKER_IDX]
    return len(waits), float(sum(max(0, 4 - visible[i]) for i in waits))


def candidate_features(snapshot, cands, ctx=None, rules=None, skip_prod=False, deadline=None):
    """每个候选一个向量（顺序 ``FEATURE_NAMES``）。``skip_prod``：不算生产选择 P 的特征（优势模型里 P 恒为 0，用不到）。
    ``deadline``（``time.perf_counter()`` 的绝对时刻）：到点就停，返回已算出的前几个（P 占位 None 不算耗时）——
    nn_discard 特征在多财神手牌上单个候选可到 ~25ms（性能核）/ ~100ms（能效核），不设上限会拖慢出牌。"""
    import time as _time
    from .. import bot
    hand, meld_groups, chain_count, piao, rules2, visible, _ = bot._discard_inputs(snapshot, rules)
    weights = load_weights()
    me = snapshot.get("seat", 0)
    melds = snapshot.get("melds") or [[], [], [], []]
    discards = snapshot.get("discards") or [[], [], [], []]
    scores = snapshot.get("scores") or [0, 0, 0, 0]
    opp = [s for s in range(4) if s != me]
    best_score = cands[0]["score"] if cands else 0.0
    out = []
    for c in cands:
        if skip_prod and c["rank"] == 0:
            out.append(None)
            continue
        if deadline is not None and out and _time.perf_counter() > deadline:
            break
        t = c["tile"]
        rem = list(hand)
        rem.remove(t)
        counts = to_counts(rem)
        dist = hu_distance(counts, meld_groups)
        types, live = _improving(counts, meld_groups, visible)
        k = min(c["rank"], 4)
        vec = [1.0 if i == k else 0.0 for i in range(5)]
        vec += [max(-5.0, (c["score"] - best_score) / 1000.0), 1.0 if c["rank"] == 0 else 0.0,
                1.0 if c["in_tier"] else 0.0, min(c["shanten"], 4) / 4.0,
                min(dist, 6) / 6.0, 1.0 if is_baotou(counts, meld_groups) else 0.0, types / 34.0, live / 40.0,
                1.0 if t == JOKER else 0.0, visible[TILE_INDEX[t]] / 4.0,
                counts[JOKER_IDX] / 4.0, meld_groups / 4.0, (snapshot.get("wall_remaining") or 0) / 136.0,
                1.0 if snapshot.get("dealer") == me else 0.0, (snapshot.get("round_no") or 1) / 8.0,
                (scores[me] - sum(scores[s] for s in opp) / 3.0) / 100.0]
        vec += [len(melds[s]) / 4.0 for s in opp]
        vec += [len(discards[s]) / 20.0 for s in opp]
        ctx_nd = rules2["_ctx"]
        vec += [float(x) for x in nn_discard.vector(hand, t, meld_groups, visible, weights, ctx_nd, rules2)]
        out.append(vec)
    return out
