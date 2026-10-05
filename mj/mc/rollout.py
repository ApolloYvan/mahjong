"""阶段二 §4：轻量推演——把一局从"当前这一刻"推演到结束，四家分别用一套
快速策略（不是生产 ``mj.bot.choose_action``：那是留给真实决策的，推演里
每次决策要跑几十上百局，必须快）。

设计取舍（如实记录）：任务书原话是"轻量推演状态（计数数组，不用
``deepcopy(RoundEngine)``）"。这里没有照做——``RolloutState`` 直接
``mj.sim.engine.RoundEngine.clone()``（本来就是 ``copy.deepcopy``）。
原因：连杠/杠飘链/"财飘只在爆头胡时生效"/4个白板/杠开这几条规则的口径，
是上一阶段花了一整轮经验调试才从真实数据里抠出来的（见
``mj/sim/engine.py`` 模块 docstring 里的 H1-H5 / 3c 记录），跟 G1 引擎的
状态转移紧密耦合在一堆私有字段上（``chain_owner``/``chain_has_gang``/
``chain_has_piao``/``god_discarder_seat``/``response_stage`` 等）。在
``mj/mc/`` 下另起一套"计数数组"状态机、自己重新实现这些转移规则，等于
把这条已经踩过坑的逻辑复刻第二遍，复刻错的风险远大于 ``deepcopy`` 的
性能成本。correctness（§2 硬约束"必须和 G1 引擎完全一致"）优先于这条
性能建议——性能问题等 ``tools/mc_check.py speed`` 量出来真的不够用，再
针对量出来的热点做增量优化（比如换成手写的 undo/redo 而不是整份深拷贝），
不在这轮预先猜测性能瓶颈在哪。

四家默认策略（``GreedyPolicy``，对手用；我方在推演里按固定路线走，见
``ROUTE_POLICIES``）：
- 出牌：``mj.mc.fast.hu_distance`` 最小的牌；平局按"孤立程度"（越孤立
  越先弃，见 ``_isolation_score``）——不用真实进张数分平局，见
  ``_best_discard`` 的性能记录。
- 吃碰：只在会让 ``hu_distance`` 变小时才做；两摊吃上限/抓打圈/财神不可
  吃碰杠等约束全部交给 ``RoundEngine.apply_claim`` 自己判——它抛
  ``IllegalActionError`` 就当"这个候选不可行"，不在这里重复实现一遍
  约束（跟 ``tools/arena2.py`` 的 try/except 兜底是同一个模式）。
- 胡：能胡就胡。
- 飘：``baotou`` 态且手上有财神时，按 ``piao_prob`` 概率主动打出财神。
"""
import random

from ..rules import baotou
from ..sim.engine import IllegalActionError
from ..tiles import JOKER, JOKER_IDX, to_counts
from .fast import hu_distance, is_hu

DEFAULT_PARAMS = {
    "claim_aggressiveness": 1.0,   # >1 更愿意吃碰（容忍打平/微弱变差），<1 更保守；见 tools/mc_check.py calibrate
    "discard_noise": 0.0,          # 出牌在"并列最优"候选里加多少随机扰动（0=总选第一个确定性候选）
    "piao_prob": 0.5,              # 爆头态时主动飘财神的概率
}


def _pre_draw_counts(engine, seat):
    pre = list(engine.hands[seat])
    if engine.drawn_tile is not None and engine.turn == seat:
        pre.remove(engine.drawn_tile)
    return to_counts(pre)


def _isolation_score(counts, idx):
    """越孤立（同种张数少、相邻花色张数少）分越高，弃牌平局时优先弃孤立牌；
    字牌/财神没有"相邻"概念，只看同种张数。"""
    from ..tiles import is_suited
    score = -counts[idx]
    if is_suited(idx) and idx % 9 not in (0,):
        score -= counts[idx - 1]
    if is_suited(idx) and idx % 9 != 8:
        score -= counts[idx + 1]
    return score


