"""S1：生产快照 -> 定长稀疏特征 + 动作空间 + 合法动作掩码。纯标准库，实战要用。

所有"座位"一律用相对座位：0=我自己，1=我的下家，2=对家，3=上家（``rel = (seat - my_seat) % 4``）。
编码是**稀疏**的：``encode_sparse`` 返回 ``(indices, values)``，纯 Python 推理时第一层只对非零项累加
（nnz 约 150~250，比 ~1.3k 维稠密向量快一个数量级）；``encode`` 给稠密向量（训练/校验用）。

输入是服务端原始快照（``mj.sim.snapshot.build_snapshot`` 的 schema / 线上 ``/state``）。快照里没有、
bot 自己按局记录的两个量通过 ``ctx`` 传入（缺省 0）：``piao_count``（本座位当前链里自由飘出的财神数）、
``chain_has_gang``。

特征块（名字、维度见 ``LAYOUT``；``FEATURE_DIM`` 是总维度）：手牌计数/4、刚摸的牌 one-hot、窗口牌 one-hot、
四家副露牌计数/4 + 四家副露种类计数/4、四家牌河计数/4、四家最近 6 张牌河 one-hot（0=最近）、全桌可见牌计数/4、
以及一批标量（墙剩余、局号、庄家相对座位、四家分数/100、阶段、抓打圈、链、飘数…）。
"""
from ..tiles import INDEX_TILE, JOKER, JOKER_IDX, NSUITS, TILE_INDEX

RECENT = 6
MAX_ROUND = 8

# ---------------------------------------------------------------- 特征布局
_BLOCKS = [
    ("hand", NSUITS),
    ("drawn", NSUITS),
    ("window", NSUITS),
    ("meld_tiles", 4 * NSUITS),
    ("meld_kinds", 4 * 4),            # 每家：chi / peng / gang_an / gang_other 的组数
    ("disc_count", 4 * NSUITS),
    ("disc_recent", 4 * RECENT * NSUITS),
    ("visible", NSUITS),
    ("wall", 1),
    ("round_oh", MAX_ROUND),
    ("round_frac", 1),
    ("dealer_rel", 4),
    ("turn_rel", 4),
    ("scores", 4),
    ("phase", 3),                      # draw / response_peng / response_chi
    ("self_responding", 1),
    ("catch_play", 1),
    ("god_rel", 5),                    # 4 个相对座位 + 无
    ("chain", 1),
    ("chain_visible", 1),
    ("my_piao", 1),
    ("chain_has_gang", 1),
    ("joker_cnt", 1),
    ("my_melds", 1),
    ("has_drawn", 1),
    ("is_dealer", 1),
]
LAYOUT = {}
_off = 0
for _name, _size in _BLOCKS:
    LAYOUT[_name] = (_off, _size)
    _off += _size
FEATURE_DIM = _off
del _off, _name, _size

PHASES = ("draw", "response_peng", "response_chi")


def window_of(snapshot):
    """当前弃牌：服务端没有 window_tile，在 last_discard 里；回退顺序同 ``mj.responses._discarded``。"""
    return snapshot.get("window_tile") or snapshot.get("last_discard") or snapshot.get("discarded_tile") or None


def _rel(seat, me):
    return (seat - me) % 4


def _tidx(t):
    return TILE_INDEX.get(t)


def _meld_kind_slot(kind):
    if kind == "chi":
        return 0
    if kind == "peng":
        return 1
    if kind == "gang_an":
        return 2
    return 3 if isinstance(kind, str) and kind.startswith("gang") else None


