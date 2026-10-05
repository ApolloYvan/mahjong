"""轻量推演状态：``mj.sim.engine.RoundEngine`` 的计数数组镜像，不 deepcopy。

目标：单局推演在性能核上 <=5ms（``tools/mc_check.py slim-speed`` 实测），
且跟 ``RoundEngine`` 在**同一串动作**下逐局对照胜者/番数/得分/detail 零不一致
（``tools/mc_check.py slim-equiv``，1 万局，交给用户跑）。

状态转移逻辑（抓打圈、飘/杠链归属与清零、禁杠、流局阈值、每人两摊吃上限、
财神不可吃碰杠、结算公式）逐条照 ``mj/sim/engine.py`` 抄——那些规则是用真实语料
一条条证伪出来的，这里不重新发明，只换数据结构：手牌是 34 维 int 列表，
副露只记"组数/吃数/碰过哪些牌"（推演里不需要副露的具体牌面）。

唯一的"近似"是 ``can_hu`` 里的**精确必要条件预过滤**（孤张下界）：被预过滤
拒掉的手牌必然不胡，所以不改变任何结果，只是跳过绝大多数手牌的 ``evaluate``。
胡牌与计番本身仍然调用 ``mj.rules.evaluate``（单一真相源）。
"""
import random
from functools import lru_cache

from ..rules import baotou, evaluate, payout, seven_pairs
from ..sim.engine import (CHAIN_CAP, DEAL_SIZE, GANG_FORBIDDEN_REMAINING, IllegalActionError,
                          TOTAL_TILES, WALL_EXHAUST_REMAINING)
from ..tiles import INDEX_TILE, JOKER_IDX, NSUITS, TILE_INDEX

DRAW, RESP = 0, 1
J = JOKER_IDX

_evaluate_cached = lru_cache(maxsize=1 << 18)(evaluate)
_baotou_cached = lru_cache(maxsize=1 << 18)(baotou)


def _isolated_singles(c):
    """手里"孤张"个数：数量恰为 1 且同花色 +-1/+-2 都没有牌的非财神牌。"""
    n = 0
    for t in range(27):
        if c[t] == 1:
            r = t % 9
            if not ((r >= 1 and c[t - 1]) or (r >= 2 and c[t - 2]) or (r <= 7 and c[t + 1]) or (r <= 6 and c[t + 2])):
                n += 1
    for t in range(27, J):
        if c[t] == 1:
            n += 1
    return n


def _std_possible(c):
    """标准胡的精确必要条件：每个孤张各自占一个独立的组/将，组里除它外的牌只能
    靠财神补——做将最少补 1 张，做面子最少补 2 张，所以 jokers >= 2I-1（I>=1）。"""
    i = _isolated_singles(c)
    return i == 0 or c[J] >= 2 * i - 1


def _flat_meld(m):
    kind = m.get("kind")
    if kind == "gang" and m.get("sub"):
        kind = "gang_%s" % m["sub"]
    return {"kind": kind, "tiles": list(m.get("tiles") or [])}