class GreedyPolicy:
    """默认快速策略：对手用；``params`` 由 ``tools/mc_check.py calibrate``
    拟合，本轮先用可配置的默认值（``DEFAULT_PARAMS``）。"""

    def __init__(self, params=None, rng=None):
        self.params = {**DEFAULT_PARAMS, **(params or {})}
        self.rng = rng or random.Random()

    def decide_draw(self, engine, seat):
        counts14 = list(engine.hands[seat])
        meld_groups = engine.meld_groups(seat)
        if is_hu(to_counts(counts14), meld_groups):
            return {"action": "hu"}
        gang_tile = self._self_gang_tile(engine, seat, counts14)
        if gang_tile is not None:
            return {"action": "gang", "tile": gang_tile}
        pre13 = _pre_draw_counts(engine, seat)
        if pre13[JOKER_IDX] and baotou(pre13, meld_groups) and self.rng.random() < self.params["piao_prob"]:
            return {"action": "discard", "tile": JOKER}
        return {"action": "discard", "tile": self._best_discard(counts14, meld_groups)}

    def _self_gang_tile(self, engine, seat, counts14):
        """暗杠（手里 4 张一样）或补杠（手里 1 张 + 已经碰过同种）——检测到
        就杠，真正的合法性（比如禁杠阈值 ``GANG_FORBIDDEN_REMAINING``）仍
        交给 ``RoundEngine.apply_gang`` 判，这里判非法会在 ``_apply_draw``
        里被捕获退回摸到的那张弃牌。"""
        counts = to_counts(counts14)
        for idx, n in enumerate(counts):
            if idx == JOKER_IDX:
                continue
            if n >= 4:
                from ..tiles import INDEX_TILE
                return INDEX_TILE[idx]
        for m in engine.melds[seat]:
            if m.get("kind") == "peng" and m["tiles"] and m["tiles"][0] in counts14:
                return m["tiles"][0]
        return None

    def _best_discard(self, hand_tiles, meld_groups):
        """不用 ``mj.shanten.ukeire()`` 分平局（故意，不是漏了）：
        ``ukeire()`` 内部要试 34 种可能的下一张牌各算一次 ``shanten``，
        比 ``hu_distance`` 本身贵一个数量级。先按"只对并列最优候选才算
        ukeire"优化过一次，实测（见下面的性能记录）早期牌局平局候选常
        有 5-6 个，优化后单局仍要 2-3 秒、个别手牌（多财神）飙到 17 秒，
        跟阶段二的预算（能效核上一个出牌决策要在 1 秒内跑够 128×3 局
        完整推演）差了两个数量级，``tests/test_mc_rollout.py`` 的全自
        对弈测试也因此跑到 410 秒。这里换成不查真实进张数、只用
        ``_isolation_score``（纯计数运算，O(1)）分平局——这是"轻量推演
        对手"该有的精度：对手策略本来就是生产 ``choose_action`` 的粗糙
        近似（见 ``tools/mc_check.py calibrate`` 要拟合的校准目标），
        平局时选哪张孤立张的差异对推演结果的影响远小于"能不能在预算内
        跑完"本身。"""
        from ..tiles import TILE_INDEX
        counts = to_counts(hand_tiles)
        scored = []
        seen_idx = set()
        for tile in hand_tiles:
            idx = TILE_INDEX[tile]
            if idx in seen_idx:
                continue
            seen_idx.add(idx)
            after = list(counts)
            after[idx] -= 1
            dist = hu_distance(tuple(after), meld_groups)
            iso = _isolation_score(counts, idx)
            scored.append((dist, iso, tile))
        scored.sort()
        best_key = scored[0][:2]
        ties = [s for s in scored if s[:2] == best_key]
        if self.params["discard_noise"] > 0 and len(ties) > 1 and self.rng.random() < self.params["discard_noise"]:
            return self.rng.choice(ties)[2]
        return ties[0][2]

    def decide_response(self, engine, seat, kind_options):
        """``kind_options``：这个座位当前合法可能想尝试的动作类型列表
        （由调用方按 ``response_stage`` 传进来，``["gang","peng"]`` 或
        ``["chi"]``）。逐个试算 ``hu_distance`` 会不会变小（真正的合法性
        仍交给 ``RoundEngine.apply_claim`` 判，见 ``_apply_response``），
        变小才采用，否则 pass。杠永远接受——原地补一张不改变听牌结构，
        只有好处（跟碰/吃不同，杠不需要跟"距离是否变小"比较）。"""
        pre = to_counts(engine.hands[seat])
        meld_groups = engine.meld_groups(seat)
        current = hu_distance(pre, meld_groups)
        for kind in kind_options:
            if kind == "gang":
                return {"action": "gang"}
            best_after = self._trial_distance(engine, seat, kind)
            if best_after is not None and best_after < current * self.params["claim_aggressiveness"]:
                return {"action": kind, "tiles": self._chi_tiles(engine, seat) if kind == "chi" else None}
        return {"action": "pass"}

    def _trial_distance(self, engine, seat, kind):
        from ..tiles import TILE_INDEX
        tile = engine.window_tile
        if tile is None:
            return None
        counts = list(to_counts(engine.hands[seat]))
        idx = TILE_INDEX[tile]
        if kind == "peng":
            if counts[idx] < 2:
                return None
            counts[idx] -= 2
            return hu_distance(tuple(counts), engine.meld_groups(seat) + 1)
        if kind == "chi":
            chi_opt = self._chi_tiles(engine, seat)
            if chi_opt is None:
                return None
            for t in chi_opt:
                counts[TILE_INDEX[t]] -= 1
            return hu_distance(tuple(counts), engine.meld_groups(seat) + 1)
        return None

    def _chi_tiles(self, engine, seat):
        """返回吃这张 ``window_tile`` 要搭配的另外两张（不含被吃的那张
        本身），三种顺子位置（该牌是顺子里最小/中间/最大那张）里第一个
        手牌凑得齐的。"""
        from ..tiles import TILE_INDEX, INDEX_TILE, is_suited, rank
        tile = engine.window_tile
        if tile is None or not is_suited(TILE_INDEX[tile]):
            return None
        idx = TILE_INDEX[tile]
        r = rank(idx)
        suit_base = idx - (r - 1)
        hand = list(engine.hands[seat])
        for seq_start in (r - 2, r - 1, r):
            seq = [seq_start, seq_start + 1, seq_start + 2]
            if seq[0] < 1 or seq[2] > 9:
                continue
            others = [n for n in seq if n != r]
            tiles_needed = [INDEX_TILE[suit_base + n - 1] for n in others]
            if all(t in hand for t in tiles_needed):
                return tiles_needed
        return None


