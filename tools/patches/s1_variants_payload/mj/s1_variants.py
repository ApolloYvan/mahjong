"""S1（弃胡转爆头）的放宽变体（实验开关，默认全部关闭；``mj/hu_strategy.py::_decline_small_hu_tile`` 里挂钩）。

依据（tools/s1_split.py，实战 939 次触发）：S1 最终胡率 82~85%，远高于盈亏平衡成功率 ~57%（庄家每次 +17.5 分，闲家 +6.9 分）——
现行触发条件偏保守。每个变体一个开关，默认关闭，默认值时行为逐字节不变（tests/test_s1_variants.py）：

E1 ``s1_wall_relax_enabled``：墙尾门槛放宽。现行"墙剩余 - 20 < 8 不弃胡"，改为 ``s1_wall_relax_min``（默认 4）。
E2 ``s1_one_step_enabled``：允许"差一步转爆头"的弃胡。有效财神 >= 2、没有能直接转成爆头的出牌，但存在打出后"再进一张就能转爆头"的出牌，
   且这张牌的有效进张（按场上剩余张数加权）>= ``s1_one_step_min_ukeire``（默认 8）时弃胡，打有效进张最多的那张。
E3 ``s1_ev_enabled``：按期望值决定弃胡，替换"财神数 x 副露数"的触发格子。候选 = 能直接转爆头的出牌（同现行）；用实战触发记录拟合的
   等待成功率 p̂（models/s1_phat.json：按财神数、副露数、墙剩余分格 + 层级收缩）与盈亏平衡成功率 p*（庄/闲分别算）比较，
   p̂ > p* + ``s1_ev_margin``（默认 0.1）时弃胡。p* = (G_now + L) / (G_win + L)，G_now = payout(f)，G_win = payout(ratio*f)，
   L = 对手自摸时我们的付出（模型文件里按庄/闲给）。文件缺失/加载失败 -> 不弃胡（回落直接胡）。
   E3 需要 S1 总开关 ``rule_decline_joker_hold_enabled`` 为 1（它是 S1 的前提），不依赖 S1 的格子表，所以也覆盖现行没涵盖的局面。
   关于"有效进张"：E3 的候选弃完就是爆头（摸任意牌都胡），进张不构成区分，所以不进 p̂ 的分格；E2 的候选才用有效进张。
"""
import json
import os
import threading

from .rules import baotou, payout
from .shanten import combined_route
from .tiles import JOKER, JOKER_IDX, NSUITS, TILE_INDEX, to_counts

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PHAT_PATH = os.environ.get("MJ_S1_PHAT_PATH") or os.path.join(ROOT, "models", "s1_phat.json")
_LOCK = threading.Lock()
_CACHE = {"path": None, "mtime": None, "model": None}


def wall_min(weights):
    """E1：墙尾门槛（``wall_remaining - 20`` 小于它就不弃胡）。关闭时是现行的 8。"""
    if weights.get("s1_wall_relax_enabled", 0):
        return weights.get("s1_wall_relax_min", 4)
    return 8


# ---------------------------------------------------------------- E3：p̂ 模型