class Slim:
    __slots__ = ("counts", "meld_n", "chi_n", "pengs", "scores", "piao_count", "wall", "drawn_count",
                 "dealer", "round_no", "turn", "phase", "drawn", "window_tile", "window_seat",
                 "response_stage", "responding", "catch_play", "god_discarder_seat", "chain_count",
                 "chain_owner", "chain_has_gang", "chain_has_piao", "finished", "is_draw", "winner",
                 "fan", "detail", "draws", "claim_n", "virtual", "track", "rivers", "meld_list", "last_disc")

    def __init__(self, hands_counts, dealer, round_no, scores, wall, drawn_count=0):
        self.counts = [list(c) for c in hands_counts]
        self.meld_n = [0, 0, 0, 0]
        self.chi_n = [0, 0, 0, 0]
        self.pengs = [[], [], [], []]
        self.scores = list(scores)
        self.piao_count = [0, 0, 0, 0]
        self.wall = list(wall)
        self.drawn_count = drawn_count
        self.dealer = dealer
        self.round_no = round_no
        self.turn = dealer
        self.phase = DRAW
        self.drawn = -1
        self.window_tile = -1
        self.window_seat = -1
        self.response_stage = None
        self.responding = []
        self.catch_play = False
        self.god_discarder_seat = None
        self.chain_count = 0
        self.chain_owner = None
        self.chain_has_gang = False
        self.chain_has_piao = False
        self.finished = False
        self.is_draw = False
        self.winner = None
        self.fan = None
        self.detail = None
        self.draws = [0, 0, 0, 0]       # 每座位摸牌次数（含杠后补牌）
        self.claim_n = [0, 0, 0]        # 全桌吃/碰/杠(明杠)次数，校准用
        self.virtual = False            # 本局的胡是"虚拟自摸"（胡牌率补足，见 play_out）
        self.track = False              # 记牌河/副露明细（只为 to_snapshot 服务；默认关，不拖慢推演）
        self.rivers = None
        self.meld_list = None
        self.last_disc = ""

    def clone(self):
        n = Slim.__new__(Slim)
        n.counts = [c[:] for c in self.counts]
        n.meld_n = self.meld_n[:]
        n.chi_n = self.chi_n[:]
        n.pengs = [p[:] for p in self.pengs]
        n.scores = self.scores[:]
        n.piao_count = self.piao_count[:]
        n.wall = self.wall[:]
        n.responding = self.responding[:]
        n.draws = self.draws[:]
        n.claim_n = self.claim_n[:]
        n.track = self.track
        n.rivers = [r[:] for r in self.rivers] if self.track else None
        n.meld_list = [[dict(m, tiles=list(m["tiles"])) for m in ml] for ml in self.meld_list] if self.track else None
        n.last_disc = self.last_disc
        for k in ("drawn_count", "dealer", "round_no", "turn", "phase", "drawn", "window_tile", "window_seat",
                  "response_stage", "catch_play", "god_discarder_seat", "chain_count", "chain_owner",
                  "chain_has_gang", "chain_has_piao", "finished", "is_draw", "winner", "fan", "detail", "virtual"):
            setattr(n, k, getattr(self, k))
        return n

    @classmethod
    def from_wall(cls, tiles, dealer, round_no, scores):
        """跟 ``RoundEngine.__init__`` 同样的发牌方式（13 张×4 家，从牌堆末尾 pop）。"""
        deck = [TILE_INDEX[t] for t in tiles]
        counts = [[0] * NSUITS for _ in range(4)]
        for _ in range(DEAL_SIZE):
            for seat in range(4):
                counts[seat][deck.pop()] += 1
        return cls(counts, dealer, round_no, scores, deck)

    @classmethod
    def from_engine(cls, engine, wall_idx=None):
        """从 ``RoundEngine`` 当前状态建一个 Slim（等价性测试/从决策点起推用）。
        ``wall_idx``：牌墙（牌码下标，pop 从末尾），缺省取引擎自己的 ``_deck``。"""
        counts = []
        for h in engine.hands:
            c = [0] * NSUITS
            for t in h:
                c[TILE_INDEX[t]] += 1
            counts.append(c)
        wall = wall_idx if wall_idx is not None else [TILE_INDEX[t] for t in engine._deck]
        s = cls(counts, engine.dealer, engine.round_no, engine.scores, wall, engine.drawn_count)
        for seat in range(4):
            for m in engine.melds[seat]:
                s.meld_n[seat] += 1
                if m["kind"] == "chi":
                    s.chi_n[seat] += 1
                elif m["kind"] == "peng":
                    s.pengs[seat].append(TILE_INDEX[m["tiles"][0]])
        s.piao_count = list(engine.piao_count)
        s.turn = engine.turn
        s.phase = RESP if engine.phase == "response" else DRAW
        s.drawn = TILE_INDEX[engine.drawn_tile] if engine.drawn_tile is not None else -1
        if engine.window_tile is not None:
            s.window_tile = TILE_INDEX[engine.window_tile]
        s.window_seat = engine.window_seat if engine.window_seat is not None else -1
        s.response_stage = engine.response_stage
        s.responding = list(engine.responding_seats)
        s.catch_play = engine.catch_play
        s.god_discarder_seat = engine.god_discarder_seat
        s.chain_count = engine.chain_count
        s.chain_owner = engine.chain_owner
        s.chain_has_gang = engine.chain_has_gang
        s.chain_has_piao = engine.chain_has_piao
        s.enable_tracking(engine.discards, [[_flat_meld(m) for m in ms] for ms in engine.melds], engine.last_discard or "")
        return s

    # ---------------------------------------------------------------- 快照输出（服务端原始 schema）
    def enable_tracking(self, rivers=None, melds=None, last_discard=""):
        """开始记牌河/副露明细（``to_snapshot`` 需要）。``rivers``/``melds`` 缺省为空（整局从头开始的状态）；
        从中途快照建 Slim 时把快照里的 discards/melds 传进来（melds 用服务端扁平 kind：chi/peng/gang_an/gang_ming/gang_bu）。"""
        self.track = True
        self.rivers = [list(r) for r in (rivers or [[], [], [], []])]
        self.meld_list = [[dict(m, tiles=list(m["tiles"])) for m in ml] for ml in (melds or [[], [], [], []])]
        self.last_disc = last_discard or ""
        return self

    def to_snapshot(self, seat):
        """从 ``seat`` 视角输出服务端原始快照（字段同 ``mj.sim.snapshot.build_snapshot``：my_hand / last_discard /
        hand_counts / discards / melds / god / …，没有 window_tile）。需要先 ``enable_tracking``。
        ``my_hand`` 的顺序跟引擎不同（计数数组没有顺序）：按牌码升序，刚摸的牌放最后。"""
        if not self.track:
            raise IllegalActionError("没有开启 enable_tracking，无法输出快照")
        if self.phase == 2:
            phase = "finished"       # 流局结算后；自摸胡之后引擎的 phase 仍是 draw（只置 finished 标志），这里同口径
        elif self.phase == RESP:
            phase = "response_peng" if self.response_stage == "peng" else "response_chi"
        else:
            phase = "draw"
        drawn = self.drawn if (phase == "draw" and self.turn == seat and self.drawn >= 0) else -1
        c = list(self.counts[seat])
        if drawn >= 0:
            c[drawn] -= 1
        hand = [INDEX_TILE[t] for t in range(NSUITS) for _ in range(c[t])]
        if drawn >= 0:
            hand.append(INDEX_TILE[drawn])
        pre = list(self.counts[seat])
        if drawn >= 0:
            pre[drawn] -= 1
        owns_chain = seat == self.chain_owner or seat == self.god_discarder_seat
        return {
            "seat": seat, "phase": phase, "turn": self.turn, "dealer": self.dealer, "round_no": self.round_no,
            "wall_remaining": self.wall_remaining() - 1, "scores": list(self.scores),
            "drawn_tile": INDEX_TILE[drawn] if drawn >= 0 else "",
            "responding_seats": sorted(self.responding) if phase in ("response_peng", "response_chi") else [],
            "my_hand": hand, "last_discard": self.last_disc,
            "hand_counts": [sum(cc) for cc in self.counts],
            "discards": [list(r) for r in self.rivers],
            "melds": [[dict(m, tiles=list(m["tiles"])) for m in ml] for ml in self.meld_list],
            "rules": {},
            "god": {"baotou": bool(baotou(tuple(pre), self.meld_n[seat])),
                    "chain_count": self.chain_count if owns_chain else 0,
                    "catch_play": self.catch_play,
                    "god_discarder_seat": -1 if self.god_discarder_seat is None else self.god_discarder_seat},
        }

    # ---------------------------------------------------------------- 只读
    def wall_remaining(self):
        return TOTAL_TILES - DEAL_SIZE * 4 - self.drawn_count

    def needs_draw(self):
        return (self.phase == DRAW and self.drawn < 0
                and sum(self.counts[self.turn]) == DEAL_SIZE - 3 * self.meld_n[self.turn])

    def _next(self, seat):
        return (seat + 1) % 4

    # ---------------------------------------------------------------- 摸牌/判胡
    def step_draw(self, forced=None):
        if self.phase != DRAW:
            raise IllegalActionError("摸牌时机不对")
        if self.wall_remaining() <= WALL_EXHAUST_REMAINING:
            self._settle_draw()
            return -1
        t = forced if forced is not None else self.wall.pop()
        self.counts[self.turn][t] += 1
        self.draws[self.turn] += 1
        self.drawn_count += 1
        self.drawn = t
        return t

    def can_hu(self):
        """返回 ``evaluate`` 结果 dict 或 None，口径同 ``RoundEngine.can_self_draw_hu``。"""
        d = self.drawn
        if d < 0:
            return None
        seat = self.turn
        c = self.counts[seat]
        mg = self.meld_n[seat]
        if not (_std_possible(c) or (mg == 0 and seven_pairs(tuple(c)) is not None)):
            return None
        c[d] -= 1
        try:
            c13 = tuple(c)
            eff = 0
            if self.chain_count and (seat == self.chain_owner or seat == self.god_discarder_seat):
                held = c13[J] and mg == 0
                if self.chain_has_gang or held or _baotou_cached(c13, mg):
                    eff = self.chain_count
            return _evaluate_cached(c13, d, eff, self.piao_count[seat], mg, False, False)
        finally:
            c[d] += 1

    def apply_hu(self, seat):
        if self.phase != DRAW or seat != self.turn or self.drawn < 0:
            raise IllegalActionError("自摸时机不对")
        res = self.can_hu()
        if not res:
            raise IllegalActionError("座位 %d 当前牌不构成自摸" % seat)
        self.finished = True
        self.winner = seat
        self.fan = res["fan"]
        self.detail = res["detail"]
        self._settle_selfdraw(seat, res["fan"])
        return res

    def apply_virtual_hu(self, seat, fan):
        """胡牌率补足用的"虚拟自摸"：不检查手牌，番数由调用方给，其余按真实自摸支付规则结算。"""
        if self.phase != DRAW or seat != self.turn or self.drawn < 0:
            raise IllegalActionError("虚拟自摸时机不对")
        self.finished = True
        self.winner = seat
        self.fan = fan
        self.detail = ["virtual"]
        self.virtual = True
        self._settle_selfdraw(seat, fan)

    def _settle_selfdraw(self, seat, fan):
        if seat == self.dealer:
            self.scores[seat] += payout(fan, dealer=True)
            for s in range(4):
                if s != seat:
                    self.scores[s] -= 8 * fan
        else:
            self.scores[seat] += payout(fan, dealer=False)
            self.scores[self.dealer] -= 8 * fan
            for s in range(4):
                if s != seat and s != self.dealer:
                    self.scores[s] -= fan

    def _settle_draw(self):
        self.finished = True
        self.is_draw = True
        self.winner = None
        self.phase = 2

    # ---------------------------------------------------------------- 弃牌
    def apply_discard(self, seat, t):
        if self.phase != DRAW or seat != self.turn:
            raise IllegalActionError("弃牌时机不对")
        c = self.counts[seat]
        if c[t] <= 0:
            raise IllegalActionError("座位 %d 手里没有 %d" % (seat, t))
        if self.catch_play and seat != self.god_discarder_seat and t != self.drawn:
            raise IllegalActionError("抓打圈内只能打刚摸到的牌")
        is_joker = t == J
        free_choice = is_joker and ((not self.catch_play) or seat == self.god_discarder_seat)
        if free_choice and self.chain_count >= CHAIN_CAP:
            raise IllegalActionError("动作链已封顶")
        c[t] -= 1
        if self.track:
            self.rivers[seat].append(INDEX_TILE[t])
            self.last_disc = INDEX_TILE[t]
        if free_choice:
            self.chain_count += 1
            self.chain_owner = seat
            self.chain_has_piao = True
            self.piao_count[seat] += 1
        self.drawn = -1
        self._advance_catch_play(seat, is_joker, free_choice)
        self.window_tile = t
        self.window_seat = seat
        if is_joker:
            self._advance_after_no_claim(seat)
            return
        peng = self._peng_window_seats(seat)
        if peng:
            self.phase = RESP
            self.response_stage = "peng"
            self.responding = peng
        else:
            self._open_chi_window_or_advance(seat)

    def _advance_catch_play(self, discarder, is_joker, is_piao):
        if is_joker:
            self.catch_play = True
            self.god_discarder_seat = discarder
        elif self.catch_play and discarder == self.god_discarder_seat:
            self.catch_play = False
            self.god_discarder_seat = None
            if self.chain_owner is None:
                self.chain_count = 0
                self.chain_has_gang = False
                self.chain_has_piao = False
        if not is_piao and discarder == self.chain_owner:
            self.piao_count[discarder] = 0
            self.chain_owner = None
            if self.god_discarder_seat is None or self.god_discarder_seat == discarder:
                self.chain_count = 0
                self.chain_has_gang = False
                self.chain_has_piao = False

    def _peng_window_seats(self, discarder):
        if self.catch_play:
            return [self.god_discarder_seat] if self.god_discarder_seat != discarder else []
        return [s for s in range(4) if s != discarder]

    def _open_chi_window_or_advance(self, discarder):
        chi_seat = self._next(discarder)
        if self.catch_play and self.god_discarder_seat != chi_seat:
            self._advance_after_no_claim(discarder)
            return
        self.phase = RESP
        self.response_stage = "chi"
        self.responding = [chi_seat]

    def _advance_after_no_claim(self, discarder):
        self.window_tile = -1
        self.window_seat = -1
        self.response_stage = None
        self.responding = []
        self.turn = self._next(discarder)
        if self.wall_remaining() <= WALL_EXHAUST_REMAINING:
            self._settle_draw()
            return
        self.phase = DRAW

    # ---------------------------------------------------------------- 响应
    def apply_pass(self, seat):
        if self.phase != RESP or seat not in self.responding:
            raise IllegalActionError("座位 %d 现在没有响应窗口可以 pass" % seat)
        self.responding.remove(seat)
        if self.responding:
            return
        if self.response_stage == "peng":
            self._open_chi_window_or_advance(self.window_seat)
        else:
            self._advance_after_no_claim(self.window_seat)

    def chi_options(self, seat, t):
        if t >= 27 or t == J:
            return []
        c = self.counts[seat]
        out = []
        for a, b in ((-2, -1), (-1, 1), (1, 2)):
            i1, i2 = t + a, t + b
            if min(i1, i2) < 0 or max(i1, i2) >= 27 or i1 // 9 != t // 9 or i2 // 9 != t // 9:
                continue
            if c[i1] and c[i2]:
                out.append([i1, i2])
        return out

    def apply_claim(self, seat, kind, tiles=None):
        expected = "chi" if kind == "chi" else "peng"
        if self.phase != RESP or seat not in self.responding or self.response_stage != expected:
            raise IllegalActionError("座位 %d 现在没有 %s 的响应窗口" % (seat, kind))
        t = self.window_tile
        if t == J:
            raise IllegalActionError("财神不能被吃/碰/杠")
        c = self.counts[seat]
        if kind == "peng":
            if c[t] < 2:
                raise IllegalActionError("碰张数不够")
            c[t] -= 2
            self.pengs[seat].append(t)
        elif kind == "chi":
            if seat != self._next(self.window_seat) or self.chi_n[seat] >= 2:
                raise IllegalActionError("不能吃")
            if list(tiles) not in self.chi_options(seat, t):
                raise IllegalActionError("吃的组合不合法")
            for x in tiles:
                c[x] -= 1
            self.chi_n[seat] += 1
        elif kind == "gang":
            if c[t] < 3 or self.wall_remaining() <= GANG_FORBIDDEN_REMAINING:
                raise IllegalActionError("明杠不合法")
            c[t] -= 3
        else:
            raise IllegalActionError("未知声明类型")
        self.meld_n[seat] += 1
        if self.track:
            if kind == "peng":
                self.meld_list[seat].append({"kind": "peng", "tiles": [INDEX_TILE[t]] * 3})
            elif kind == "chi":
                self.meld_list[seat].append({"kind": "chi", "tiles": [INDEX_TILE[x] for x in tiles] + [INDEX_TILE[t]]})
            else:
                self.meld_list[seat].append({"kind": "gang_ming", "tiles": [INDEX_TILE[t]] * 4})
        self.claim_n[{"chi": 0, "peng": 1, "gang": 2}[kind]] += 1
        self.window_tile = -1
        self.window_seat = -1
        self.response_stage = None
        self.responding = []
        self.turn = seat
        self.phase = DRAW
        self.drawn = -1
        if kind == "gang":
            self._advance_gang_chain(seat)
            self.step_draw()

    def apply_gang(self, seat, t, kind):
        if t == J:
            raise IllegalActionError("财神不能杠")
        if self.wall_remaining() <= GANG_FORBIDDEN_REMAINING:
            raise IllegalActionError("墙尾禁杠")
        c = self.counts[seat]
        if kind == "an":
            if self.phase != DRAW or seat != self.turn or c[t] < 4:
                raise IllegalActionError("暗杠不合法")
            c[t] -= 4
            self.meld_n[seat] += 1
            if self.track:
                self.meld_list[seat].append({"kind": "gang_an", "tiles": [INDEX_TILE[t]] * 4})
        elif kind == "bu":
            if self.phase != DRAW or seat != self.turn or t not in self.pengs[seat] or c[t] <= 0:
                raise IllegalActionError("补杠不合法")
            c[t] -= 1
            self.pengs[seat].remove(t)
            if self.track:
                ml = self.meld_list[seat]
                ml.remove(next(m for m in ml if m["kind"] == "peng" and m["tiles"][0] == INDEX_TILE[t]))
                ml.append({"kind": "gang_bu", "tiles": [INDEX_TILE[t]] * 4})
        else:
            raise IllegalActionError("未知杠类型")
        self.drawn = -1
        self._advance_gang_chain(seat)
        self.step_draw()

    def _advance_gang_chain(self, seat):
        if self.chain_owner is not None and seat != self.chain_owner:
            self.piao_count[self.chain_owner] = 0
            self.chain_count = 0
            self.chain_has_gang = False
            self.chain_has_piao = False
        if self.chain_count >= CHAIN_CAP:
            return
        self.chain_count += 1
        self.chain_owner = seat
        self.chain_has_gang = True