def encode_sparse(snapshot, ctx=None):
    """返回 ``(indices, values)``（升序，无重复，值全部非零）。"""
    ctx = ctx or {}
    me = snapshot.get("seat", 0)
    feats = {}

    def put(block, i, v):
        if v:
            feats[LAYOUT[block][0] + i] = float(v)

    hand = snapshot.get("my_hand") or []
    hand_c = [0] * NSUITS
    for t in hand:
        hand_c[TILE_INDEX[t]] += 1
    vis = list(hand_c)
    for i, n in enumerate(hand_c):
        put("hand", i, n / 4.0)

    drawn = snapshot.get("drawn_tile")
    if drawn:
        put("drawn", TILE_INDEX[drawn], 1.0)
    wt = window_of(snapshot)
    if wt:
        put("window", TILE_INDEX[wt], 1.0)

    melds = snapshot.get("melds") or [[], [], [], []]
    for seat in range(4):
        r = _rel(seat, me)
        mt = [0] * NSUITS
        kinds = [0, 0, 0, 0]
        for m in (melds[seat] if seat < len(melds) else []) or []:
            slot = _meld_kind_slot(m.get("kind"))
            if slot is not None:
                kinds[slot] += 1
            for t in m.get("tiles") or []:
                mt[TILE_INDEX[t]] += 1
        for i, n in enumerate(mt):
            put("meld_tiles", r * NSUITS + i, n / 4.0)
            vis[i] += n
        for k, n in enumerate(kinds):
            put("meld_kinds", r * 4 + k, n / 4.0)

    discards = snapshot.get("discards") or [[], [], [], []]
    for seat in range(4):
        r = _rel(seat, me)
        river = (discards[seat] if seat < len(discards) else []) or []
        dc = [0] * NSUITS
        for t in river:
            dc[TILE_INDEX[t]] += 1
        for i, n in enumerate(dc):
            put("disc_count", r * NSUITS + i, n / 4.0)
            vis[i] += n
        for slot, t in enumerate(reversed(river[-RECENT:])):
            put("disc_recent", (r * RECENT + slot) * NSUITS + TILE_INDEX[t], 1.0)
    for i, n in enumerate(vis):
        put("visible", i, n / 4.0)

    put("wall", 0, (snapshot.get("wall_remaining") or 0) / 136.0)
    rn = snapshot.get("round_no") or 1
    put("round_oh", min(max(rn, 1), MAX_ROUND) - 1, 1.0)
    put("round_frac", 0, rn / float(MAX_ROUND))
    dealer = snapshot.get("dealer")
    if dealer is not None:
        put("dealer_rel", _rel(dealer, me), 1.0)
        put("is_dealer", 0, 1.0 if dealer == me else 0.0)
    turn = snapshot.get("turn")
    if turn is not None:
        put("turn_rel", _rel(turn, me), 1.0)
    scores = snapshot.get("scores") or [0, 0, 0, 0]
    for seat in range(4):
        put("scores", _rel(seat, me), scores[seat] / 100.0)
    phase = snapshot.get("phase")
    if phase in PHASES:
        put("phase", PHASES.index(phase), 1.0)
    put("self_responding", 0, 1.0 if me in (snapshot.get("responding_seats") or []) else 0.0)

    god = snapshot.get("god") or {}
    put("catch_play", 0, 1.0 if god.get("catch_play") else 0.0)
    gds = god.get("god_discarder_seat", -1)
    put("god_rel", 4 if gds is None or gds < 0 else _rel(gds, me), 1.0)
    chain = god.get("chain_count") or 0
    put("chain", 0, chain / 3.0)
    put("chain_visible", 0, 1.0 if chain else 0.0)
    put("my_piao", 0, (ctx.get("piao_count") or 0) / 4.0)
    put("chain_has_gang", 0, 1.0 if ctx.get("chain_has_gang") else 0.0)
    put("joker_cnt", 0, hand_c[JOKER_IDX] / 4.0)
    put("my_melds", 0, len(melds[me]) / 4.0 if me < len(melds) else 0.0)
    put("has_drawn", 0, 1.0 if drawn else 0.0)

    idx = sorted(feats)
    return idx, [feats[i] for i in idx]


def encode(snapshot, ctx=None):
    """稠密向量（长度 ``FEATURE_DIM``），训练/校验用。"""
    idx, val = encode_sparse(snapshot, ctx)
    out = [0.0] * FEATURE_DIM
    for i, v in zip(idx, val):
        out[i] = v
    return out


