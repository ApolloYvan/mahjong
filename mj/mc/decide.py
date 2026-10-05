"""阶段三：实时 MC 出牌/胡飘决策 —— ``mc_override(snapshot, action, rules, ctx)``。

2026-10-03 起实时路径改用 ``mj.mc.slim``（计数数组状态机，逐局对照 RoundEngine
0 不一致）+ 校准好的对手参数和胡牌率补足表（``models/mc_opp_params.json``，
``tools/mc_check.py calibrate`` 产物），不再用 ``RoundEngine``+``GreedyPolicy``。
没有校准文件时**拒绝**推演（回落生产选择，原因 ``no_calibration``）——未校准的
对手偏慢，会让 MC 系统性高估"慢而大"的路线。

======================================================================
三个开关（``load_weights()`` 读取，默认全 0，``weights_overlay`` 可按座位覆盖）
======================================================================
- ``mc_hu_enabled``：胡 / 飘 / 弃胡 的决策（手牌已构成胡牌、或生产要自由飘财神）；
- ``mc_discard_enabled``：只对**手持财神**的出牌决策；
- ``mc_menqing_enabled``：门清出牌（无副露且 ``pair_shanten<=3``，口径同
  ``tools/mc_offline.py::_classify`` 的 ``menqing_route``）。
三个都是 0 时 ``mc_override`` 第一件事就是读开关然后 ``return None``，不读快照任何字段、
不跑任何推演（``tests/test_mc_decide.py::TestDisabledIsByteIdentical``）。键没有加进
``mj.fit.DEFAULT_WEIGHTS``（不改 ``load_weights()`` 的返回值），用 ``.get(key, 0)`` 读。

======================================================================
方法（沿用 tools/mc_offline.py 的离线口径，换成能在 1s 内跑的两阶段）
======================================================================
1. 候选：生产选择 + 胡牌时的"胡/飘财神/打刚摸的牌" + 向听距离最优的 ``mc_topk`` 张。
2. **选择阶段**（pilot，少量局数）：每个候选 x 每条路线（R_A 普通/R_B 爆头/R_C 七对/
   R_D 七对爆头）各推演，选出每个候选的最优路线，并挑出除生产选择外 pilot 均值最高的
   **一个**备选。
3. **检验阶段**（独立的另一批牌局）：只对 生产选择 vs 备选 配对推演（同一副补全牌局、
   同一个随机数种子），备选相对生产的配对差 > ``mc_z`` 倍标准误才推翻。选择和检验用
   不同的牌局，避免"在同一批样本上挑最大值又拿它当证据"。
4. 价值 = 我方得分增量 + 连庄价值 ``V(局号)``（``models/dealer_value.json``）。
5. 对手（非我方座位）按校准好的策略参数走，摸牌后没胡再按补足表触发"虚拟自摸"；我方座位
   从不补足。

======================================================================
预算与回落
======================================================================
``ctx["t0"]``（``time.monotonic()``，bot 拿到快照时打的点）+ ``cap_discard_hu_s``（1.0s）
是截止时间；检验阶段到点即停。回落（返回 None，调用方用生产选择）的原因都记在
``ctx["mc_log"]["fallback_reason"]``：``no_calibration`` / ``timeout``（选择阶段没跑完）/
``insufficient_n``（检验阶段局数 < ``mc_min_n``）/ ``production_illegal`` / ``no_alternative``
/ ``not_significant`` / ``exception:...``。arena 模式 ``ctx["fixed_n"]`` 给定总推演局数
（默认 128，pilot = 1/4），不看时间，随机数种子由 ``ctx["seed"]``（对局种子+决策序号）决定，可复现。

信息缺口（快照里没有，bot 自己按局记录，经 ``ctx`` 传入）：``piao_count``（本座位当前链里
自由飘出的财神数，"4个白板"计番要用）、``chain_has_gang``（当前链里是否含杠）。
"""
import hashlib
import json
import os
import random
import time

from ..fit import load_weights
from ..shanten import pair_shanten
from ..sim.engine import IllegalActionError
from ..tiles import JOKER, JOKER_IDX, TILE_INDEX, to_counts
from .determinize import determinize_batch
from .fast import hu_distance, is_hu
from .slim import Slim, play_out

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# MJ_MC_OPP_PARAMS：冒烟/测试用，指向 `mc_check calibrate --quick` 的产物；正式对局不设，用 models/mc_opp_params.json
PARAMS_PATH = os.environ.get("MJ_MC_OPP_PARAMS") or os.path.join(ROOT, "models", "mc_opp_params.json")
TOTAL_TILES = 136
DEAL_SIZE = 13