# ---------------------------------------------------------------- 向听估计（查表，策略用）
# 逐花色把 9 位计数分解成 (面子数 m, 搭子数 t, 有无将 p) 的帕累托前沿，按计数元组惰性缓存；
# 四组（三花色+字牌）前沿合并后取 8-2m-t-p，再减去财神张数（每张财神≈+1 进度）。
# 只给**对手/路线策略**排序弃牌用，不参与任何胡牌/计番判定（那些仍走 rules.evaluate）。

_SUIT_CACHE = {}


def _pareto(opts):
    out = []
    for o in sorted(set(opts), reverse=True):
        if not any(q[0] >= o[0] and q[1] >= o[1] and q[2] >= o[2] for q in out):
            out.append(o)
    return tuple(out)


def _suit_opts(c9):
    r = _SUIT_CACHE.get(c9)
    if r is not None:
        return r
    res = []

    def dfs(c, i, m, t, p):
        while i < 9 and c[i] == 0:
            i += 1
        if i >= 9:
            res.append((m, t, p))
            return
        n = c[i]
        if n >= 3:
            c[i] -= 3
            dfs(c, i, m + 1, t, p)
            c[i] += 3
        if n >= 2:
            c[i] -= 2
            if p:
                dfs(c, i, m, t + 1, p)
            else:
                dfs(c, i, m, t, 1)
                dfs(c, i, m, t + 1, p)
            c[i] += 2
        if i <= 6 and c[i + 1] and c[i + 2]:
            c[i] -= 1; c[i + 1] -= 1; c[i + 2] -= 1
            dfs(c, i, m + 1, t, p)
            c[i] += 1; c[i + 1] += 1; c[i + 2] += 1
        if i <= 7 and c[i + 1]:
            c[i] -= 1; c[i + 1] -= 1
            dfs(c, i, m, t + 1, p)
            c[i] += 1; c[i + 1] += 1
        if i <= 6 and c[i + 2]:
            c[i] -= 1; c[i + 2] -= 1
            dfs(c, i, m, t + 1, p)
            c[i] += 1; c[i + 2] += 1
        c[i] -= 1          # 单张，丢掉
        dfs(c, i + 1 if c[i] == 0 else i, m, t, p)
        c[i] += 1

    dfs(list(c9), 0, 0, 0, 0)
    r = _pareto(res)
    _SUIT_CACHE[c9] = r
    return r