def wall_bin(wall_remaining):
    """还能摸几轮：(墙剩余-20)//8，封顶 5。"""
    return max(0, min(5, ((wall_remaining or 0) - 20) // 8))


def _keys(jokers, melds, wbin):
    j = min(jokers, 2)
    m = min(melds, 2)
    return ("g", "j%d" % j, "j%dm%d" % (j, m), "j%dm%dw%d" % (j, m, wbin))


class PhatModel:
    def __init__(self, obj):
        self.levels = obj["levels"]
        self.k = float(obj.get("shrink_k", 10.0))
        self.L = obj["L"]
        self.ratio = float(obj.get("ratio", 2.0))

    def p_hat(self, jokers, melds, wbin):
        keys = _keys(jokers, melds, wbin)
        w0, n0 = self.levels["g"]
        p = (w0 + 0.5) / (n0 + 1.0)
        for key in keys[1:]:
            w, n = self.levels.get(key, (0, 0))
            p = (w + self.k * p) / (n + self.k)       # 子格向父格收缩
        return p

    def p_star(self, fan, dealer):
        L = self.L["dealer" if dealer else "non"]
        g_now = payout(fan, dealer=dealer)
        g_win = payout(fan * self.ratio, dealer=dealer)
        return (g_now + L) / (g_win + L)


def load_model():
    path = os.environ.get("MJ_S1_PHAT_PATH") or PHAT_PATH
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    with _LOCK:
        if _CACHE["path"] == path and _CACHE["mtime"] == mtime:
            return _CACHE["model"]
        try:
            with open(path, encoding="utf-8") as f:
                model = PhatModel(json.load(f))
        except Exception:   # noqa: BLE001 —— 模型坏了 = 没有模型：不弃胡
            model = None
        _CACHE.update(path=path, mtime=mtime, model=model)
        return model


def ev_choice(snapshot, hu_result, weights, decline_choice):
    """E3：返回要弃的牌或 None。``decline_choice`` = ``hu_strategy._decline_discard_choice``（避免循环导入由调用方传入）。"""
    if snapshot.get("wall_remaining", 40) - 20 < 4:     # 弃了之后至少还得轮到自己再摸一次
        return None
    counts = hu_result.get("counts")
    if not counts:
        return None
    model = load_model()
    if model is None:
        return None
    tile = decline_choice(snapshot, hu_result)
    if tile is None:
        return None
    seat = snapshot.get("seat", -1)
    melds = snapshot.get("melds") or []
    n_melds = len(melds[seat]) if isinstance(melds, list) and len(melds) == 4 and isinstance(seat, int) and 0 <= seat < 4 else 0
    dealer = snapshot.get("dealer") is not None and snapshot.get("dealer") == seat
    fan = max(1, hu_result.get("fan", 1))
    p_hat = model.p_hat(counts[JOKER_IDX], n_melds, wall_bin(snapshot.get("wall_remaining")))
    if p_hat > model.p_star(fan, dealer) + weights.get("s1_ev_margin", 0.1):
        return tile
    return None


# ---------------------------------------------------------------- E2：差一步转爆头

def _unseen(snapshot):
    from .tiles import visible_counts
    hand = snapshot.get("my_hand") or []
    return visible_counts(hand, snapshot.get("discards") or [], snapshot.get("melds") or [])


def one_step_ukeire(counts13, meld_groups, visible):
    """打出之后的 13 张：若已经爆头返回 None（那是直接转，不归 E2）；否则返回"再摸进一张就能转成爆头"的牌的
    有效进张——按场上剩余张数（4 - 可见）加权求和。摸进 u 之后要能打掉某一张得到爆头形（``rules.baotou``）。"""
    if baotou(tuple(counts13), meld_groups):
        return None
    live = 0
    for u in range(NSUITS):
        if u == JOKER_IDX or counts13[u] >= 4 or visible[u] >= 4:
            continue
        c = list(counts13)
        c[u] += 1
        ok = False
        for d in range(NSUITS):
            if d == u or not c[d]:
                continue          # 打掉刚摸进的 u 等于没摸，不会比 counts13 更好
            c[d] -= 1
            if baotou(tuple(c), meld_groups):
                ok = True
            c[d] += 1
            if ok:
                break
        if ok:
            live += 4 - visible[u]
    return live


def one_step_choice(snapshot, hu_result, weights, meld_groups):
    """E2：返回要弃的牌或 None（有效财神 >= 2、存在差一步转爆头且有效进张 >= N 的出牌时）。"""
    counts = hu_result.get("counts")
    if not counts or counts[JOKER_IDX] < 2:
        return None
    min_ukeire = weights.get("s1_one_step_min_ukeire", 8)
    hand = list(snapshot.get("my_hand") or [])
    visible = _unseen(snapshot)
    best_tile, best_key = None, None
    for cand in sorted(set(t for t in hand if t != JOKER)):
        left = list(hand)
        left.remove(cand)
        counts_left = to_counts(left)
        live = one_step_ukeire(counts_left, meld_groups, visible)
        if live is None or live < min_ukeire:
            continue
        s, waits = combined_route(counts_left, meld_groups)
        key = (-live, s, -len(waits))
        if best_key is None or key < best_key:
            best_key, best_tile = key, cand
    return best_tile