DEFAULT_CONFIG = {
    "mc_z": 1.0,
    "mc_min_n": 64,           # 检验阶段（不含 pilot）至少这么多局才有资格推翻
    "mc_topk": 2,             # 向听距离最优的备选出牌张数（另外还有胡/飘/打刚摸的牌）
    "mc_pilot_time": 6,       # 时间模式下选择阶段局数
    "batch_size": 16,
    # 阶段一实测调整：能效核比性能核慢 3.8 倍，出牌/胡飘预算上限 1.0s（用户定的预算线）。
    "cap_discard_hu_s": 1.0,
    "mc_fixed_n": 128,        # arena 模式：不按时间停，固定推演次数（由调用方通过 ctx["fixed_n"] 传入）
}
_CONFIG = dict(DEFAULT_CONFIG)


def set_config(**kwargs):
    """测试/调参用：直接改模块级字典，单线程调用方自己负责。"""
    _CONFIG.update(kwargs)


def get_config():
    return dict(_CONFIG)


_DEALER_VALUE = None


def _dealer_value(round_no):
    global _DEALER_VALUE
    if _DEALER_VALUE is None:
        try:
            with open(os.path.join(ROOT, "models", "dealer_value.json"), encoding="utf-8") as f:
                _DEALER_VALUE = json.load(f).get("V") or {}
        except (OSError, ValueError):
            _DEALER_VALUE = {}
    return float(_DEALER_VALUE.get(str(round_no), 0.0))


_OPP = {"loaded": False, "params": None, "ok": False}


def load_opp_params(path=None):
    """(params, all_within_3pp)；没有校准文件返回 (None, False)。进程内缓存。"""
    if path is None and _OPP["loaded"]:
        return _OPP["params"], _OPP["ok"]
    try:
        with open(path or PARAMS_PATH, encoding="utf-8") as f:
            obj = json.load(f)
        res = (obj["params"], bool(obj.get("all_within_3pp")))
    except (OSError, ValueError, KeyError):
        res = (None, False)
    if path is None:
        _OPP.update(loaded=True, params=res[0], ok=res[1])
    return res


# ---------------------------------------------------------------- 快照 -> Slim

def slim_from_snapshot(snap, deal, our_seat, ctx=None):
    """服务端快照 + 一副补全牌局 -> ``Slim``。``ctx`` 里的 piao_count/chain_has_gang 是 bot 自己
    按局记录的（快照没有这两个字段）。"""
    ctx = ctx or {}
    counts = []
    for seat in range(4):
        c = [0] * 34
        tiles = snap["my_hand"] if seat == our_seat else deal[seat]
        for t in tiles:
            c[TILE_INDEX[t]] += 1
        counts.append(c)
    wall = [TILE_INDEX[t] for t in deal["wall"]]
    server_wall = snap.get("wall_remaining") or 0
    st = Slim(counts, snap["dealer"], snap["round_no"], snap.get("scores") or [0, 0, 0, 0], wall,
              TOTAL_TILES - DEAL_SIZE * 4 - (server_wall + 1))
    for seat in range(4):
        for m in (snap.get("melds") or [[], [], [], []])[seat]:
            st.meld_n[seat] += 1
            if m["kind"] == "chi":
                st.chi_n[seat] += 1
            elif m["kind"] == "peng":
                st.pengs[seat].append(TILE_INDEX[m["tiles"][0]])
    st.turn = our_seat
    dt = snap.get("drawn_tile")
    st.drawn = TILE_INDEX[dt] if dt else -1
    god = snap.get("god") or {}
    st.chain_count = god.get("chain_count") or 0
    st.chain_owner = our_seat if st.chain_count else None
    if st.chain_count:
        st.piao_count[our_seat] = int(ctx.get("piao_count") or 0)
        st.chain_has_gang = bool(ctx.get("chain_has_gang"))
        st.chain_has_piao = st.piao_count[our_seat] > 0
    st.catch_play = bool(god.get("catch_play"))
    g = god.get("god_discarder_seat", -1)
    st.god_discarder_seat = None if g is None or g < 0 else g
    return st


# ---------------------------------------------------------------- 候选