def _honor_opts(h):
    m = sum(1 for x in h if x >= 3)
    pairs = sum(1 for x in h if x == 2)
    opts = [(m, pairs, 0)]
    if pairs:
        opts.append((m, pairs - 1, 1))
    return _pareto(opts)


def _merge(a, b):
    return _pareto([(x[0] + y[0], x[1] + y[1], min(1, x[2] + y[2])) for x in a for y in b])


def _shanten_from_opts(groups, meld_n, jokers):
    cur = groups[0]
    for g in groups[1:]:
        cur = _merge(cur, g)
    best = 9
    for m, t, p in cur:
        m += meld_n
        if m > 4:
            m = 4
        tt = min(t, 4 - m)
        v = 8 - 2 * m - tt - p
        if v < best:
            best = v
    return best - jokers


def shanten_est(c, meld_n=0):
    groups = [_suit_opts(tuple(c[0:9])), _suit_opts(tuple(c[9:18])), _suit_opts(tuple(c[18:27])),
              _honor_opts(c[27:J])]
    return _shanten_from_opts(groups, meld_n, c[J])


def _pick_discard_shanten(c, meld_n, rng, noise):
    """按向听估计选弃牌：先用孤立启发式挑 4 个最像废牌的候选，再用查表向听细分。"""
    cands = []
    for t in range(J):
        n = c[t]
        if not n:
            continue
        v = _KEEP[n]
        if t < 27:
            r = t % 9
            if r >= 1 and c[t - 1]:
                v += 2
            if r >= 2 and c[t - 2]:
                v += 1
            if r <= 7 and c[t + 1]:
                v += 2
            if r <= 6 and c[t + 2]:
                v += 1
        cands.append((v, t))
    if not cands:
        return J
    cands.sort()
    best, key = -1, None
    jk = c[J]
    for v, t in cands[:5]:
        c[t] -= 1
        sh = shanten_est(c, meld_n)
        c[t] += 1
        k = (sh, v + (rng.random() * noise if noise else 0.0))
        if key is None or k < key:
            key, best = k, t
    return best


