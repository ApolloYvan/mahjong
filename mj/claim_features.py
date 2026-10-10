"""吃碰的特征化决策：线上与 tools/discard_fit.py 的拟合共用同一份特征定义。

每个响应窗口 = 若干选项：「过」（特征全 0，得分恒为 0）+ 每一种合法的碰/吃法。
选项得分 = Σ fc_<特征> × 特征值，取最高者；所有吃法得分都 ≤ 0 就过。
后继弃牌沿用 claim_assessment 的模拟（声明后最优弃牌的向听/听口），不另写一套。

开关 rule_fitted_claim_enabled，且 **只有 weights 里存在 fc_peng（即写入了拟合结果）
时才生效**。财神不可吃碰、两摊吃上限、墙尾、抓打圈等硬规则仍由原有路径把守。
"""
from .responses import claim_assessment
from .shanten import pair_route_allowed
from .tiles import to_counts

CL_FEATURES = ("chi", "peng", "d_shanten", "d_uke", "after_bt", "after_tenpai", "tenpai", "first",
               "jokers", "late", "pair_route", "chi2", "dealer",
               # 2026-10-09 局面特征（拟合高手吃碰用；权重缺省为 0 = 不影响旧拟合）：
               "mg1", "mg2", "mid", "flat_nojoker", "flat_joker", "peng_flat", "chi_flat", "uke_late")


def option_features(hand, take, meld_groups, chi_existing, wall_remaining, is_dealer):
    """take = 从手里拿出的两张（碰为两张同种，吃为顺子的另两张）。无合法后继时返回 None。"""
    snap = {"my_hand": list(hand), "melds": [[{"kind": "x"}] * meld_groups], "seat": 0}
    a = claim_assessment(snap, take, weights={})
    if a["after_shanten"] is None:
        return None
    is_chi = len(set(take)) == 2
    f = dict.fromkeys(CL_FEATURES, 0.0)
    f["chi" if is_chi else "peng"] = 1.0
    f["d_shanten"] = float(a["after_shanten"] - a["before_shanten"])
    f["d_uke"] = float(a["after_ukeire"] - a["before_ukeire"])
    f["after_bt"] = float(bool(a.get("after_baotou")))
    f["after_tenpai"] = float(a["after_shanten"] == 0)
    f["tenpai"] = float(a["before_shanten"] == 0)
    f["first"] = float(meld_groups == 0)
    f["jokers"] = float(min(hand.count("白"), 2))
    f["late"] = float(wall_remaining < 30)
    f["pair_route"] = float(meld_groups == 0 and pair_route_allowed(tuple(to_counts(hand)), 0))
    f["chi2"] = float(is_chi and chi_existing >= 1)
    f["dealer"] = float(is_dealer)
    flat = a["after_shanten"] >= a["before_shanten"]
    f["mg1"] = float(meld_groups == 1)
    f["mg2"] = float(meld_groups >= 2)
    f["mid"] = float(30 <= wall_remaining < 60)
    f["flat_nojoker"] = float(flat and hand.count("白") == 0)
    f["flat_joker"] = float(flat and hand.count("白") > 0)
    f["peng_flat"] = float(flat and not is_chi)
    f["chi_flat"] = float(flat and is_chi)
    f["uke_late"] = f["d_uke"] * float(wall_remaining < 45)
    return f


def score(f, weights):
    return sum(weights.get("fc_" + k, 0) * v for k, v in f.items())


def applies(weights):
    return bool(weights.get("rule_fitted_claim_enabled", 0)) and "fc_peng" in weights