def decode(dense):
    """把稠密向量里**可逆**的字段还原出来（校验器用）：手牌/刚摸/窗口牌、四家副露牌与种类、四家牌河计数与最近 6 张、
    墙剩余、局号、庄家/轮到谁（相对座位）、分数、阶段、抓打圈、链数、飘数等。返回 dict，座位均为相对座位。"""
    def blk(name):
        o, n = LAYOUT[name]
        return dense[o:o + n]

    def counts(v, scale=4.0):
        return [int(round(x * scale)) for x in v]

    out = {"hand": counts(blk("hand")), "visible": counts(blk("visible"))}
    d, w = blk("drawn"), blk("window")
    out["drawn"] = d.index(1.0) if 1.0 in d else None
    out["window"] = w.index(1.0) if 1.0 in w else None
    mt, dc, dr = blk("meld_tiles"), blk("disc_count"), blk("disc_recent")
    out["meld_tiles"] = [counts(mt[r * NSUITS:(r + 1) * NSUITS]) for r in range(4)]
    out["meld_kinds"] = [counts(blk("meld_kinds")[r * 4:(r + 1) * 4]) for r in range(4)]
    out["disc_count"] = [counts(dc[r * NSUITS:(r + 1) * NSUITS]) for r in range(4)]
    recent = []
    for r in range(4):
        row = []
        for slot in range(RECENT):
            seg = dr[(r * RECENT + slot) * NSUITS:(r * RECENT + slot + 1) * NSUITS]
            row.append(seg.index(1.0) if 1.0 in seg else None)
        recent.append(row)
    out["disc_recent"] = recent
    out["wall"] = int(round(blk("wall")[0] * 136))
    out["round_no"] = blk("round_oh").index(1.0) + 1
    out["dealer_rel"] = blk("dealer_rel").index(1.0) if 1.0 in blk("dealer_rel") else None
    out["turn_rel"] = blk("turn_rel").index(1.0) if 1.0 in blk("turn_rel") else None
    out["scores"] = [int(round(x * 100)) for x in blk("scores")]
    ph = blk("phase")
    out["phase"] = PHASES[ph.index(1.0)] if 1.0 in ph else None
    out["self_responding"] = bool(blk("self_responding")[0])
    out["catch_play"] = bool(blk("catch_play")[0])
    gr = blk("god_rel")
    out["god_rel"] = gr.index(1.0) if 1.0 in gr else None   # 4 = 无
    out["chain"] = int(round(blk("chain")[0] * 3))
    out["my_piao"] = int(round(blk("my_piao")[0] * 4))
    out["chain_has_gang"] = bool(blk("chain_has_gang")[0])
    return out


# ---------------------------------------------------------------- 动作空间
# 0..33 打出该牌（打财神=飘）；34 胡；35 自己回合的杠；36 过；37 碰；38 明杠；39/40/41 吃（低/中/高）
A_DISCARD0, A_HU, A_GANG_SELF, A_PASS, A_PENG, A_GANG_MING, A_CHI_LOW, A_CHI_MID, A_CHI_HIGH = 0, 34, 35, 36, 37, 38, 39, 40, 41
N_ACTIONS = 42
CHI_OFFSETS = {A_CHI_LOW: (-2, -1), A_CHI_MID: (-1, 1), A_CHI_HIGH: (1, 2)}


def chi_slot(window_tile, own_tiles):
    """吃：窗口牌 + 手里两张 -> 动作下标；组合不合法返回 None。"""
    w = TILE_INDEX.get(window_tile)
    if w is None or w >= 27 or len(own_tiles) != 2:
        return None
    pair = sorted(TILE_INDEX[t] for t in own_tiles)
    for slot, (a, b) in CHI_OFFSETS.items():
        if pair == sorted((w + a, w + b)):
            return slot
    return None