# ====================================================================== 快速策略

DEFAULT_PARAMS = {
    "p_peng": 0.5,       # 碰窗口里手有 >=2 张时碰的概率
    "p_chi": 0.25,       # 吃窗口里有可吃组合时吃的概率
    "p_gang": 0.8,       # 有杠可杠时杠的概率
    "piao_prob": 0.5,    # 爆头态且手里有财神时主动飘的概率
    "p_flat": 0.2,       # 吃/碰后向听不变时仍然吃/碰的概率（变小时按 p_peng/p_chi）
    "noise": 0.5,        # 出牌打分的随机扰动幅度（0=完全确定）
    "bt_wall": 38,       # R_B/R_D：引擎口径墙剩余 <= 这个值才接受非爆头的胡
}

_KEEP = (0, 0, 6, 9, 10)
_QIDUI = (0, 0, 8, 7, 7)


def _pick_discard(c, qidui, rng, noise):
    best, bv = -1, 1e9
    for t in range(J):
        n = c[t]
        if not n:
            continue
        if qidui:
            v = _QIDUI[n]
        else:
            v = _KEEP[n]
            if t < 27:
                r = t % 9
                if r >= 1 and c[t - 1]:
                    v += 2
                if r >= 2 and c[t - 2]:
                    v += 1
                if r <= 7 and c[t + 1]:
                    v += 2
                if r <= 6 and c[t + 2]:
                    v += 1
        if noise:
            v += rng.random() * noise
        if v < bv:
            bv, best = v, t
    return best if best >= 0 else J