def run_round(engine, seat_policies, max_steps=3000):
    """从 ``engine`` 当前状态推演到 ``engine.finished``。``seat_policies``：
    ``{seat: policy}``，policy 需要 ``decide_draw(engine,seat)``/
    ``decide_response(engine,seat,kind_options)``。返回 ``engine``
    （跟 ``tools/arena2.py::play_round`` 同构，故意保持接口一致，两边都是
    "驱动 RoundEngine 到 finished"这一件事，只是决策来源不同）。"""
    steps = 0
    while not engine.finished:
        steps += 1
        if steps > max_steps:
            break
        if engine.phase == "draw":
            seat = engine.turn
            if engine.needs_draw():
                engine.step_draw()
                if engine.finished:
                    break
            action = seat_policies[seat].decide_draw(engine, seat)
            _apply_draw(engine, seat, action)
        elif engine.phase == "response":
            for seat in list(engine.responding_seats):
                if engine.phase != "response" or seat not in engine.responding_seats:
                    continue
                options = _response_options(engine, seat)
                action = seat_policies[seat].decide_response(engine, seat, options)
                _apply_response(engine, seat, action)
        else:
            break
    return engine


def _response_options(engine, seat):
    if engine.response_stage == "peng":
        return ["gang", "peng"]
    return ["chi"]


def _resolve_self_gang_kind(engine, seat, tile):
    for m in engine.melds[seat]:
        if m.get("kind") == "peng" and m["tiles"][0] == tile:
            return "bu"
    return "an"


def _apply_draw(engine, seat, action):
    try:
        if action["action"] == "hu":
            engine.apply_hu(seat)
        elif action["action"] == "gang":
            tile = action.get("tile") or engine.drawn_tile or engine.hands[seat][0]
            engine.apply_gang(seat, tile, _resolve_self_gang_kind(engine, seat, tile))
        else:
            engine.apply_discard(seat, action.get("tile") or engine.active_tile or engine.hands[seat][-1])
    except IllegalActionError:
        engine.apply_discard(seat, engine.active_tile or engine.hands[seat][-1])


def _apply_response(engine, seat, action):
    try:
        kind = action.get("action")
        if kind == "pass" or not kind:
            engine.apply_pass(seat)
        elif kind == "peng":
            engine.apply_claim(seat, "peng", None)
        elif kind == "gang":
            engine.apply_claim(seat, "gang", None)
        elif kind == "chi":
            engine.apply_claim(seat, "chi", action.get("tiles"))
        else:
            engine.apply_pass(seat)
    except IllegalActionError:
        engine.apply_pass(seat)