def _candidates(snap, action, hu_ok, k):
    """[(kind, tile)]：生产选择放第一个。"""
    hand = snap["my_hand"]
    mg = len((snap.get("melds") or [[], [], [], []])[snap["seat"]])
    counts = to_counts(hand)
    scored, seen = [], set()
    for t in hand:
        if t in seen:
            continue
        seen.add(t)
        after = list(counts)
        after[TILE_INDEX[t]] -= 1
        scored.append((hu_distance(tuple(after), mg), t))
    scored.sort()
    cands = []

    def add(c):
        if c not in cands:
            cands.append(c)

    add((action.get("action"), action.get("tile") or None) if action.get("action") == "discard"
        else ("hu", None))
    if hu_ok:
        add(("hu", None))
        if snap.get("drawn_tile"):
            add(("discard", snap["drawn_tile"]))
    if JOKER in hand:
        add(("discard", JOKER))
    n_extra = 0
    for _d, t in scored:
        if n_extra >= k:
            break
        if ("discard", t) not in cands:
            add(("discard", t))
            n_extra += 1
    return cands


def _apply(st, seat, cand):
    kind, tile = cand
    if kind == "hu":
        st.apply_hu(seat)
    else:
        st.apply_discard(seat, TILE_INDEX[tile])


def _routes(hand):
    return ["R_A", "R_B", "R_C", "R_D"] if JOKER in hand else ["R_A", "R_C"]


def _value(st, seat, before):
    v = st.scores[seat] - before
    if not st.is_draw and st.winner == seat:
        v += _dealer_value(st.round_no)
    return v


def _mean_se(v):
    n = len(v)
    if n < 2:
        return (sum(v) / n if n else 0.0), float("inf")
    m = sum(v) / n
    return m, (sum((x - m) ** 2 for x in v) / (n - 1) / n) ** 0.5


def _seed_for(snap, ctx):
    if ctx.get("seed") is not None:
        return int(ctx["seed"]) & 0x7FFFFFFF
    key = repr((snap.get("game_id"), snap.get("round_no"), snap.get("seat"), snap.get("wall_remaining"),
                tuple(snap.get("my_hand") or ())))
    return int(hashlib.md5(key.encode()).hexdigest()[:8], 16) & 0x7FFFFFFF


# ---------------------------------------------------------------- 推演求值