def _baotou_state(st, seat):
    """摸牌前 13 张是否爆头（带孤张下界预过滤，必要条件：多补一张完全孤立的牌也不破坏）。"""
    c = st.counts[seat]
    d = st.drawn
    if d >= 0:
        c[d] -= 1
    try:
        if not _std_possible_plus_one(c) and not (st.meld_n[seat] == 0):
            return False
        return bool(_baotou_cached(tuple(c), st.meld_n[seat]))
    finally:
        if d >= 0:
            c[d] += 1


def _std_possible_plus_one(c):
    i = _isolated_singles(c)
    return c[J] >= 2 * (i + 1) - 1


def play_out(st, rng, our_seat=-1, route="R_A", params=None):
    """从 ``st`` 当前状态推演到结束。``our_seat`` 按 ``route``（R_A 普通/R_B 爆头/
    R_C 七对/R_D 七对爆头）走，其余座位用默认快速策略。返回 ``st``。"""
    p = dict(DEFAULT_PARAMS) if params is None else {**DEFAULT_PARAMS, **params}
    tp = p.get("topup")            # 胡牌率补足表（校准产物），只作用于非我方座位
    base = st.drawn_count // 4 if st.drawn_count and not any(st.draws) else 0   # 从中途状态起推：估计各家已摸次数
    steps = 0
    while not st.finished and steps < 3000:
        steps += 1
        if st.phase == DRAW:
            seat = st.turn
            if st.needs_draw():
                st.step_draw()
                if st.finished:
                    break
            _draw_action(st, seat, rng, p, route if seat == our_seat else "R_A",
                         tp if seat != our_seat else None, base)
        elif st.phase == RESP:
            for seat in list(st.responding):
                if st.phase != RESP or seat not in st.responding:
                    continue
                _resp_action(st, seat, rng, p, route if seat == our_seat else "R_A")
        else:
            break
    return st