def action_to_index(snapshot, action):
    """动作 dict -> 动作下标；无法映射返回 None。"""
    kind = (action or {}).get("action")
    if kind == "discard":
        return TILE_INDEX.get(action.get("tile"))
    if kind == "hu":
        return A_HU
    if kind == "gang":
        return A_GANG_SELF if snapshot.get("phase") == "draw" else A_GANG_MING
    if kind == "pass":
        return A_PASS
    if kind == "peng":
        return A_PENG
    if kind == "chi":
        return chi_slot(window_of(snapshot), list(action.get("tiles") or []))
    return None


def index_to_action(snapshot, idx):
    """动作下标 -> 可直接交给服务端的动作 dict；无法还原返回 None。"""
    if 0 <= idx < NSUITS:
        return {"action": "discard", "tile": INDEX_TILE[idx]}
    if idx == A_HU:
        return {"action": "hu", "tile": snapshot.get("drawn_tile") or ""}
    if idx == A_GANG_SELF:
        hand = snapshot.get("my_hand") or []
        me = snapshot.get("seat", 0)
        pengs = {m["tiles"][0] for m in (snapshot.get("melds") or [[], [], [], []])[me]
                 if m.get("kind") == "peng" and m.get("tiles")}
        for t in hand:
            if t != JOKER and (hand.count(t) >= 4 or t in pengs):
                return {"action": "gang", "tile": t}
        return None
    if idx == A_PASS:
        return {"action": "pass", "tile": ""}
    if idx == A_PENG:
        return {"action": "peng", "tile": window_of(snapshot) or ""}
    if idx == A_GANG_MING:
        return {"action": "gang", "tile": window_of(snapshot) or ""}
    if idx in CHI_OFFSETS:
        wt = window_of(snapshot)
        w = TILE_INDEX.get(wt)
        if w is None:
            return None
        a, b = CHI_OFFSETS[idx]
        return {"action": "chi", "tile": wt, "tiles": [INDEX_TILE[w + a], INDEX_TILE[w + b]]}
    return None


def legal_actions(snapshot, is_hu_fn=None):
    """当前决策点的合法动作下标列表（升序）。口径是"必要条件"：不模拟规则开关（如有财必靠），
    也不检查墙尾杠限制，所以是真实合法集合的超集；训练里用来做掩码，标签必须落在其中。
    ``is_hu_fn(counts14, meld_groups)`` 缺省用 ``mj.mc.fast.is_hu``。"""
    phase = snapshot.get("phase")
    me = snapshot.get("seat", 0)
    hand = snapshot.get("my_hand") or []
    god = snapshot.get("god") or {}
    restricted = bool(god.get("catch_play")) and god.get("god_discarder_seat") != me
    out = []
    if phase == "draw":
        drawn = snapshot.get("drawn_tile")
        if restricted and drawn:
            out.extend([TILE_INDEX[drawn]])
        else:
            out.extend(sorted({TILE_INDEX[t] for t in hand}))
        if drawn:
            if is_hu_fn is None:
                from ..mc.fast import is_hu as is_hu_fn
            from ..tiles import to_counts
            mg = len((snapshot.get("melds") or [[], [], [], []])[me])
            if is_hu_fn(to_counts(hand), mg):
                out.append(A_HU)
        if index_to_action(snapshot, A_GANG_SELF) is not None:   # 吃碰后没摸牌时也可能补杠，不依赖 drawn
            out.append(A_GANG_SELF)
        return sorted(set(out))
    if phase in ("response_peng", "response_chi"):
        if me not in (snapshot.get("responding_seats") or []):
            return []
        out.append(A_PASS)
        wt = window_of(snapshot)
        if restricted or not wt or wt == JOKER:
            return out
        if phase == "response_peng":
            n = hand.count(wt)
            if n >= 2:
                out.append(A_PENG)
            if n >= 3:
                out.append(A_GANG_MING)
        else:
            w = TILE_INDEX[wt]
            if w < 27:
                for slot, (a, b) in CHI_OFFSETS.items():
                    ia, ib = w + a, w + b
                    if 0 <= ia < 27 and 0 <= ib < 27 and ia // 9 == w // 9 == ib // 9 \
                            and INDEX_TILE[ia] in hand and INDEX_TILE[ib] in hand:
                        out.append(slot)
        return sorted(out)
    return out
