"""G2.1：把 ``mj.sim.engine.RoundEngine`` 的内部状态映射成服务端**原始**
快照的字段命名（不是引擎内部 schema，也不是 ``logs/*.jsonl`` 里落盘的
``decision`` 记录那种已经改过名的扁平 payload）——这份快照是直接喂给生产
``mj.bot.choose_action(snapshot, rules, gang_open)`` 用的，字段名必须跟
``mj/state.py::normalize()``/``mj/responses.py`` 实际读取的键一致，读
``mj/bot.py``+``mj/responses.py``+``mj/state.py`` 里所有 ``snapshot.get(...)``
调用点核实过一遍（``my_hand``、``last_discard``，不是 ``hand``——落盘的
``decision.hand`` 是 ``mj/logging.py::DecisionLog.action`` 改名之后的，
两者不是同一个 schema，之前一版做错过，见
``tools/sim_snapshot_check.py`` 的 ``_compare`` 怎么把两边字段对上）。

已核实的口径差异：
- 杠用扁平 ``kind``："gang_an"/"gang_ming"/"gang_bu"，不是引擎内部的
  ``{"kind": "gang", "sub": "an"}``，也不是 ``mj/melds.py`` 生产代码自己
  产生的 ``{"kind": "gang"}``（三边三种写法，生产代码从没按 kind 精确
  匹配过 gang，不影响线上策略，见 3c 报告）。
- ``wall_remaining``：服务端口径 = 引擎口径 - 1（3c 用 99 万条真实决策
  核对出来的全局固定偏移，庄家起手隐藏的那张摸牌服务端算、引擎不算）。
- ``god_discarder_seat``：没有豁免时服务端给 ``-1``，不是 ``None``。
- ``phase``：服务端在响应阶段区分 ``response_peng``/``response_chi`` 两个
  值，不是引擎内部单一的 ``"response"`` + ``response_stage``。
- ``turn``：响应阶段服务端给的是"这次弃牌的打牌人是谁"（即窗口触发者），
  跟引擎自己的 ``self.turn``（响应阶段结算前还停在打牌人那里没推进）
  语义天然一致。
- ``drawn_tile``：只有"当前这一刻真的摸到手里、还没打出去"才非空——碰/吃
  之后的弃牌决策是空的，不是刚碰/吃到的那张（第一版猜错过，见
  ``mj.sim.engine`` 里 ``active_tile`` 字段的实证注释）；庄家起手 14 张
  那张隐藏摸牌，服务端会在第一次决策里把它当 drawn_tile 报出来。
- ``responding_seats``（碰窗口，已钉死，不是"还没查清的残留"）：服务端
  给的是这个碰窗口**开窗那一刻的原始名单**（打牌人以外的 3 个座位），
  不随着其他座位陆续 pass 掉而收窄；``mj.sim.engine`` 自己的
  ``self.responding_seats`` 是**活的、随 pass 收缩**的集合（``apply_pass``
  会 ``.remove(seat)``）。用全量 3c 精确对齐核对过全部 178682 条
  ``responding_seats`` 不一致：100% 是 ``response_peng``（``response_chi``
  0 条不一致），100% 是"日志比引擎多几个座位"（``log ⊇ engine``，反方向
  ``engine ⊇ log`` 0 例）——跟"已经 pass 过的座位服务端仍然留在名单里"
  完全吻合，不是资格判定本身有分歧（通读 ``mj/bot.py``/``mj/responses.py``/
  ``mj/state.py`` 全部 ``responding_seats`` 用法，生产代码只检查"我自己
  这个座位在不在名单里"，从不读其他座位，这个"静态 vs 实时收缩"的差异
  不影响任何真实决策）。本模块故意不改成"开窗时的静态名单"：``build_snapshot``
  只在"查询这一刻"被调用一次，没有状态记住"这个窗口最初问了哪些人"，
  要还原静态名单得在 ``RoundEngine`` 里单独维护一份"本窗口原始成员"字段，
  本轮判断收益（跟生产行为无关）不值得这个改动的复杂度，如实记录为
  已排查清楚、刻意不修的已知差异。
- ``god.chain_count``：**服务端按座位下发，不是全局广播**——只有当前有
  资格兑现这条链的座位（``chain_owner`` 或当前 ``god_discarder_seat``，
  见 ``mj.sim.engine.can_self_draw_hu`` 的归属判据）自己查询快照时才看到
  真实的链数，其余座位一律看到 0，即使引擎内部这条链客观上还活着。
- **没有 ``piao`` 字段**（``god`` 里也没有）：服务端从不下发这个字段的
  真实值（``god.piao``/顶层 ``piao`` 都不下发，``mj.state.canonical_piao``
  两边都读不到只能回退 0）——这是一个已知的线上问题（见 ``mj/bot.py::
  hu_result`` 调用点的注释），本模块故意不"悄悄修好"它，原样复现服务端
  这个恒为空的行为，否则喂给 ``mj.bot.choose_action`` 的快照会比真实服务端
  更"聪明"，测不出这个真实存在的低估。需要引擎自己的精确 piao 计数时，
  直接读 ``engine.piao_count[seat]``，不要指望这份快照里有。

已知不能验证的字段：``discards``（``logs/*.jsonl`` 落盘的 ``decision``
记录本身没有这个字段，没有真实数据能核对形状/顺序是否和服务端一致），
这里按"每座位一个列表、按弃牌顺序"的最直观形状给，供 ``mj.responses.py``
里读 discards 算染色/危险度的路径能跑起来，不保证跟服务端字节级一致。
"""
from ..rules import baotou
from ..tiles import JOKER_IDX, to_counts