def _topup_fan(rng, cum):
    r = rng.random()
    for fan, c in cum:
        if r < c:
            return fan
    return cum[-1][0]


def _draw_action(st, seat, rng, p, route, tp=None, base=0):
    qidui = route in ("R_C", "R_D")
    keep_joker = route in ("R_B", "R_D")
    if st.drawn >= 0:
        res = st.can_hu()
        if res and (not keep_joker or res["baotou"] or st.wall_remaining() <= p["bt_wall"]):
            st.apply_hu(seat)
            return
    if tp is not None and st.drawn >= 0:
        # 胡牌率补足：策略没胡时，按（庄/闲, 自己第几摸）的概率再触发一次虚拟自摸
        side = "d" if seat == st.dealer else "n"
        row = tp["p"][side]
        k = base + st.draws[seat]
        if k < len(row) and row[k] > 0 and rng.random() < row[k]:
            st.apply_virtual_hu(seat, _topup_fan(rng, tp["fan"][side]))
            return
    c = st.counts[seat]
    restricted = st.catch_play and seat != st.god_discarder_seat and st.drawn >= 0
    if not restricted and not qidui and rng.random() < p["p_gang"]:
        for t in range(J):
            if c[t] == 4 or (c[t] and t in st.pengs[seat]):
                try:
                    st.apply_gang(seat, t, "an" if c[t] == 4 else "bu")
                    return _draw_action(st, seat, rng, p, route, tp, base) if not st.finished and st.phase == DRAW else None
                except IllegalActionError:
                    break
    if restricted:
        st.apply_discard(seat, st.drawn)
        return
    if c[J] and not keep_joker and rng.random() < p["piao_prob"] and _baotou_state(st, seat):
        try:
            st.apply_discard(seat, J)
            return
        except IllegalActionError:
            pass
    if qidui:
        st.apply_discard(seat, _pick_discard(c, True, rng, p["noise"]))
    else:
        st.apply_discard(seat, _pick_discard_shanten(c, st.meld_n[seat], rng, p["noise"]))