def _evaluate(snap, cands, prod, our_seat, ctx, opp, log):
    """返回 (alt_index_or_None)；原因写进 ``log["fallback_reason"]``。"""
    cfg = _CONFIG
    t0 = ctx.get("t0")
    if t0 is None:
        t0 = time.monotonic()
    fixed_n = ctx.get("fixed_n")
    deadline = t0 + float(ctx.get("cap_s") or cfg["cap_discard_hu_s"])
    seed = _seed_for(snap, ctx)
    routes = _routes(snap["my_hand"])
    pilot = max(2, fixed_n // 4) if fixed_n else cfg["mc_pilot_time"]
    before = (snap.get("scores") or [0, 0, 0, 0])[our_seat]
    timed = fixed_n is None

    def expired():
        return timed and time.monotonic() >= deadline

    def build(deals):
        return [slim_from_snapshot(snap, d, our_seat, ctx) for d in deals]

    def run(base, di, cand, route):
        st = base.clone()
        _apply(st, our_seat, cand)
        play_out(st, random.Random(seed ^ (di * 7919 + 1)), our_seat=our_seat, route=route, params=opp)
        return _value(st, our_seat, before)

    # --- 选择阶段
    bases = build(determinize_batch(snap, pilot, seed))
    best_route, pilot_mean, dead = {}, {}, set()
    for ci, cand in enumerate(cands):
        for route in routes:
            vals = []
            for di, base in enumerate(bases):
                if expired():
                    log["fallback_reason"] = "timeout"
                    return None
                try:
                    vals.append(run(base, di, cand, route))
                except IllegalActionError:
                    dead.add(ci)
                    break
            if ci in dead:
                break
            m = sum(vals) / len(vals)
            if ci not in pilot_mean or m > pilot_mean[ci]:
                pilot_mean[ci], best_route[ci] = m, route
    if prod in dead:
        log["fallback_reason"] = "production_illegal"
        return None
    alts = [ci for ci in range(len(cands)) if ci != prod and ci not in dead]
    if not alts:
        log["fallback_reason"] = "no_alternative"
        return None
    alt = max(alts, key=lambda ci: pilot_mean[ci])
    log.update({"prod": "%s:%s" % cands[prod], "alt": "%s:%s" % cands[alt],
                "route_prod": best_route[prod], "route_alt": best_route[alt],
                "pilot_prod": round(pilot_mean[prod], 3), "pilot_alt": round(pilot_mean[alt], 3)})

    # --- 检验阶段：独立的另一批牌局，只比 生产 vs 备选
    vp, va = [], []
    batch_no = 1
    while True:
        if fixed_n is not None:
            need = fixed_n - pilot - len(vp)
            if need <= 0:
                break
            n_b = min(cfg["batch_size"], need)
        else:
            if expired():
                break
            n_b = cfg["batch_size"]
        deals = determinize_batch(snap, n_b, seed + batch_no)
        batch_no += 1
        for j, d in enumerate(deals):
            if expired():
                break
            base = slim_from_snapshot(snap, d, our_seat, ctx)
            di = (batch_no << 8) + j
            vp.append(run(base, di, cands[prod], best_route[prod]))
            va.append(run(base, di, cands[alt], best_route[alt]))
    n = len(vp)
    log["n"] = n
    if n < cfg["mc_min_n"]:
        log["fallback_reason"] = "insufficient_n"
        return None
    diffs = [a - b for a, b in zip(va, vp)]
    dm, se = _mean_se(diffs)
    log["diff"], log["se"] = round(dm, 4), round(se, 4)
    if dm > 0 and dm > cfg["mc_z"] * se:
        return alt
    log["fallback_reason"] = "not_significant"
    return None


# ---------------------------------------------------------------- 入口

def _switches():
    w = load_weights()
    return (bool(w.get("mc_hu_enabled", 0)), bool(w.get("mc_discard_enabled", 0)),
            bool(w.get("mc_menqing_enabled", 0)))


def mc_override(snapshot, action, rules=None, ctx=None):
    """``mj/bot.py::choose_action`` 算出生产 ``action`` 之后调用；返回要替换成的动作，``None`` =
    用生产动作。三个开关全 0 时第一行读完开关就返回，不读快照任何字段。``ctx``（可选 dict，调用方
    每次决策新建）：入 ``t0``/``piao_count``/``chain_has_gang``/``fixed_n``/``seed``，出
    ``mc_log``（本次决策的 MC 摘要，bot 写进决策日志）。"""
    hu_on, disc_on, men_on = _switches()
    if not (hu_on or disc_on or men_on):
        return None
    if ctx is None:
        ctx = {}
    t_start = time.monotonic()
    log = {}
    try:
        res = _mc_override(snapshot, action, ctx, log, hu_on, disc_on, men_on)
    except Exception as exc:   # noqa: BLE001 — MC 自己的任何 bug 都不能影响正式决策
        log["fallback_reason"] = "exception:%r" % (exc,)
        res = None
    if log:
        log["elapsed_ms"] = round((time.monotonic() - t_start) * 1000, 1)
        log["overridden"] = res is not None
        ctx["mc_log"] = log
    return res


def _mc_override(snapshot, action, ctx, log, hu_on, disc_on, men_on):
    if snapshot.get("phase") != "draw":
        return None
    kind = (action or {}).get("action")
    if kind not in ("hu", "discard"):
        return None
    our_seat = snapshot.get("seat")
    god = snapshot.get("god") or {}
    if god.get("catch_play") and god.get("god_discarder_seat") != our_seat:
        return None   # 抓打圈受限：只能打刚摸到的牌，没有可选项
    hand = snapshot.get("my_hand") or []
    drawn = snapshot.get("drawn_tile")
    mg = len((snapshot.get("melds") or [[], [], [], []])[our_seat])
    c14 = to_counts(hand)
    hu_now = bool(drawn) and is_hu(c14, mg)
    free_piao = kind == "discard" and action.get("tile") == JOKER
    if hu_now or kind == "hu" or free_piao:
        cls = "hu"
        if not hu_on:
            return None
    else:
        has_joker = c14[JOKER_IDX] > 0
        menqing = men_on and mg == 0 and pair_shanten(c14) <= 3
        if not ((has_joker and disc_on) or menqing):
            return None
        cls = "joker_discard" if has_joker and disc_on else "menqing"
    opp, calibrated = load_opp_params()
    log.update({"cls": cls, "calibrated_all_3pp": calibrated})
    if opp is None:
        log["fallback_reason"] = "no_calibration"
        return None
    cands = _candidates(snapshot, action, hu_now, _CONFIG["mc_topk"])
    prod = 0
    log["cands"] = ["%s:%s" % c for c in cands]
    alt = _evaluate(snapshot, cands, prod, our_seat, ctx, opp, log)
    if alt is None:
        return None
    kind_a, tile_a = cands[alt]
    if kind_a == "hu":
        return {"action": "hu", "tile": drawn or ""}
    return {"action": "discard", "tile": tile_a}