def _normalize_meld(m):
    if not isinstance(m, dict):
        return m
    kind = m.get("kind")
    if kind == "gang" and m.get("sub"):
        kind = "gang_%s" % m["sub"]
    return {"kind": kind, "tiles": list(m.get("tiles") or [])}


def _phase_for(engine):
    if engine.phase == "response":
        return "response_peng" if engine.response_stage == "peng" else "response_chi"
    return engine.phase


def build_snapshot(engine, seat):
    """返回座位 ``seat`` 此刻看到的原始快照 dict（字段命名见模块
    docstring），可以直接传给 ``mj.bot.choose_action``。不修改引擎状态，
    可以在强制回放/自对弈的任意节点调用。"""
    phase = _phase_for(engine)
    hand = list(engine.hands[seat])
    if phase == "draw" and engine.drawn_tile is not None and engine.turn == seat:
        pre_hand = list(hand)
        pre_hand.remove(engine.drawn_tile)
    else:
        pre_hand = hand
    counts13 = to_counts(pre_hand)
    is_baotou = baotou(counts13, engine.meld_groups(seat))
    seat_owns_chain = seat == engine.chain_owner or seat == engine.god_discarder_seat
    return {
        "seat": seat,
        "phase": phase,
        "turn": engine.turn,
        "dealer": engine.dealer,
        "round_no": engine.round_no,
        "wall_remaining": engine.wall_remaining() - 1,   # 服务端口径，见模块 docstring
        "scores": list(engine.scores),
        "drawn_tile": (engine.active_tile or "") if (phase == "draw" and engine.turn == seat) else "",
        # 服务端没有 window_tile：当前弃牌在 last_discard 里（生产代码按 window_tile -> last_discard ->
        # discarded_tile 回退取值，见 mj/responses.py::_discarded）。last_discard 是"最近一次打出的牌"，
        # 摸牌阶段也在（线上 result 事件的原始快照里 phase=finished 时也带着，整局持续保留）。
        "last_discard": engine.last_discard or "",
        "hand_counts": [len(h) for h in engine.hands],   # 四家当前手牌张数（服务端字段）
        "responding_seats": sorted(engine.responding_seats) if phase != "draw" else [],
        "my_hand": hand,
        "discards": [list(d) for d in engine.discards],   # 形状未经真实数据核实，见模块 docstring
        "melds": [[_normalize_meld(m) for m in seat_melds] for seat_melds in engine.melds],
        "rules": {},
        "god": {
            "baotou": bool(is_baotou),
            "chain_count": engine.chain_count if seat_owns_chain else 0,
            "catch_play": engine.catch_play,
            "god_discarder_seat": -1 if engine.god_discarder_seat is None else engine.god_discarder_seat,
        },
    }