def _resp_action(st, seat, rng, p, route):
    if route in ("R_C", "R_D"):
        st.apply_pass(seat)
        return
    t = st.window_tile
    c = st.counts[seat]
    mg = st.meld_n[seat]
    try:
        if st.response_stage == "peng":
            if c[t] >= 3 and rng.random() < p["p_gang"] and st.wall_remaining() > GANG_FORBIDDEN_REMAINING:
                st.apply_claim(seat, "gang")
                return
            if c[t] >= 2:
                sb = shanten_est(c, mg)
                c[t] -= 2
                sa = shanten_est(c, mg + 1)
                c[t] += 2
                if rng.random() < (p["p_peng"] if sa < sb else p["p_flat"] if sa == sb else 0.0):
                    st.apply_claim(seat, "peng")
                    return
        elif st.chi_n[seat] < 2:
            opts = st.chi_options(seat, t)
            if opts:
                sb = shanten_est(c, mg)
                best, bs = None, 99
                for o in opts:
                    c[o[0]] -= 1
                    c[o[1]] -= 1
                    sa = shanten_est(c, mg + 1)
                    c[o[0]] += 1
                    c[o[1]] += 1
                    if sa < bs:
                        best, bs = o, sa
                if rng.random() < (p["p_chi"] if bs < sb else p["p_flat"] if bs == sb else 0.0):
                    st.apply_claim(seat, "chi", best)
                    return
    except IllegalActionError:
        pass
    st.apply_pass(seat)
