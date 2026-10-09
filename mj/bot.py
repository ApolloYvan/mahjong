"""在线麻将 Bot：自动匹配、判胡、策略出牌、决策日志。"""
import argparse
import concurrent.futures
import contextlib
import hashlib
import gc
import json
import os
import signal
import socket
import threading
import time
import traceback

gc.disable()

from .api import ApiError, MahjongApi, is_transient
from .async_log import AsyncDecisionLog, AnomalyRingBuffer, ConsecutiveStateDeduper
from .defense import penalties as defense_penalties_fn
from .ev import choose_route_discard
from .fit import load_weights, thread_weights_overlay
from .mc.chain_tracker import ChainTracker
from .joker_ev import hand_all_wait
from .hu_strategy import choose_hu_or_piao
from .logging import DecisionLog, utc_now
from .notify import notify_sequences
from .observability import (
    SCHEMA_VERSION,
    POLICY_VERSION,
    build_hash as _build_hash,
    config_hash as _config_hash,
    local_estimate_marker,
    new_decision_id,
    server_truth_unavailable_marker,
    stable_sample_state,
    state_hash as _state_hash,
)
from .rules import evaluate
from .responses import choose_chi, choose_gang, choose_peng, response_gang_take
from .sanitize import sanitize_text
from .state import canonical_chain_count, canonical_piao
from .state import normalize as _normalize_state
from .state import replay_dict as _replay_dict
from .strategy import choose_discard as choose_baseline_discard
from .tiles import JOKER, TILE_INDEX, to_counts, visible_counts

KNOWN_GUIDE_VERSION = 35  # v35: 404 按 code 判型(GONE可重试/NOT_FOUND放弃) / 功能开关 /
                          # 吃最多2摊服务端校验 / 删除匿名自注册 / M10R8。逐条核对见
                          # docs/audit/GUIDE_V35.md

# 2026-09-22 存活可观测性修复：房间 a_282b85347354 的决策日志在服务端仍在跑的
# 情况下戛然而止——没有 result、没有 error，进程消失得无声无息。_ACTIVE_GAME_IDS
# 记录当前仍在 play_game() 里的 game_id（play_game 最外层 try/finally 维护），
# 供 SIGTERM/SIGINT 处理器在终止记录里写出"死的时候手上还捏着哪几局"。
_active_game_ids_lock = threading.Lock()
_active_game_ids = set()


def _install_signal_handlers(log):
    """只在进程收到 SIGTERM/SIGINT 时补一条终止记录——不改变任何正常退出路径的
    行为。当前任何非正常退出都不留痕迹，这本身就是要修的可观测性缺陷（见
    §3.5）。不做自动重启：重启涉及令牌与房间状态，不在本次授权范围内。"""
    def _handle(signum, frame):
        signame = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
        with _active_game_ids_lock:
            active = sorted(_active_game_ids)
        try:
            log.append("error", {"reason": "signal_exit", "signal": signame, "active_games": active})
        except Exception:
            pass
        try:
            if hasattr(log, "close"):
                log.close(timeout=10.0)
        finally:
            os._exit(128 + signum)

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _handle)


def _melds_for_seat(snapshot, seat):
    melds = snapshot.get("melds") or {}
    if isinstance(melds, list):
        if len(melds) == 4 and all(isinstance(m, list) for m in melds):
            if isinstance(seat, int) and 0 <= seat < 4:
                return melds[seat]
            return []
        return melds
    return melds.get(str(seat), melds.get(seat, []))


def _meld_count(snapshot, seat):
    return len(_melds_for_seat(snapshot, seat))


def _discard_inputs(snapshot, rules=None):
    """``choose_discard`` 的输入准备（抽成函数，供 ``mj.nn.cands.production_candidates`` 复用同一口径）：
    返回 (hand, meld_groups, chain_count, piao, rules, visible, use_route)；没有手牌返回 None。"""
    hand = snapshot.get("my_hand") or []
    god = snapshot.get("god") or {}
    seat = snapshot.get("seat", -1)
    if not hand:
        return None
    meld_groups = _meld_count(snapshot, seat)
    visible = visible_counts(hand, snapshot.get("discards") or [], snapshot.get("melds") or [])
    defense_penalties = defense_penalties_fn(snapshot)
    if defense_penalties:
        rules = dict(rules or {})
        rules["_defense"] = defense_penalties
    chain_count = canonical_chain_count(snapshot)
    piao = canonical_piao(snapshot)
    is_dealer = snapshot.get("dealer") == seat
    # 弃牌小模型要用的局面信息（mj/nn_discard.py）；route EV（mj/route_ev.py）
    # 额外读 catch_play/round_no；其余打分路径不读 _ctx。round_no（T3
    # dealer_value 查表用，1..8）与 dealer 相对座位（T2 预留，本轮不用）
    # 都是公开信息，取不到时保持 None，dealer_value() 会据此回落 V=0。
    dealer_seat = snapshot.get("dealer")
    rules = dict(rules or {})
    rules["_ctx"] = {"wall": snapshot.get("wall_remaining", 60), "dealer": is_dealer,
                     "opp_melds": [_meld_count(snapshot, s) for s in range(4) if s != seat],
                     "catch_play": bool(god.get("catch_play")),
                     "round_no": snapshot.get("round_no"),
                     "dealer_rel": ((dealer_seat - seat) % 4
                                   if isinstance(dealer_seat, int) and isinstance(seat, int) else None)}
    use_route = bool(god.get("catch_play") or chain_count or piao or (rules or {}).get("YouCaiBiKao"))
    # dealer_route_enabled（默认 1 = 现行为）：0 = 庄家不强制 use_route、不加 dealer_hint，和闲家走同一条出牌路径（消融用）
    if is_dealer and load_weights().get("dealer_route_enabled", 1):
        use_route = True
        if isinstance(rules, dict):
            rules = {**rules, "dealer_hint": True}
    return hand, meld_groups, chain_count, piao, rules, visible, use_route


def choose_discard(snapshot, rules=None):
    hand = snapshot.get("my_hand") or []
    god = snapshot.get("god") or {}
    seat = snapshot.get("seat", -1)
    if god.get("catch_play") and god.get("god_discarder_seat") != seat:
        drawn = snapshot.get("drawn_tile")
        if drawn:
            return {"action": "discard", "tile": drawn}
    if hand:
        hand, meld_groups, chain_count, piao, rules, visible, use_route = _discard_inputs(snapshot, rules)
        discard = (choose_route_discard(hand, meld_groups, chain_count, piao, rules, visible)
                   if use_route else choose_baseline_discard(hand, meld_groups, chain_count, piao, rules,
                                                             visible))
        return {"action": "discard", "tile": discard}
    return None


def _hand_expectation(snapshot, seat):
    """摸牌前手牌应有张数 = 13 - 3*副露组数。"""
    return 13 - 3 * _meld_count(snapshot, seat)


def hu_result(snapshot, rules=None, gang_open=False):
    """判断当前快照是否构成胡牌，返回 evaluate() 的结果字典或 None。

    财神（JOKER/白板）口径（2026-09-23 实测纠错）：

    - **摸到财神凑成的非爆头普通成牌，默认可以胡。** 只有在服务端下发
      ``rules["YouCaiBiKao"]``（有财必靠）时才拒绝。
    - 爆头（baotou）= 13 张听牌态摸任意合法牌都胡；财神也在"任意牌"范围内，
      所以爆头态摸到财神无论规则开关都能胡。

    纠错依据——此前这里无条件拒绝"drawn 是财神且非爆头"的成牌，依据是
    ``docs/HANDOFF_CURRENT.md`` 的规则摘要，``docs/refactor/VALIDATION.md``
    里查不到任何真实对局验证。全量重放 21 房 / 1672 局（判据：该次
    ``tile_drawn`` 之后本局立刻 ``round_ended`` 且胜者为该座位）显示：

        对手用摸来的白板在非爆头手上当场自摸成和 **243 次**，服务端全部确认；
        我们 86 次机会 **0 次**。这些房间的决策日志里 ``rules`` 全部是空 ``{}``。

    生产日志独立复核：``phase=="draw"`` 且 ``drawn_tile=="白"`` 且
    ``action=="discard"`` 的决策 518 条中，**99 条手牌已构成合法和牌**
    （番型全部是平胡）——每一条都是白丢掉的一次胡牌。

    详见 ``docs/audit/TASK_P0_JOKER_HU.md``。

    **回滚条件：** 上线后任何一次 ``hu`` 动作收到 ``HTTP 409 INVALID_ACTION``，
    说明服务端确实禁止、上述推断被证伪——把下面那个 ``if`` 改回无条件版本。
    """
    hand = list(snapshot.get("my_hand") or [])
    drawn = snapshot.get("drawn_tile")
    if not drawn:
        return None
    expected = _hand_expectation(snapshot, snapshot.get("seat", -1))
    if len(hand) == expected + 1 and drawn in hand:
        hand.remove(drawn)
    elif len(hand) != expected:
        return None
    # 已知阶段二待办（本轮只记录、不修，见 G1 返修任务的验收说明）：
    # canonical_piao(snapshot) 恒为 0——服务端从不下发 god.piao/顶层 piao
    # 真实值（G2.1 用 mj.sim.snapshot 对全量决策日志精确 seq 对齐核出来
    # 的，321 条真实决策里胡牌人明明自由弃过财神、chain_count 也对得上，
    # 但 piao 字段仍是 0）。后果：飘过财神之后自摸，如果手里剩的财神数 +
    # 本局飘出的次数凑够 4（见 mj.rules.evaluate 的"4个白板"判据），这里
    # 会因为 piao 恒为 0 而漏乘一次 ×2，低估自己的真实番数。修法需要在
    # 线上维护一个"本座位本局飘出财神次数"的状态（跟 mj.sim.engine.
    # piao_count 同一套逻辑），snapshot 本身给不了，留到阶段二一起做。
    try:
        result = evaluate(
            to_counts(hand), TILE_INDEX[drawn],
            chain_count=canonical_chain_count(snapshot),
            piao=canonical_piao(snapshot),
            meld_groups=_meld_count(snapshot, snapshot.get("seat", -1)),
            must_kaoxiang=bool((rules or {}).get("YouCaiBiKao", False)),
            gang_open=gang_open,
        )
    except (KeyError, TypeError):
        return None

    # 「有财必靠」开启时，财神只能在爆头态完成胡牌；规则未开启则不限制。
    # 依据与回滚条件见本函数 docstring。
    if (rules or {}).get("YouCaiBiKao") and drawn == JOKER and not (result and result.get("baotou")):
        return None
    # joker_nonbaotou_hu_disabled（默认 0 = 现行为）：1 = 摸到财神且非爆头时不胡——复现 2026-09-23 之前的旧 bug
    # （实战 243:0），只用于评估器验收（tools/arena_suite.sh 的 V2：平台应判现行显著更好）。
    if drawn == JOKER and result and not result.get("baotou") and load_weights().get("joker_nonbaotou_hu_disabled", 0):
        return None
    return result


def can_hu(snapshot, rules=None, gang_open=False):
    """与 hu_result() 保持完全一致的判定口径（见 hu_result 文档字符串）：
    先算出真实结果；drawn 是财神且非爆头时，仅在 ``rules["YouCaiBiKao"]``
    开启的前提下才拒绝。"""
    return bool(hu_result(snapshot, rules=rules, gang_open=gang_open))


def _can_peng(snapshot):
    return snapshot.get("phase") == "response_peng" and len(snapshot.get("my_hand") or []) >= 2


def _can_chi(snapshot):
    seat = snapshot.get("seat", -1)
    melds = _melds_for_seat(snapshot, seat)
    return (
        snapshot.get("phase") == "response_chi"
        and len([m for m in melds if isinstance(m, dict) and m.get("kind") == "chi"]) < 2
    )


def _gang_bomb_choice(snapshot):
    """杠爆窗口：爆头态摸成暗杠四张，且杠后仍听任意 → ×4 确定胡优于 ×2 即胡。
    非听任意态不打（保 ×2 即胡）；补杠与二次杠由 replenish 决策自然续接。

    墙尾门槛必须与其他杠路径（``responses.choose_gang``、``choose_action`` 抓打杠
    分支）一致，统一使用 ``wall_remaining<=20``（20 张墙尾/最后 10 墩禁止杠，
    官网规则核验已确认）。历史 bug：此处误用 ``<=4``，导致墙尾 5~20 张区间本应
    禁杠却仍会触发杠爆，绕开了平台的墙尾杠限制。
    """
    drawn = snapshot.get("drawn_tile")
    hand = snapshot.get("my_hand") or []
    if not drawn or drawn == "白" or hand.count(drawn) < 4:
        return None
    if snapshot.get("wall_remaining", 99) <= 20:
        return None
    meld_groups = _meld_count(snapshot, snapshot.get("seat", -1))
    counts = list(to_counts(hand))
    counts[TILE_INDEX[drawn]] -= 4
    counts = tuple(counts)
    if sum(counts) != 13 - 3 * (meld_groups + 1):
        return None
    if not hand_all_wait(counts, meld_groups + 1):
        return None
    return {"action": "gang", "tile": drawn}


def _baotou_gang_open_choice(snapshot, weights=None):
    """rule_baotou_gang_open_enabled（默认关）：爆头态下补杠 / 手中暗杠后仍听任意
    → 岭上补牌必胡，吃到"杠开"×2。

    ``_gang_bomb_choice`` 只看"摸到的牌凑成暗杠"，而爆头态每张摸牌都能胡，
    ``choose_action`` 会直接走胡牌分支，补杠（碰过的刻子摸到第 4 张）和手里
    早就有的四张从来没机会杠。全语料 杠开·爆头 的 62 次胡牌里：补杠 42、
    明杠 13、暗杠 7；爆头研究所 247 胡里 6 次 杠开·爆头 =4番，我们 221 胡里 1 次。
    杠开 ×2 已用 205 条真实结算核实（见 mj/rules.py::evaluate）。
    """
    weights = weights if weights is not None else load_weights()
    if not weights.get("rule_baotou_gang_open_enabled", 0):
        return None
    if snapshot.get("wall_remaining", 99) <= 20:
        return None
    hand = snapshot.get("my_hand") or []
    seat = snapshot.get("seat", -1)
    meld_groups = _meld_count(snapshot, seat)
    base = list(to_counts(hand))
    if sum(base) != 14 - 3 * meld_groups:
        return None
    for meld in _melds_for_seat(snapshot, seat):
        tiles = meld.get("tiles", []) if isinstance(meld, dict) else []
        tile = tiles[0] if tiles else None
        if len(tiles) == 3 and len(set(tiles)) == 1 and tile != "白" and tile in hand:
            counts = list(base)
            counts[TILE_INDEX[tile]] -= 1
            if hand_all_wait(tuple(counts), meld_groups):
                return {"action": "gang", "tile": tile}
    for tile in sorted(set(hand)):
        if tile != "白" and hand.count(tile) == 4:
            counts = list(base)
            counts[TILE_INDEX[tile]] -= 4
            if hand_all_wait(tuple(counts), meld_groups + 1):
                return {"action": "gang", "tile": tile}
    return None


def _bu_defer_tile(snapshot, weights=None):
    """bu_defer_enabled（默认关，2026-10-08）：持财神、墙剩 > bu_defer_wall_min 时，手里那张「自己碰牌的第 4 张」
    先留着——不当场补杠、也不打掉。它和财神凑成对子；等其余牌成形（能胡）时，``_baotou_gang_open_choice``
    补杠它，剩下正好爆头听，岭上必胡 = 杠开·爆头 4番。返回要留的那张，不适用返回 None。

    tools/gangkai_study.py：玄武-2346 摸到第 4 张 53 次留了 22 次（我们 67 次留 4 次）；他持财神时留的 9 局
    胡 5 局、其中 4 局杠开·爆头，+34.8/局；无财神留的 13 局 −3.7/局，所以只在持财神时留。"""
    weights = weights if weights is not None else load_weights()
    if not weights.get("bu_defer_enabled", 0):
        return None
    if snapshot.get("wall_remaining", 0) <= weights.get("bu_defer_wall_min", 24):
        return None
    hand = snapshot.get("my_hand") or []
    if JOKER not in hand:
        return None
    for meld in _melds_for_seat(snapshot, snapshot.get("seat", -1)):
        tiles = meld.get("tiles", []) if isinstance(meld, dict) else []
        if len(tiles) == 3 and len(set(tiles)) == 1 and tiles[0] != JOKER and tiles[0] in hand:
            return tiles[0]
    return None


def _nn_valid(snapshot, action, rules, gang_open):
    """网络给出的动作再用生产自己的判定校验一遍（规则开关、墙尾禁杠、抓打圈等）；不通过 -> 回落生产。"""
    kind = action.get("action")
    seat = snapshot.get("seat", -1)
    phase = snapshot.get("phase")
    hand = snapshot.get("my_hand") or []
    god = snapshot.get("god") or {}
    restricted = bool(god.get("catch_play")) and god.get("god_discarder_seat") != seat
    if phase == "draw":
        if snapshot.get("turn") != seat:
            return False
        if kind == "hu":
            return can_hu(snapshot, rules, gang_open=gang_open)
        if kind == "discard":
            tile = action.get("tile")
            if tile not in hand:
                return False
            return not restricted or tile == snapshot.get("drawn_tile")
        if kind == "gang":
            tile = action.get("tile")
            if snapshot.get("wall_remaining", 99) <= 20 or tile == JOKER or tile not in hand:
                return False
            if restricted:
                return tile == snapshot.get("drawn_tile") and hand.count(tile) >= 4
            return True
        return False
    if phase in ("response_peng", "response_chi"):
        if seat not in (snapshot.get("responding_seats") or []):
            return False
        if kind == "pass":
            return True
        if restricted:
            return False
        window = snapshot.get("window_tile") or snapshot.get("last_discard") or snapshot.get("discarded_tile")
        if not window or window == JOKER:
            return False
        if kind == "peng":
            return phase == "response_peng" and hand.count(window) >= 2
        if kind == "gang":
            return (phase == "response_peng" and hand.count(window) >= 3
                    and snapshot.get("wall_remaining", 99) > 20)
        if kind == "chi":
            from .melds import can_chi
            own = list(action.get("tiles") or [])
            return (phase == "response_chi" and len(own) == 2 and can_chi(_melds_for_seat(snapshot, seat))
                    and all(own.count(t) <= hand.count(t) for t in own))
    return False


def _nn_try(snapshot, rules, gang_open, mc_ctx):
    """网络策略（开关 nn_policy_enabled）。返回动作或 None（回落）。原因/耗时写进 ``mc_ctx["nn_log"]``。"""
    try:
        from .nn.policy import nn_action
        action, log = nn_action(snapshot, mc_ctx)
        if action is not None and not _nn_valid(snapshot, action, rules, gang_open):
            action, log["fallback"] = None, "invalid_by_production_check"
    except Exception as exc:   # noqa: BLE001
        action, log = None, {"fallback": "exception:%r" % (exc,)}
    if mc_ctx is not None:
        mc_ctx["nn_log"] = log
    return action


def _gang_strict_ok(snapshot, action):
    """gang_strict_enabled（默认 0）：补杠/暗杠只在"不降向听、且不破坏爆头/七对路线"时才杠。
    before = 现手牌（14 张）打掉最好的一张后的最小离胡距离（标准/七对取小）；
    after  = 杠完之后、补牌前的手牌的向听（暗杠：去掉 4 张、副露+1 的 10 张形；补杠：去掉 1 张进已有碰的 13 张形）。
    不允许：after > before；before 里存在"打一张后爆头"而杠完不是爆头；门清时七对严格好于标准、暗杠会毁掉七对路线。"""
    from .mc.fast import is_baotou
    from .shanten import pair_shanten, shanten
    hand = snapshot.get("my_hand") or []
    seat = snapshot.get("seat", -1)
    mg = _meld_count(snapshot, seat)
    t = TILE_INDEX[action.get("tile")]
    c14 = list(to_counts(hand))
    std_best, pair_best, bt_before = 99, 99, False
    for i, n in enumerate(c14):
        if not n:
            continue
        c = list(c14)
        c[i] -= 1
        std_best = min(std_best, shanten(tuple(c), mg))
        if mg == 0:
            pair_best = min(pair_best, pair_shanten(tuple(c)))
        if is_baotou(tuple(c), mg):
            bt_before = True
    before = min(std_best, pair_best)
    c = list(c14)
    if c14[t] >= 4:      # 暗杠
        c[t] -= 4
        after = shanten(tuple(c), mg + 1)
        bt_after = is_baotou(tuple(c), mg + 1)
        if mg == 0 and pair_best < std_best:
            return False
    else:                # 补杠
        c[t] -= 1
        after = shanten(tuple(c), mg)
        bt_after = is_baotou(tuple(c), mg)
    if after > before:
        return False
    if bt_before and not bt_after:
        return False
    return True


def _resid_try(snapshot, action, rules, mc_ctx):
    """"生产 + 修正"出牌模型（开关 nn_resid_enabled）：在生产前 K 名候选里重新排序，起点 == 生产。任何异常/超时 -> 沿用生产。"""
    try:
        from .nn.resid import rescore
        new, log = rescore(snapshot, action, mc_ctx, rules)
    except Exception as exc:   # noqa: BLE001
        new, log = None, {"fallback": "exception:%r" % (exc,)}
    if mc_ctx is not None and log:
        mc_ctx["resid_log"] = log
    return new or action


def choose_action(snapshot, rules=None, gang_open=False, mc_ctx=None):
    """生产决策 + 可选的网络策略 / "生产+修正"出牌模型(nn_resid_enabled) / 实时 MC 覆盖。``mc_ctx``（可选 dict）见 ``mj/mc/decide.py``。
    开关 ``nn_policy_enabled`` / 三个 ``mc_*_enabled`` 全为 0（默认）时直接返回生产动作，不 import mj.nn / mj.mc、
    不碰快照（tests/test_mc_bot.py、tests/test_nn_policy.py 钉死）。网络动作被生产校验否决、超时或异常 -> 回落生产。"""
    w = load_weights()
    if w.get("nn_policy_enabled", 0):
        nn = _nn_try(snapshot, rules, gang_open, mc_ctx)
        if nn is not None:
            return nn
    action = _choose_action_production(snapshot, rules, gang_open)
    if action is None:
        return None
    if (w.get("gang_strict_enabled", 0) and action.get("action") == "gang" and snapshot.get("phase") == "draw"
            and not _gang_strict_ok(snapshot, action)):
        action = _choose_action_production(snapshot, rules, gang_open, no_gang=True)   # 不满足更严的杠门槛：改走不杠的分支
        if action is None:
            return None
    # xw_gang_enabled（默认 0，2026-10-05）：仿玄武-2346 的杠风格——生产要杠时他 77% 不自摸杠、未听牌时明杠 100% 不杠。
    # 能胡的局面（杠开/爆头杠）不动；自摸杠改走不杠分支，明杠改为碰（碰不了就过）。
    if w.get("xw_gang_enabled", 0) and action.get("action") == "gang":
        if snapshot.get("phase") == "draw" and not can_hu(snapshot, rules, gang_open=gang_open):
            action = _choose_action_production(snapshot, rules, gang_open, no_gang=True)
            if action is None:
                return None
        elif snapshot.get("phase") == "response_peng":
            action = choose_peng(snapshot) or {"action": "pass", "tile": ""}
    if w.get("nn_resid_enabled", 0) and action.get("action") == "discard":
        action = _resid_try(snapshot, action, rules, mc_ctx)
    if not (w.get("mc_hu_enabled", 0) or w.get("mc_discard_enabled", 0) or w.get("mc_menqing_enabled", 0)):
        return action
    try:
        from .mc.decide import mc_override
        override = mc_override(snapshot, action, rules, mc_ctx)
    except Exception:   # noqa: BLE001 — MC 任何异常都回落生产选择
        return action
    return override or action


def _choose_action_production(snapshot, rules=None, gang_open=False, no_gang=False):
    seat = snapshot.get("seat", -1)
    phase = snapshot.get("phase")
    god = snapshot.get("god") or {}
    catch_restricted = bool(god.get("catch_play")) and god.get("god_discarder_seat") != seat
    if phase == "draw" and snapshot.get("turn") == seat:
        if can_hu(snapshot, rules, gang_open=gang_open):
            bomb = None if no_gang else (_gang_bomb_choice(snapshot) or _baotou_gang_open_choice(snapshot))
            if bomb:
                return bomb
            act = choose_hu_or_piao(snapshot, hu_result(snapshot, rules, gang_open=gang_open))
            # 抓打圈受限方只能打刚摸到的牌：能胡时生产若返回"弃胡去打别的牌"（飘/弃小胡），
            # 在受限方是非法动作（arena2 发现 3 个局面），改为直接胡。
            if (catch_restricted and act and act.get("action") == "discard"
                    and act.get("tile") != snapshot.get("drawn_tile")):
                return {"action": "hu", "tile": snapshot.get("drawn_tile") or ""}
            return act
        if catch_restricted:
            drawn = snapshot.get("drawn_tile")
            if not drawn:
                return None
            hand = snapshot.get("my_hand") or []
            # P0 修复：my_hand 含 drawn_tile 本身，暗杠需要 hand.count(drawn)
            # 达到 4 张（不是 3 张）——与 responses.choose_gang() 的修复
            # 口径完全一致，见该函数文档字符串。
            if not no_gang and drawn != "白" and hand.count(drawn) >= 4 and snapshot.get("wall_remaining", 99) > 20:
                return {"action": "gang", "tile": drawn}
            return {"action": "discard", "tile": drawn}
        keep = None if no_gang else _bu_defer_tile(snapshot)
        if not no_gang and snapshot.get("drawn_tile") and snapshot.get("wall_remaining", 99) > 20:
            gang = choose_gang(snapshot, skip=keep)
            if gang:
                return gang
        if keep:
            return choose_discard(snapshot, {**(rules or {}), "_keep": (keep,)})
        return choose_discard(snapshot, rules)
    if phase == "response_peng" and seat in (snapshot.get("responding_seats") or []):
        if catch_restricted:
            # 抓打圈受限方（非打财神者本人）：response_peng 窗口一律 pass，
            # 不得继续调用 response_gang_take/choose_peng——受限方在抓打圈
            # 内没有任何合法响应权，只有打财神者本人（god_discarder_seat
            # ==seat）豁免，仍可正常判定明杠/peng。
            return {"action": "pass", "tile": ""}
        # 接入指南：response_peng 窗口允许明杠，且明杠优先于 peng——先检
        # 查最小明杠门禁（response_gang_take），不满足再落回原有 peng/pass。
        # 七对保护与死路线逃逸由 responses 门控统一裁决（pair 路线存活判定）
        return response_gang_take(snapshot) or choose_peng(snapshot) or {"action": "pass", "tile": ""}
    if phase == "response_chi" and seat in (snapshot.get("responding_seats") or []):
        if catch_restricted:
            # 同上：抓打圈受限方 response_chi 窗口一律 pass，不得继续调用
            # choose_chi——打财神者本人豁免，仍可正常判定吃。
            return {"action": "pass", "tile": ""}
        if not _can_chi(snapshot):
            return {"action": "pass", "tile": ""}
        return choose_chi(snapshot) or {"action": "pass", "tile": ""}
    return None


POLL_INTERVAL = 0.2
POLL_MIN_GAP = 0.3
# 2026-10-05 响应窗口静默（docs/EXPERT_Q_LATENCY.md + 外部评审方案 A）：本场的响应窗口我们已经没事可做
# （已提交 / 已标记陈旧 / 已在 responded / 不在 responding_seats）时，静默到"首次看到该窗口的那次拉取的
# 上一次拉取的发出时刻 + RESPONSE_QUIET 秒"。窗口固定走满 1 秒，所以静默一定在关窗之前结束，不会漏掉下一个窗口；
# 省下的 /state 限速额度（每令牌 16 次/秒，10 场共用）留给真正要响应的窗口。0 = 关闭（回退只改环境变量）。
# 10/05 两次三房实测：静默生效（40 次/局），但丢失的吃碰只从 0.155 降到 0.113/局、拉取量仅降 7%，
# 出牌超时反而 0→5 次/240 局（疑似窗口并非总是走满 1 秒）。按事先约定：未达标 → 默认关闭，v1.1 不带。
RESPONSE_QUIET = float(os.environ.get("MJ_RESPONSE_QUIET", "0"))


def _window_id(snapshot):
    """响应窗口身份：同一窗口内不变（不含 seq / responding_seats），换窗口必变。"""
    melds = snapshot.get("melds") or []
    return (snapshot.get("round_no"), snapshot.get("phase"), snapshot.get("wall_remaining"),
            sum(len(m) for m in melds if isinstance(m, list)), snapshot.get("last_discard"))
# 独立复核 P0-1 规则5：watchdog（无 notify 时的低频全量刷新兜底）必须
# 明显低频，建议每局 >=2 秒——配合每次请求都经过 mj.api.MahjongApi 的全局
# pacer（每令牌 16 次/秒），保证 10 场合计不超过官方限制，不需要额外的
# 跨对局协调（pacer 本身就是按令牌共享的排队机制）。notify 到达时仍然
# 立即唤醒（wake.set()），不受本间隔限制——只有"无事可做、等下一次机会"
# 的空闲分支改用本间隔，不引入新的忙轮询。
WATCHDOG_INTERVAL = 5.0
RECOVERY_WATCHDOG_INTERVAL = 1.0


def _response_window_key(game_id, snapshot, returned_seq):
    """P0-2 修复：响应窗口"至多提交一次"必须按稳定窗口身份判断，不能只用
    phase 和 seat——陈旧快照会在同一个 phase 字面值下重复出现（同一 round
    内连续多次 response_peng/response_chi 窗口，phase 字符串不变，但对应
    完全不同的物理弃牌/响应者组合；旧版 ``responded`` 集合只在 phase 变化
    时清空，导致第二个同 phase 窗口被误判为"已经响应过"而永久跳过，直到
    超时）。

    P0-2 独立复核发现的缺陷修复：窗口 seq 分量不得继续读取
    ``snapshot.get("seq")``——生产快照本身不一定携带内层 seq 字段，真正
    权威的序号是 ``/state`` 响应**外层**的 ``response["seq"]``（由
    ``_play_game_loop`` 在拿到响应后立即算出并显式传入本函数）。调用方
    必须显式传入 ``returned_seq`` 参数，不再由本函数自行从 snapshot 猜测。

    稳定窗口标识组成（缺失字段以 None 占位，不影响整体元组的可比较/可
    哈希性）：
    - game_id；
    - round_no；
    - phase；
    - window_tile（当前弃出的牌，与 ``responses._discarded()`` 相同的
      三路回退：window_tile / last_discard / discarded_tile）；
    - returned_seq（调用方显式传入的外层响应 seq，用于区分同一 phase 下
      的连续窗口——同一轮内连续两次弃出相同的牌、相同响应者组合时，仍能
      通过外层 seq 的推进区分为两个不同窗口，分别允许响应）；
    - responding_seats 的规范化（排序去重）表示。
    """
    window_tile = (snapshot.get("window_tile") or snapshot.get("last_discard")
                   or snapshot.get("discarded_tile"))
    responding = tuple(sorted(set(snapshot.get("responding_seats") or [])))
    return (
        game_id,
        snapshot.get("round_no"),
        snapshot.get("phase"),
        window_tile,
        returned_seq,
        responding,
    )


# B3 生产接入常量：
# - ANOMALY_WINDOW_BEFORE/AFTER：异常窗口前后各保留多少个事件（有限
#   窗口，内存占用是 O(window) 常量，不随对局时长无限增长）。
# - HIGH_LATENCY_THRESHOLD_MS：state/action 请求延迟超过此值即视为异常，
#   触发完整记录 + 异常窗口，不再走正常态的低频采样路径。
# - STATE_SAMPLE_RATE：正常状态段落起点的稳定采样率（stable_sample_state，
#   同一 state_hash 在同一采样率下采样结果确定性可复现）。
ANOMALY_WINDOW_BEFORE = 20
ANOMALY_WINDOW_AFTER = 20
HIGH_LATENCY_THRESHOLD_MS = 1500.0
STATE_SAMPLE_RATE = 0.05

_UNSET = object()


class _GameObserver:
    """B3 生产接入：状态去重 + 异常窗口的每对局观测器。

    每个 game_id 必须使用**独立**的 ``_GameObserver`` 实例（``play_game()``
    在函数体内创建局部变量，天然不跨线程/跨对局共享任何可变状态——不存在
    模块级/类级共享字典，两个并发对局线程各自持有自己的
    ``ConsecutiveStateDeduper``/``AnomalyRingBuffer``，互不污染）。

    全部方法只做纯内存操作（去重器是 O(1) 状态机，环形缓冲是 O(window)
    有界队列）+ ``log.append()`` 的非阻塞 enqueue（``AsyncDecisionLog``
    已经保证 enqueue 不阻塞、不做磁盘 I/O）——不会给动作关键路径引入新的
    阻塞或磁盘 I/O。
    """

    def __init__(self, log, game_id):
        self._log = log
        self._game_id = game_id
        self._dedup = ConsecutiveStateDeduper()
        self._anomaly = AnomalyRingBuffer(before=ANOMALY_WINDOW_BEFORE, after=ANOMALY_WINDOW_AFTER)
        self._last_state_hash = _UNSET

    def observe_state(self, state_hash, snapshot, timestamp):
        """B3：每次拿到一个新的规范化状态时调用（normalize(snapshot) 之后
        算出的 state_hash）。

        - 事件先送入异常环形缓冲（作为潜在的 before/after 上下文）——存入
          的是 ``replay_dict(state)``（DecisionState 的有界可序列化字段：
          seat/phase/turn/hand/drawn_tile/dealer/wall_remaining/discards/
          melds_all/responding_seats/chain_count/piao/round_no），不是原始
          response，保证 anomaly_window 的 before/after 可以真正复盘状态；
        - 连续相同 state_hash 只在段落切换时落一条汇总（first_seen/
          last_seen/repeat_count），不逐条重复落盘；
        - 段落切换（新状态第一次出现）时按 state_hash 稳定采样决定是否
          记录完整快照（``state_sample``，同样携带 replay_dict）——异常
          状态永远走 ``mark_anomaly`` 完整记录，不受本采样率影响。
        """
        if state_hash is None:
            return
        state = _normalize_state(snapshot)
        replay = _replay_dict(state) if state is not None else None
        event = {"state_hash": state_hash, "time": timestamp, "state": replay}
        self._anomaly.record(event)
        self._drain_anomaly_windows()
        is_new_segment = state_hash != self._last_state_hash
        summary = self._dedup.observe(state_hash, timestamp)
        if summary is not None:
            self._log.append("state_dedup_summary", {"game_id": self._game_id, **summary})
        if is_new_segment:
            self._last_state_hash = state_hash
            if stable_sample_state(state_hash, sample_rate=STATE_SAMPLE_RATE):
                self._log.append("state_sample", {
                    "game_id": self._game_id, "state_hash": state_hash,
                    "time": timestamp, "state": replay,
                })

    def mark_anomaly(self, anomaly_type, **fields):
        """B3 最低异常清单：TimeoutError / ApiError / 409 action_rejected /
        fallback_sent / 超阈值 state-action 请求延迟，全部经此方法统一
        标记——异常状态必须完整记录（不走正常态采样），并触发
        ``anomaly_window`` 前后有限窗口输出。"""
        event = {"type": anomaly_type, "game_id": self._game_id, "time": utc_now(), **fields}
        self._anomaly.mark_anomaly(event)
        self._drain_anomaly_windows()

    def _drain_anomaly_windows(self):
        for window in self._anomaly.drain_windows():
            self._log.append("anomaly_window", {
                "game_id": self._game_id,
                "anomaly": window["anomaly"],
                "before": window["before"],
                "after": window["after"],
                # P0 返修：pending windows 超出 AnomalyRingBuffer.max_pending
                # 时会淘汰最旧窗口，累计淘汰次数随每条 anomaly_window 一起
                # 输出，供下游判断"是否曾经发生过 pending 溢出、丢了多少个
                # 未完成窗口"，不需要额外的同步 I/O 或独立输出通道。
                "pending_overflow_count": self._anomaly.pending_overflow_count,
            })

    def flush(self):
        """对局结束或异常退出时必须调用：把去重器里尚未切段的汇总，以及
        环形缓冲里仍在等待更多 after 事件的窗口，全部作为\"流提前结束\"的
        正常情况落盘，不允许因为对局提前退出而丢失这部分可观测性。"""
        summary = self._dedup.flush()
        if summary is not None:
            self._log.append("state_dedup_summary", {"game_id": self._game_id, **summary})
        for window in self._anomaly.finalize_pending():
            self._log.append("anomaly_window", {
                "game_id": self._game_id,
                "anomaly": window["anomaly"],
                "before": window["before"],
                "after": window["after"],
                "pending_overflow_count": self._anomaly.pending_overflow_count,
            })


class _SeqTracker:
    """独立复核 P0-1 返修：notify-driven canonical snapshot 获取的每对局
    序号跟踪器。

    **如实命名说明（独立复核明确要求）**：本类实现的是"notify 只作唤醒/
    去重信号，状态永远用 ``seq=0`` 取服务端权威全量 snapshot"的最小可靠
    方案——不是增量事件重放器（incremental event reducer）。官方协议
    ``seq=0`` 返回全量 snapshot，``seq=N`` 通常只返回 N 之后的 events、
    不保证含 snapshot；上一版实现错误地假设 ``seq=N`` 请求也会稳定带回
    snapshot，实际生产会遇到 events-only 响应（``snapshot`` 缺失），旧版
    ``_play_game_loop`` 在 ``not snapshot`` 时直接 ``continue``——不消费
    events、不推进 processed_seq、不产出可决策快照，导致对同一 seq 反复
    发起请求、永远拿不到新决策依据（真实生产 P0 阻断缺陷）。本类修复后
    **请求参数永远是 seq=0**，``next_request()`` 返回的 ``trigger`` 只是
    "为什么现在要刷新"的可观测性标签（notify/watchdog/recovery），不影响
    请求本身取的是全量快照还是增量——本方案没有增量路径。

    与 ``_GameObserver`` 同样原则——每个 game_id 使用**独立**实例（局部
    变量，不跨对局共享）。但与 ``_GameObserver`` 不同的是，本类会被
    **两个不同线程**并发访问：``_notification_loop``（写 notified_seq）
    与主循环线程（读 notified_seq、写/读 processed_seq/recovery 标记），
    因此所有读写都在内部锁保护下进行（规则5：所有共享状态必须线程安全）。

    语义：
    - ``observe_notify(seq)``：notify 线程每收到一条通知就调用，只保留
      "目前已知最新" seq（取 max），天然合并重复/乱序到达的通知——不会
      因为重复/倒序 notify 而让下游多请求或对旧窗口重新提交动作。
    - ``next_request()``：主循环每次决定"现在要不要刷新"时调用，返回
      ``(request_seq, trigger)``；``request_seq`` 恒为 ``0``（如实反映
      "只有全量恢复路径"，不伪装成增量请求）。``trigger`` 取值：
      - ``"recovery"``：显式调用过 ``force_recovery()``（首次进入、
        gap、SSE 重连、action 409、events-only 防御性响应）之后的下一次
        请求，必须立即执行、不等待 watchdog 间隔；
      - ``"notify"``：收到了尚未消费的新通知（``notified_seq`` 大于上次
        已消费的通知 seq）；
      - ``"watchdog"``：以上都不成立，低频全量刷新兜底（避免死锁/漏
        通知），复用主循环自身既有的等待节奏，不引入新的忙轮询。
    - ``observe_response(returned_seq, gap=False)``：处理完一次 /state
      响应后调用，用响应**外层** seq 推进 ``processed_seq``；若响应显式
      标记 ``gap=True``，等价于调用一次 ``force_recovery("gap")``。
    - ``force_recovery(reason)``：显式请求下一次进入 recovery 状态
      （``next_request()`` 返回 ``trigger="recovery"``）。统一入口，供
      gap 检测、SSE 重连（``mark_reconnected`` 现在是它的别名）、action
      409（P0-3）、events-only 防御性响应（P0-1 规则8）等场景调用，原因
      文本只用于诊断记录，不影响行为。
    - ``should_notify_trigger()``：供主循环判断"本次唤醒是否由 notify
      触发"（供 state_request_metric 的 trigger 字段区分 notify/其它）。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._notified_seq = None
        self._consumed_notified_seq = None
        self._processed_seq = None
        self._force_recovery = True  # 首次进入即视为 recovery（规则4）。
        self._last_recovery_reason = "initial"
        self._sse_healthy = False

    def observe_notify(self, seq):
        if seq is None:
            return
        with self._lock:
            if self._notified_seq is None or seq > self._notified_seq:
                self._notified_seq = seq

    def force_recovery(self, reason=None):
        with self._lock:
            self._force_recovery = True
            self._last_recovery_reason = reason or "unspecified"

    def set_sse_health(self, healthy):
        """通知线程报告 SSE 连接状态；只影响无通知时的 watchdog 节奏。"""
        with self._lock:
            self._sse_healthy = bool(healthy)

    def watchdog_interval(self):
        with self._lock:
            return WATCHDOG_INTERVAL if self._sse_healthy else RECOVERY_WATCHDOG_INTERVAL

    # 向后兼容别名：SSE 重连等价于强制 recovery。
    def mark_reconnected(self):
        self.force_recovery("sse_reconnect")

    def next_request(self):
        with self._lock:
            if self._force_recovery:
                return 0, "recovery"
            if self._notified_seq is not None and (
                    self._consumed_notified_seq is None
                    or self._notified_seq > self._consumed_notified_seq):
                return 0, "notify"
            return 0, "watchdog"

    def observe_response(self, returned_seq, gap=False, trigger=None):
        with self._lock:
            if gap:
                self._force_recovery = True
                self._last_recovery_reason = "gap"
                return
            self._force_recovery = False
            if returned_seq is not None:
                if self._processed_seq is None or returned_seq > self._processed_seq:
                    self._processed_seq = returned_seq
            if trigger == "notify" and self._notified_seq is not None:
                self._consumed_notified_seq = self._notified_seq


def _last_pace_ms(api):
    local = getattr(api, "_local", None)
    v = getattr(local, "last_pace_ms", None) if local is not None else None
    return round(v, 1) if isinstance(v, (int, float)) else None


def _fetch_snapshot(api, game_id, log=None, observer=None, seq=0):
    """B3 返修：正常轮询路径不再无条件调用 ``log.state()``——去重/采样已经
    通过 ``observer.observe_state()`` 承担"减少日志量"的职责，若这里仍然
    对每次轮询都写一条 state 记录，去重效果会被这条日志本身抵消（原始
    问题：去重没有真正减少落盘条数）。``log`` 参数仍保留供调用方传入
    （签名兼容），但不再用于逐次轮询写 state 日志。

    Timeout 时明确取不到 snapshot：不伪造状态，异常标记里显式携带
    ``state_unavailable=True``，供下游区分"这条异常窗口的 before/after
    确实缺状态"与"状态字段恰好是 None"。

    独立复核 P0-1 返修：``seq`` 默认仍为 0（保持向后兼容——测试/旧调用方
    不传时行为不变）。``_play_game_loop`` 实际调用时同样恒定传入 0——
    本方案是 notify-driven canonical snapshot 获取，不是增量请求，见
    ``_SeqTracker`` 类文档字符串的如实命名说明。``timeout=2.0`` 只是
    普通 HTTP 客户端读超时，不是长轮询语义的一部分（规则6：不再用会被
    当作异常的增量长轮询）。
    """
    started = time.monotonic()
    try:
        response = api.state(game_id, seq, timeout=2.0)
    except (TimeoutError, socket.timeout):   # 3.9：socket.timeout 不是 TimeoutError
        if observer is not None:
            observer.mark_anomaly("timeout", metric="state_request", state_unavailable=True,
                                   elapsed_ms=round((time.monotonic() - started) * 1000, 3))
        return None, None
    elapsed_ms = (time.monotonic() - started) * 1000
    if observer is not None and elapsed_ms > HIGH_LATENCY_THRESHOLD_MS:
        observer.mark_anomaly("high_latency", metric="state_request_ms",
                               elapsed_ms=round(elapsed_ms, 3))
    return response, response.get("snapshot")


def _notification_loop(api, game_id, wake, log=None, tracker=None):
    """独立复核 P0-1 返修：notify 到达时把携带的 seq 喂给 ``tracker``
    （若提供），仅供主循环判断"是否有尚未消费的新通知"（notify 触发
    watchdog 之外的额外唤醒）——**不是**增量请求的驱动信号，因为
    ``_SeqTracker.next_request()`` 恒定请求 ``seq=0`` 全量权威快照
    （如实命名：notify-driven canonical snapshot，不是 incremental event
    reducer）。``tracker`` 缺省时（如旧测试/调用方未传入）行为与之前
    完全一致，只承担"唤醒"职责。

    连续失败达到上限（``failures>=5``，判定为通知连接不可用）时，若提供
    了 ``tracker``，显式调用 ``force_recovery("notifier_unavailable")``
    ——下一次主循环的 ``next_request()`` 会强制走 recovery（立即刷新，不
    等待 watchdog 间隔），不假设断线期间发生了什么；watchdog（主循环自身
    的低频轮询节奏）此时仍会按既有节奏继续推进，不依赖 notify 是否存活
    （规则：notifier 失效时 watchdog 仍可推进）。
    """
    failures = 0
    while failures < 5:
        try:
            for payload in notify_sequences(api, game_id):
                failures = 0          # 2026-10-05：收到数据即清零，只有"连续"失败才计数（旧版只增不减，正常关流也累计）
                if tracker is not None:
                    tracker.set_sse_health(True)
                if log:
                    log.notify(game_id, payload)
                if tracker is not None:
                    tracker.observe_notify(payload.get("seq"))
                wake.set()
                if payload.get("closed"):
                    return
            failures += 1
        except Exception:
            failures += 1
            if tracker is not None:
                tracker.set_sse_health(False)
                tracker.force_recovery("sse_disconnected")
        time.sleep(2)
    if tracker is not None:
        tracker.set_sse_health(False)   # 放弃通知后主循环改用 1 秒看门狗，而不是 5 秒
        tracker.force_recovery("notifier_unavailable")
    if log:
        log.append("notifier_exit", {"game_id": game_id, "failures": failures})
    wake.set()


def _load_ab(spec, seed=None):
    """``--ab A.json,B.json[,C.json...]``：两份及以上权重覆盖（JSON 对象；空串/"current"/"-" = 当前配置，不覆盖）。
    返回 dict（含记录用的种子）。组名依次为 A、B、C……；房间按进入顺序轮流分组（各组房数相差不超过 1）。"""
    parts = [x.strip() for x in str(spec).split(",")]
    if len(parts) < 2 or len(parts) > 6:
        raise ValueError("--ab 需要 2~6 项：A.json,B.json[,C.json]")
    overlays, names = [], []
    for part in parts:
        if part in ("", "current", "-"):
            overlays.append({})
            names.append("current")
            continue
        with open(part, encoding="utf-8") as f:
            text = f.read().strip()
        obj = json.loads(text) if text else {}
        if not isinstance(obj, dict):
            raise ValueError("%s 不是 JSON 对象" % part)
        overlays.append(obj)
        names.append(os.path.splitext(os.path.basename(part))[0])
    if seed is None:
        seed = int.from_bytes(os.urandom(4), "big")
    return {"labels": tuple("ABCDEF"[:len(parts)]), "names": tuple(names), "overlays": tuple(overlays),
            "seed": int(seed), "memo": {}, "lock": threading.Lock()}


def _ab_pick(seed, room_id, n=2):
    """这一房用第几组：由（种子, 房号）的哈希决定——随机、可复现、同一房永远同一结果。"""
    return int(hashlib.md5(("%s:%s" % (seed, room_id)).encode()).hexdigest()[:8], 16) % n


def _ab_assign(ab, room_id, log):
    memo = ab.get("memo")
    if memo is not None:
        # 轮流分组：起点由种子决定；同一进程里同一房重进（--wait 内部重启）仍用原组
        with ab["lock"]:
            if room_id not in memo:
                memo[room_id] = (ab["seed"] + len(memo)) % len(ab["labels"])
            idx = memo[room_id]
    else:
        idx = _ab_pick(ab["seed"], room_id, len(ab["labels"]))
    label, name, overlay = ab["labels"][idx], ab["names"][idx], ab["overlays"][idx]
    log.append("ab_assign", {"room_id": room_id, "label": label, "config": name, "overlay": overlay,
                              "seed": ab["seed"], "time": utc_now()})
    print("A/B：房间 %s 整房使用 %s（%s），种子 %d" % (room_id, label, name, ab["seed"]))
    return label, overlay


def _safe_fallback(snapshot, rules=None, gang_open=False):
    """choose_action 抛异常时的兜底：能胡就胡；摸牌阶段打摸到的牌（没有就打最右一张）；响应窗口一律过。
    这里本身再出错（快照残缺等）就返回 None，由调用方按"无决策"等下一次快照。"""
    try:
        phase = snapshot.get("phase")
        if phase == "draw" and snapshot.get("turn") == snapshot.get("seat"):
            try:
                if can_hu(snapshot, rules, gang_open=gang_open):
                    return {"action": "hu", "tile": snapshot.get("drawn_tile") or ""}
            except Exception:   # noqa: BLE001
                pass
            hand = snapshot.get("my_hand") or []
            tile = snapshot.get("drawn_tile") or (hand[-1] if hand else "")
            return {"action": "discard", "tile": tile} if tile else None
        if phase in ("response_peng", "response_chi"):
            return {"action": "pass", "tile": ""}
    except Exception:   # noqa: BLE001
        pass
    return None


def play_game(api, game_id, log, rules=None, overlay=None):
    try:
        fresh = api.rules()
        if isinstance(fresh, dict):
            fresh = fresh.get("config", fresh)
            if isinstance(fresh, dict) and fresh:
                rules = fresh
    except ApiError:
        pass
    # B3 生产接入：每个 game_id 一个独立的 _GameObserver 局部变量——不是
    # 模块级/类级共享状态，两个并发 play_game() 线程各自持有自己的
    # ConsecutiveStateDeduper/AnomalyRingBuffer 实例，互不污染
    # （见 _GameObserver 类文档）。函数体全部剩余逻辑包在 try/finally 里，
    # 保证正常结束（finished/404放弃）与任何异常退出都会调用
    # observer.flush()，落盘尚未切段的去重汇总与尚未完成的异常窗口。
    observer = _GameObserver(log, game_id)
    with _active_game_ids_lock:
        _active_game_ids.add(game_id)
    try:
        # 实战按房随机 A/B：这一房的配置只在本线程内生效（thread_weights_overlay），不影响同进程里别的线程
        with (thread_weights_overlay(overlay) if overlay else contextlib.nullcontext()):
            _play_game_loop(api, game_id, log, rules, observer)
    finally:
        observer.flush()
        with _active_game_ids_lock:
            _active_game_ids.discard(game_id)


def _play_game_loop(api, game_id, log, rules, observer):
    last_phase = None
    last_round = None
    responded = set()
    rejections = {}
    # 独立复核 P0-1 返修：draw 阶段 409 后不再在同一次异常处理里就地用
    # "触发 409 的那份旧 snapshot"发起 fallback（违反规则6）。fallback_attempts
    # 记录已经尝试过几次 fallback（与 rejections 分开计数），真正的 fallback
    # 决策推迟到下一次循环迭代、拿到全新权威 snapshot 并确认仍处于本人
    # draw 回合之后才做（见循环体靠前的 fallback 判定分支）。
    fallback_attempts = {}
    # P0 修复：响应窗口"至多提交一次"——按稳定窗口身份（_response_window_key）
    # 而不是 phase+seat 判断，避免陈旧快照在同一 phase 字面值下重复出现时
    # 被误判为"已响应"/被重复提交同一决定。submitted_response_windows 记录
    # 成功提交过的窗口；stale_response_windows 记录发生过 409 的窗口（发生
    # 409 后视为陈旧，强制走下一轮 /state 轮询取新状态，不再对同一窗口重试
    # 同一决定；不发送猜测式 fallback——见 choose_action() 分支）。
    submitted_response_windows = set()
    stale_response_windows = set()
    pending_gang = False
    chain_tracker = ChainTracker()   # MC 输入：本座位飘出的财神数/当前链是否含杠（快照里没有）
    last_dealer = None
    last_wall = None
    opp_chain = False
    wake = threading.Event()
    # P0/P1 修复：notify-driven canonical snapshot 获取——tracker 记录本局
    # 最新通知 seq 与是否需要立即 recovery，_notification_loop 写、主循环
    # 读写，见 _SeqTracker（如实命名：不是增量事件重放器）。
    seq_tracker = _SeqTracker()
    notifier = threading.Thread(target=_notification_loop, args=(api, game_id, wake, log, seq_tracker),
                                daemon=True)
    notifier.start()
    last_poll = 0.0
    dead_polls = 0
    prev_sent = None      # 上一次"拿到快照"的拉取的发出时刻（含排队，偏早 = 偏保守）
    prev_wid = None
    wid_quiet = 0.0       # 当前响应窗口可以静默到的时刻（0 = 不静默）
    quiet_wid = None      # 确认"这个窗口没我们的事了"的窗口：成功提交响应 / 不在 responding_seats。409 不算（视图可能已过期）
    quiet_until = 0.0
    quiet_n = 0           # 本场实际静默次数（随 state_request_metric 落盘，验收用）
    # 两次拉取之间的最小间隔：默认 POLL_MIN_GAP（0.3 秒）。2026-10-06 起可以用权重覆盖 poll_min_gap 按房设置
    # （--ab 按房随机 A/B 时 thread_weights_overlay 只作用于本房的对局线程），用来实测"缩短窗口反应时间"。
    poll_min_gap = float(load_weights().get("poll_min_gap", POLL_MIN_GAP))

    def _quiet_target():
        if prev_wid is None or prev_wid != quiet_wid:
            return 0.0
        return wid_quiet if wid_quiet > time.monotonic() else 0.0

    while True:
        if quiet_until:
            delay = quiet_until - time.monotonic()
            quiet_until = 0.0
            if delay > 0:
                quiet_n += 1
                time.sleep(min(delay, RESPONSE_QUIET))
                wake.clear()          # 丢掉窗口内别家过/超时触发的唤醒；关窗事件一定在静默结束之后才来
                wake.wait(0.3)
        wake.clear()
        gap = poll_min_gap - (time.monotonic() - last_poll)
        if gap > 0:
            time.sleep(gap)
        last_poll = time.monotonic()
        request_seq, trigger = seq_tracker.next_request()
        try:
            fetch_started = time.monotonic()
            response, snapshot = _fetch_snapshot(api, game_id, log, observer, seq=request_seq)
        except ApiError as error:
            log.error(game_id, error)
            observer.mark_anomaly("api_error", metric="state_request",
                                   status=error.status, phase="poll")
            if error.status == 404 and not is_transient(error):
                # 指南 v35：404 下 TOURNAMENT_GONE（暂时不可达，应重试）与
                # TOURNAMENT_NOT_FOUND/GAME_NOT_FOUND（不存在，应放弃）语义相反，
                # 必须按 body 的 code 判型。此前只看 status==10 次即放弃——实测
                # 房间 a_e2eb6e654532 的 7 张桌各收到正好 10 次 404 后被整桌放弃。
                dead_polls += 1
                if dead_polls >= 10:
                    print("对局不存在，放弃:", game_id, error.code or "(无 code)")
                    return
            elif error.status == 404:
                dead_polls = 0          # 可重试的 404 不计入放弃计数
            else:
                dead_polls = 0
            time.sleep(1)
            continue
        dead_polls = 0
        if response is None:
            # _fetch_snapshot 内部超时：既没有 response 也没有 snapshot，
            # 不构成"events-only"场景（规则8只针对确实收到了 response 但
            # 缺 snapshot 的情形），按既有节奏重试即可。
            wake.wait(POLL_INTERVAL)
            continue
        if not snapshot:
            # 独立复核 P0-1 规则8：官方协议 seq=N 通常只返回 events、不保证
            # 含 snapshot；本方案虽然恒定请求 seq=0（应当总能拿到全量
            # snapshot），但仍需防御性处理"服务端意外返回 events-only
            # 响应"的场景——不得死循环、不得基于不完整 events 直接决策：
            # 记录 metric、把可获得的最高 events seq 作为诊断信息推进，
            # 强制下一次请求走 recovery（seq=0 全量恢复），本轮不产出任何
            # 决策。
            events = response.get("events") or []
            max_event_seq = None
            for event in events:
                event_seq = event.get("seq") if isinstance(event, dict) else None
                if event_seq is not None and (max_event_seq is None or event_seq > max_event_seq):
                    max_event_seq = event_seq
            log.state_request_metric(
                game_id=game_id, requested_seq=request_seq,
                returned_seq=response.get("seq", max_event_seq),
                trigger=trigger, elapsed_ms=round((time.monotonic() - fetch_started) * 1000, 3),
                pending=bool(response.get("pending")), gap=True, state_hash=None,
            )
            observer.mark_anomaly("events_only_response_without_snapshot",
                                   metric="state_request", requested_seq=request_seq,
                                   max_event_seq=max_event_seq, event_count=len(events))
            seq_tracker.force_recovery("events_only_response")
            wake.wait(POLL_INTERVAL)
            continue
        # P1 修复：紧凑的 state_request_metric（不记录完整快照）——供
        # mj.timing 统计 notify 利用率/seq gap/watchdog 次数（不再是恒为
        # 0 的 notify_to_state_matches）。
        returned_seq = response.get("seq")
        response_gap = bool(response.get("gap"))
        pace_ms_now = _last_pace_ms(api)
        log.state_request_metric(
            game_id=game_id, requested_seq=request_seq, returned_seq=returned_seq,
            trigger=trigger, elapsed_ms=round((time.monotonic() - fetch_started) * 1000, 3),
            pending=bool(response.get("pending")), gap=response_gap,
            state_hash=_state_hash(_normalize_state(snapshot, rules=rules))
            if snapshot else None,
            pace_ms=pace_ms_now, n429=getattr(type(api), "count_429", None), quiet_n=quiet_n,
        )
        seq_tracker.observe_response(returned_seq, gap=response_gap, trigger=trigger)
        # B3：每次拿到有效快照都规范化 + 计算 state_hash 喂给观测器——
        # 正常轮询期间连续相同状态只在段落切换时落一条汇总（不逐条重复
        # 落盘），段落切换时按 state_hash 稳定采样决定是否记录完整快照。
        poll_state = _normalize_state(snapshot, rules=rules)
        if poll_state is not None:
            observer.observe_state(_state_hash(poll_state), snapshot, utc_now())
        if response.get("finished") or snapshot.get("finished") or snapshot.get("phase") == "finished":
            log.result(game_id, response)
            return
        seat = snapshot.get("seat", -1)
        round_no = snapshot.get("round_no")
        if round_no != last_round:
            responded.clear()
            rejections.clear()
            fallback_attempts.clear()
            # 新一轮：旧窗口的标识天然不会再出现（round_no 已变），但仍
            # 显式清空两个集合，避免长局下无界增长（规则4的一部分）。
            submitted_response_windows.clear()
            stale_response_windows.clear()
            last_phase = None
            pending_gang = False
            last_round = round_no
        phase = snapshot.get("phase")
        if phase in ("response_peng", "response_chi"):
            wid = _window_id(snapshot)
            if wid != prev_wid:   # 这次拉取第一次看到这个窗口：它是在上一次拉取被处理之后才打开的
                wid_quiet = (prev_sent + RESPONSE_QUIET) if (prev_sent is not None and RESPONSE_QUIET > 0) else 0.0
            prev_wid = wid
        else:
            prev_wid = None
            wid_quiet = 0.0
        # 2026-10-05 实测修正：界限要用请求真正发出的时刻（排队等待之后），不能用排队前的时刻。
        # 限速排队 p50 约 260ms，用排队前时刻会让静默期几乎为 0（10/05 三房实测拉取量毫无下降）。
        # 服务器处理第 k−1 次请求一定在它真正发出之后，所以"关窗 > 发出时刻 + 1 秒"仍然成立。
        fetch_pace = pace_ms_now / 1000.0 if pace_ms_now is not None else 0.0
        prev_sent = fetch_started + fetch_pace
        if phase != last_phase:
            responded.clear()
            rejections.clear()
            fallback_attempts.clear()
            last_phase = phase
        wall = snapshot.get("wall_remaining")
        dealer_now = snapshot.get("dealer")
        if last_wall is not None and wall is not None and wall > last_wall + 5:
            if dealer_now == last_dealer:
                opp_chain = dealer_now != seat and dealer_now is not None
            else:
                opp_chain = dealer_now != seat and last_dealer is not None
        if wall is not None:
            last_wall = wall
        if dealer_now is not None:
            last_dealer = dealer_now
        if phase == "settled":
            wake.wait(seq_tracker.watchdog_interval())
            continue
        if seat < 0:
            wake.wait(seq_tracker.watchdog_interval())
            continue
        response_window_key = None
        if phase in ("response_peng", "response_chi"):
            response_window_key = _response_window_key(game_id, snapshot, returned_seq)
            # 至多提交一次：同一窗口已经成功提交过，或已经因 409 被标记
            # 陈旧，都不得再对同一窗口提交任何决定（含猜测式 fallback）——
            # 强制等待下一次 /state 轮询取得新窗口（新一轮/新弃牌/外层 seq
            # 推进后 _response_window_key 自然产生不同的键）。
            if response_window_key in submitted_response_windows or response_window_key in stale_response_windows:
                quiet_until = _quiet_target()
                if not quiet_until:
                    wake.wait(seq_tracker.watchdog_interval())
                continue
        if phase in ("response_peng", "response_chi") and seat in responded:
            quiet_until = _quiet_target()
            if not quiet_until:
                wake.wait(seq_tracker.watchdog_interval())
            continue
        started = time.monotonic()
        call_rules = rules
        if opp_chain and isinstance(call_rules, dict):
            call_rules = {**call_rules, "opp_chain": True}
        # 独立复核 P0-1/P0-3 规则6：draw 阶段连续 409 后，fallback 决策必须
        # 基于**这一次全新拿到的权威 snapshot**（已确认 phase=="draw" 且
        # seat 仍是本人回合），不得沿用触发 409 的那份旧 snapshot 直接
        # fallback——因此 fallback 判定放在这里（已经过本轮全新 fetch），
        # 而不是放在下面 ApiError 409 的异常处理块里就地发起。
        is_fallback = (phase == "draw" and rejections.get(phase, 0) >= 2
                       and fallback_attempts.get(phase, 0) < 2)
        mc_ctx = None
        if is_fallback:
            hand = snapshot.get("my_hand") or []
            fallback_tile = snapshot.get("drawn_tile") or (hand[-1] if hand else "")
            decision = {"action": "discard", "tile": fallback_tile} if fallback_tile else None
        else:
            mc_ctx = {"t0": started, **chain_tracker.ctx(snapshot)}
            try:
                decision = choose_action(snapshot, call_rules, gang_open=pending_gang, mc_ctx=mc_ctx)
            except Exception as error:   # noqa: BLE001
                # 2026-10-05 外部评审 P0：策略在罕见局面抛异常会结束整个对局线程，GameLedger 不会重新派发，
                # 该场剩余局全部由服务端代打。这里记日志后改用最保守的合法动作，对局继续。
                log.error(game_id, error)
                decision = _safe_fallback(snapshot, call_rules, pending_gang)
                log.append("safe_fallback", {"game_id": game_id, "phase": phase,
                                             "error": sanitize_text(repr(error), limit=200),
                                             "decision": decision})
        prepare_ms = (time.monotonic() - started) * 1000
        if not decision:
            if phase in ("response_peng", "response_chi"):   # 不在 responding_seats：这个窗口没我们的事
                quiet_wid = prev_wid
                quiet_until = _quiet_target()
            if not quiet_until:
                wake.wait(seq_tracker.watchdog_interval())
            continue
        if decision["action"] == "pass":
            responded.add(seat)
        else:
            responded.clear()
        # B1/P0-1: decision_id 必须在调用 api.action() 之前创建，不能等 API
        # 成功后才生成——这样 action_rejected/fallback_sent（发生在 API 调用
        # 失败/拒绝路径）才能和最终的 action/hu_detail 共享同一个 ID，形成
        # 完整的决策关联链（同一次决策的所有日志条目都能通过 decision_id
        # 找到）。
        #
        # P0-1 返修：仅有 decision_id 贯穿各分支还不够——如果 api.action()
        # 直接抛 TimeoutError（连响应都没收到），此前唯一记录的是
        # log.error(...)，其 payload 里根本不含 decision_id/action/tile，
        # 导致"这次超时到底尝试了什么动作"完全无法从日志里还原。现在改为：
        # 在调用 api.action() 之前，先显式写一条 decision_attempt 记录
        # （outcome='pending'），包含 decision_id/game_id/state_hash/action/
        # tile/policy_version/schema_version/config_hash/build_hash 全部
        # 字段；之后无论走到哪个分支都用同一个 decision_id 调用
        # log.decision_attempt_outcome(...) 推进明确的 outcome
        # （success/timeout/api_error/conflict_409/fallback_sent/hu），
        # 不允许任何分支跳过这一步。
        decision_id = new_decision_id()
        attempt_state = _normalize_state(snapshot, rules=call_rules)
        attempt_state_hash = _state_hash(attempt_state) if attempt_state is not None else None
        attempt_config_hash = _config_hash(call_rules if call_rules is not None else snapshot.get("rules"))
        attempt_build_hash = _build_hash()
        log.decision_attempt(
            game_id, decision_id=decision_id, round_no=snapshot.get("round_no"),
            seat=seat, state_hash=attempt_state_hash, action=decision.get("action"),
            tile=decision.get("tile"), policy_version=POLICY_VERSION,
            schema_version=SCHEMA_VERSION, config_hash=attempt_config_hash,
            build_hash=attempt_build_hash, attempted_at=utc_now(),
        )
        action_started = time.monotonic()
        try:
            api.action(game_id, **decision)
        except (TimeoutError, socket.timeout) as error:   # 3.9：socket.timeout 不是 TimeoutError
            log.error(game_id, error)
            log.decision_attempt_outcome(decision_id=decision_id, outcome="timeout",
                                          outcome_detail=sanitize_text(repr(error), limit=200),
                                          resolved_at=utc_now(), game_id=game_id)
            # B3 最低异常清单：TimeoutError 必须触发异常窗口。
            observer.mark_anomaly("timeout", metric="action_request", decision_id=decision_id,
                                   action=decision.get("action"), tile=decision.get("tile"))
            time.sleep(0.5)
            continue
        except ApiError as error:
            if error.status == 409:
                # 独立复核 P0-3：任意 action 409 后立即进入 recovery——下一次
                # /state 请求必须是 seq=0（官方指南明确：动作 409 后用 seq=0
                # 重建全量快照，不得在同一份触发 409 的旧 snapshot 上继续
                # 决策）。
                seq_tracker.force_recovery("action_409")
                rejections[phase] = rejections.get(phase, 0) + 1
                log.action_rejected(game_id, phase, decision, error, rejections[phase], snapshot,
                                     decision_id=decision_id)
                log.decision_attempt_outcome(
                    decision_id=decision_id, outcome="conflict_409",
                    outcome_detail=sanitize_text(str(error), limit=200), resolved_at=utc_now(), game_id=game_id,
                )
                # B3 最低异常清单：409 action_rejected 必须触发异常窗口。
                observer.mark_anomaly("action_rejected_409", decision_id=decision_id,
                                       phase=phase, rejections=rejections.get(phase, 0),
                                       is_fallback=is_fallback)
                if is_fallback:
                    # fallback 本身也被 409：记录一次 fallback_attempts，
                    # 交给下一次循环迭代基于全新权威 snapshot 重新判定是否
                    # 还要继续尝试（不在本次异常处理里就地重试）。
                    #
                    # 事件转换历史建模（原任务书规则6）：同一 decision_id
                    # 先记录 outcome="conflict_409"（上面已记录），再追加
                    # 一条 outcome="fallback_sent"（accepted=False）——这是
                    # 同一次决策的完整终态转换历史，不是重复脏数据，下游
                    # （mj.timing）按 decision_id 取最后一条视为 final_outcome
                    # 即可正确统计。
                    fallback_attempts[phase] = fallback_attempts.get(phase, 0) + 1
                    log.fallback_sent(game_id, snapshot, decision.get("tile"), False, error,
                                       decision_id=decision_id)
                    log.decision_attempt_outcome(
                        decision_id=decision_id, outcome="fallback_sent",
                        outcome_detail=sanitize_text(
                            f"tile={decision.get('tile')} accepted=False error={error}", limit=200),
                        resolved_at=utc_now(), game_id=game_id,
                    )
                elif phase in ("response_peng", "response_chi") and response_window_key is not None:
                    # P0 修复：响应阶段发生 409 后立即标记该窗口陈旧
                    # （不等到第二次 409），且绝不发送猜测式 fallback——
                    # 强制走下一次 /state 轮询取得新状态，新窗口出现后才
                    # 允许提交新的响应决定（_response_window_key 的
                    # round_no/window_tile/returned_seq 任一推进都会产生
                    # 新键）。
                    stale_response_windows.add(response_window_key)
                    responded.add(seat)
                time.sleep(0.1)
                continue
            log.error(game_id, error)
            log.decision_attempt_outcome(decision_id=decision_id, outcome="api_error",
                                          outcome_detail=sanitize_text(str(error), limit=200),
                                          resolved_at=utc_now(), game_id=game_id)
            # B3 最低异常清单：非 409 ApiError 也必须触发异常窗口。
            observer.mark_anomaly("api_error", metric="action_request", decision_id=decision_id,
                                   status=error.status)
            time.sleep(0.5)
            continue
        action_elapsed_ms = (time.monotonic() - action_started) * 1000
        chain_tracker.observe(snapshot, decision)   # 服务端已接受这个动作
        if action_elapsed_ms > HIGH_LATENCY_THRESHOLD_MS:
            # B3 最低异常清单：超过明确阈值的 action 请求延迟必须触发异常窗口。
            observer.mark_anomaly("high_latency", metric="action_request_ms",
                                   elapsed_ms=round(action_elapsed_ms, 3), decision_id=decision_id)
        if phase in ("response_peng", "response_chi") and response_window_key is not None:
            # P0 修复：成功提交后不再对同一窗口提交第二次。
            submitted_response_windows.add(response_window_key)
            quiet_wid = prev_wid
            quiet_until = _quiet_target()   # 成功提交响应后：窗口剩余时间不再为本场拉取
        if is_fallback:
            log.fallback_sent(game_id, snapshot, decision.get("tile"), True, decision_id=decision_id)
            log.decision_attempt_outcome(decision_id=decision_id, outcome="fallback_sent",
                                          outcome_detail=f"tile={decision.get('tile')} accepted=True",
                                          resolved_at=utc_now(), game_id=game_id)
            # B3 最低异常清单：fallback_sent 必须触发异常窗口（无论是否被
            # 接受——fallback 本身就是异常恢复路径）。
            observer.mark_anomaly("fallback_sent", decision_id=decision_id,
                                   tile=decision.get("tile"), accepted=True)
        if (decision["action"] == "discard" and decision.get("tile") == "白"
                and phase == "draw" and can_hu(snapshot, rules, gang_open=pending_gang)):
            log.piao_attempt(game_id, snapshot, decision_id=decision_id)
        if decision["action"] == "hu":
            result = hu_result(snapshot, rules, gang_open=pending_gang) or {}
            log.append("hu_detail", {
                "decision_id": decision_id,
                "game_id": game_id,
                "seat": snapshot.get("seat"),
                "round_no": snapshot.get("round_no"),
                "fan": result.get("fan"),
                "detail": result.get("detail"),
                "chain_count": canonical_chain_count(snapshot),
                "piao": canonical_piao(snapshot),
                "hand": snapshot.get("my_hand"),
                "drawn_tile": snapshot.get("drawn_tile"),
                **local_estimate_marker(),
                # B4：在线阶段的 /state 快照不提供服务端 round_ended 结算
                # 事件，必须显式标记"服务端结算不可得"，不能假装已经对账，
                # 也不能发明假的 server_truth——真正的服务端结算真相只能
                # 通过赛后 tools/pull_all_events.py 拉取门户事件流后用
                # data_tool import-events 补齐（见 data/README.md）。
                **server_truth_unavailable_marker(),
            })
            log.decision_attempt_outcome(decision_id=decision_id, outcome="hu",
                                          outcome_detail=f"fan={result.get('fan')}",
                                          resolved_at=utc_now(), game_id=game_id)
        elif not is_fallback:
            # is_fallback 分支已经在上面记录了 outcome="fallback_sent"，
            # 不再重复写 outcome="success"（同一 decision_id 的事件转换
            # 历史只应记录一次真正的终态，不能既是 fallback_sent 又是
            # success——这两个是同一次 fallback 尝试的唯一终态，不是
            # "先 409 后 fallback_sent"那种真实存在的多阶段转换）。
            log.decision_attempt_outcome(decision_id=decision_id, outcome="success",
                                          outcome_detail=None, resolved_at=utc_now(), game_id=game_id)
        pending_gang = decision["action"] == "gang"
        log.action(
            game_id,
            snapshot,
            decision,
            client_prepare_ms=prepare_ms,
            action_request_ms=(time.monotonic() - action_started) * 1000,
            opp_chain=opp_chain,
            rules=call_rules,
            decision_id=decision_id,
            **({"mc": {**(mc_ctx.get("mc_log") or {}), **({"nn": mc_ctx["nn_log"]} if mc_ctx.get("nn_log") else {})}}
               if mc_ctx and (mc_ctx.get("mc_log") or mc_ctx.get("nn_log")) else {}),
        )
        if decision["action"] == "pass" and phase in ("response_peng", "response_chi"):
            log.append("pass_window", {
                "game_id": game_id,
                "seat": snapshot.get("seat"),
                "phase": phase,
                "hand": snapshot.get("my_hand"),
                "melds": snapshot.get("melds"),
                "god": snapshot.get("god"),
            })


_MATCH_PERMANENT_ERROR_MARKERS = {
    "FEATURE_DISABLED": "管理页已关闭自由匹配/测试房功能（永久条件，需人工介入，不应重试）。",
    "TOKEN_NOT_SCOPED": "误用了报名/测试令牌调用自由匹配接口——自由匹配需要全局令牌"
                        "（门户「我的AI身份」令牌），不是赛事报名令牌或测试房令牌。",
    "PORTAL_BINDING_REQUIRED": "该令牌尚未完成门户绑定——请使用门户「我的AI身份」页面"
                               "生成/绑定的令牌调用自由匹配接口。",
}


def match_with_retry(api, max_seconds=7200):
    """自由匹配带重试：只有明确的 MATCH_BUSY、429 和网络瞬态才退避重试。

    以下三类错误必须立即退出，绝不循环重试（均为永久性配置/令牌误用
    问题，重试无法自愈，只会浪费时间掩盖真实原因）：
    - ``FEATURE_DISABLED``：管理页已关闭自由匹配/测试房功能（永久条件）。
    - ``TOKEN_NOT_SCOPED``：误用了报名/测试令牌调用 /api/match。
    - ``PORTAL_BINDING_REQUIRED``：应使用门户「我的AI身份」令牌。

    401 同样立即抛出（历史行为保留，令牌本身无效同样不应重试）。
    """
    deadline = time.time() + max_seconds
    warned = False
    while time.time() < deadline:
        try:
            return api.match({"M": 10, "Rounds": 8})
        except ApiError as error:
            if error.status == 401:
                raise
            body = error.body or ""
            for marker, hint in _MATCH_PERMANENT_ERROR_MARKERS.items():
                if marker in body:
                    raise SystemExit(f"自由匹配遇到永久性错误 {marker}：{hint}") from error
            if not warned:
                print("匹配暂不可用，等待重试:", sanitize_text(repr(error), limit=140))
                warned = True
            # 只有明确的 MATCH_BUSY（409/403 中标明排队/占用）、429 限流、
            # 以及未识别的瞬态错误才退避重试；403 单独放宽等待时间
            # （历史行为：门户偶发短暂限流，非 FEATURE_DISABLED/权限问题时
            # 通常几秒后恢复）。
            time.sleep(10 if error.status == 403 else 5)
        except (OSError, TimeoutError) as error:
            print("匹配网络异常，重试:", sanitize_text(repr(error), limit=140))
            time.sleep(5)
    raise SystemExit("匹配等待超时，退出")


def _resolve_token(cli_token, environ, kind="scoped"):
    """令牌解析优先级：显式命令行 token > MJ_TOKEN 环境变量 > 本地令牌文件
    （.mj_token / ~/.mahjong_token）。见 mj/token.py——2026-09-24 起新增文件兜底，
    免得每开一个终端都要重新 export。"""
    from .token import resolve_token as _resolve
    return _resolve(cli_token, environ, kind=kind)


def main():
    parser = argparse.ArgumentParser(
        epilog="推荐用环境变量传令牌，避免出现在 shell 历史/进程列表里：\n"
               "  MJ_TOKEN='...' python3 -m mj.bot --wait",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("token", nargs="?", default=None,
                        help="登录令牌（可选）。优先级：显式命令行 token > MJ_TOKEN 环境变量。"
                             "两者都未提供时报错退出。推荐使用环境变量：MJ_TOKEN='...' python3 -m mj.bot --wait"
                             "（避免令牌出现在 shell 历史/进程列表里）。")
    parser.add_argument("--server", default="https://10.240.169.190:18080")
    parser.add_argument("--once", action="store_true", help="只打一个房间，打完退出")
    parser.add_argument("--rooms", type=int, default=0, help="自由匹配打满这么多房后自动退出（0 = 不限）")
    parser.add_argument("--room", help="直接打指定房间（不再调 match）")
    parser.add_argument("--ab", default=None,
                        help="实战按房随机 A/B：A.json,B.json（权重覆盖文件；\"current\" = 当前配置）。每开一个新房，由（种子,房号）的哈希"
                             "决定这一房整房用 A 还是 B，日志里记 ab_assign（config 标签、种子、overlay）和 ab_game（房/局/标签）；"
                             "配套 tools/live_ab_report.py。只用于自由匹配/--room，不用于 --wait 赛事。")
    parser.add_argument("--ab-seed", type=int, default=None, help="--ab 的随机种子（缺省随机生成，会记进日志）")
    parser.add_argument("--wait", action="store_true", help="赛场模式：多阶段正式赛全自动状态机")
    parser.add_argument("--wait-seconds", type=float, default=None,
                        help="--wait 的最长等待秒数（默认不设上限，以服务端终态"
                             "finished/closed/void 作为唯一正常退出条件；用户复核"
                             "指出决赛圈可能顺延，旧版默认 3 小时硬截止会导致赛事"
                             "尚未结束就静默退出。仅在脚本化短时测试/调试时才应"
                             "显式传入该参数；正式赛不应设置，若需要安全阀，应用"
                             "外部进程级 watchdog 监控+报警/重启，而不是让状态机"
                             "自己\"正常\"退出）")
    parser.add_argument("--max-consecutive-errors", type=int, default=20,
                        help="快照拉取（api.me/api.tournament）连续失败次数阈值")
    parser.add_argument("--max-consecutive-command-errors", type=int, default=20,
                        help="register/ready 命令连续失败次数阈值（独立计数器，"
                             "不会被快照拉取成功清零；用户复核发现的缺陷修复："
                             "旧版两类错误共用一个计数器，只要快照拉取正常，"
                             "register/ready 持续失败也永远不会触发熔断）")
    parser.add_argument("--async-log-queue-size", type=int, default=10000,
                        help="B2 观测生产闭环：异步日志有界队列容量（默认10000）")
    parser.add_argument("--sync-log", action="store_true",
                        help="调试用：使用同步 DecisionLog 而非异步包装（默认异步）")
    args = parser.parse_args()
    # 令牌优先级：显式命令行 token > MJ_TOKEN 环境变量；两者都没有时非零退出
    # （argparse.error() 内部调用 sys.exit(2)）。错误信息只提示环境变量名，
    # 不回显任何候选值——即使显式 token 为空字符串也不会被当作"已提供"。
    # 赛事路径要参赛令牌，自由匹配（/api/match）要全局令牌——作用域互斥，混用被 400 拒。
    _kind = "scoped" if (args.wait or args.room) else "global"
    token = _resolve_token(args.token, os.environ, kind=_kind)
    if not token:
        parser.error(
            "missing token: pass it as a positional argument, or set the MJ_TOKEN "
            "environment variable, e.g. MJ_TOKEN='...' python3 -m mj.bot --wait"
        )
    args.token = token
    ab = None
    if args.ab:
        if args.wait:
            parser.error("--ab 只能用于自由匹配/--room，不能用于 --wait 赛事")
        try:
            ab = _load_ab(args.ab, args.ab_seed)
        except (OSError, ValueError) as error:
            parser.error("--ab 参数不对：%s" % error)
    api = MahjongApi(args.server, args.token)
    # B2 观测生产闭环：默认使用有界队列+单写线程的异步日志包装，动作线程
    # 只做非阻塞 enqueue，不因为落盘慢而阻塞对局逻辑。--sync-log 保留同步

    # 路径用于调试对比（如需精确复现"每条日志立即落盘"的旧行为）。
    log = DecisionLog() if args.sync_log else AsyncDecisionLog(
        DecisionLog(), maxsize=args.async_log_queue_size,
    )
    _install_signal_handlers(log)
    # 启动记录：事后查「这次配置从什么时候开始生效」用（tools 的 --since）。不记命令行参数，避免带出令牌。
    log.append("bot_start", {"pid": os.getpid(), "rooms": getattr(args, "rooms", 0),
                             **({"ab": {"names": ab["names"], "seed": ab["seed"]}} if ab else {})})
    rules = {}
    try:
        rules = api.rules()
    except ApiError:
        pass
    try:
        version = api.version()
    except (OSError, ValueError):
        version = {}
    guide = version.get("version", 0)
    breaking = []
    for item in version.get("changes") or []:
        if isinstance(item, dict):
            # 2026-09-24：只收**比本 bot 已知版本更新**的 breaking 条目。
            # 原先收全部历史条目，于是 guide==KNOWN（都是 35）时照样刷 5 条陈年
            # 变更，每次启动都像出了大事，真正的新变更反而会被淹掉。
            if int(item.get("version") or 0) <= KNOWN_GUIDE_VERSION:
                continue
            if str(item.get("type", "")).lower() == "breaking":
                breaking.append(item.get("summary") or item.get("title") or str(item))
        elif isinstance(item, str) and "breaking" in item.lower():
            breaking.append(item)
    if guide > KNOWN_GUIDE_VERSION:
        print("⚠ 服务器指南 v%d，本 bot 已知 v%d —— 有 %d 条 BREAKING 需人工核对:"
              % (guide, KNOWN_GUIDE_VERSION, len(breaking)))
        for note in breaking[:5]:
            print("   -", note)
    else:
        print("接入指南 v%d，与本 bot 一致。" % guide)

    if args.wait:
        # 正式赛多阶段全自动状态机：register/ready/stage_open/stage_done/
        # running/finished 由 mj.tournament.run_tournament 统一驱动，
        # 不再从 game_id 字符串猜测赛事 ID，也不再把 active_games 为空
        # 误判为赛事结束。
        from .tournament import run_tournament as _run_tournament

        def _on_play(game_ids):
            if not game_ids:
                return
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(game_ids)) as executor:
                futures = [executor.submit(play_game, api, gid, log, rules) for gid in game_ids]
            for future in concurrent.futures.as_completed(futures):
                try:
                    future.result()
                except Exception as error:
                    print("对局线程异常:", sanitize_text(repr(error)))
                    traceback.print_exc()

        # 2026-10-05 外部评审 P0：run_tournament 连续 20 次网络/命令失败（约 7 分钟断网）会 raise，
        # 进程退出后赛方代跑无人重启，后续对局全部由服务端代打。这里在进程内重启状态机：
        # register/ready 幂等（409 ALREADY_READY 视为成功），GameLedger 随新状态机清空，进行中的对局会被重新接回。
        # 只有状态机正常返回（赛事终态 / --wait-seconds 到时）或 Ctrl+C 才退出。
        restarts = 0
        while True:
            try:
                decision = _run_tournament(
                    api, max_seconds=args.wait_seconds, on_play=_on_play,
                    max_consecutive_errors=args.max_consecutive_errors,
                    max_consecutive_command_errors=args.max_consecutive_command_errors,
                )
                break
            except Exception as error:   # noqa: BLE001
                restarts += 1
                print("赛事状态机异常（第 %d 次），30 秒后重启：%s" % (restarts, sanitize_text(repr(error))))
                traceback.print_exc()
                time.sleep(30)
        print("赛事状态机结束:", decision.action, decision.reason)
        if hasattr(log, "close"):
            # 正常退出路径：尽量排空异步日志队列（超时容许尾部损失，
            # 但 dropped/error 计数已经在 AsyncDecisionLog 内部持续累积，
            # 不会因为这里超时而丢失可观测性）。
            log.close(timeout=10.0)
        return

    # 安全停止：另开终端 `touch .mj_stop`，打完当前这一房后不再匹配新房、直接退出。
    # 直接 Ctrl+C 可能正好在「上一房结束→已匹配新房」之间，新房里我们的座位全程由服务端超时代打
    # （2026-09-28 a_81df00ba3d44：709 次出牌全部超时，−4.6/局）。
    stop_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".mj_stop")
    if os.path.exists(stop_file):
        os.remove(stop_file)
        print("已清除上次留下的停止标记 .mj_stop")
    rooms_done = 0
    while True:
        if args.room:
            room_id = args.room
            print("指定房间:", room_id)
        else:
            if os.path.exists(stop_file):
                os.remove(stop_file)
                print("检测到 .mj_stop：当前房已打完，不再匹配新房，退出。")
                if hasattr(log, "close"):
                    log.close(timeout=10.0)
                return
            match = match_with_retry(api)
            room_id = match["room_id"]
            print("匹配房间:", room_id)
            config = match.get("config")
            if isinstance(config, dict) and config:
                rules = config
        ab_label, ab_overlay = _ab_assign(ab, room_id, log) if ab else (None, None)
        info = None
        while True:
            try:
                info = api.tournament(room_id)
            except ApiError as error:
                if error.status == 404 and not is_transient(error):
                    print("房间已不存在:", room_id, error.code or "(无 code)")
                    break
                if error.status == 404:
                    # TOURNAMENT_GONE：房暂时不可达，重试而不是丢掉整个房间
                    print("房间暂时不可达，重试:", room_id, error.code)
                    time.sleep(5)
                    continue
                print("房间状态查询失败:", sanitize_text(repr(error)))
                time.sleep(5)
                continue
            status = info.get("status")
            if status in ("finished", "closed", "void"):
                print("本房间结束:", status)
                break
            try:
                active = api.me().get("active_games") or []
            except (ApiError, OSError, TimeoutError) as error:
                print("身份查询失败:", sanitize_text(repr(error)))
                time.sleep(3)
                continue
            if active:
                if ab:
                    for game in active:
                        log.append("ab_game", {"room_id": room_id, "game_id": game["game_id"], "label": ab_label})
                with concurrent.futures.ThreadPoolExecutor(max_workers=len(active)) as executor:
                    futures = [executor.submit(play_game, api, game["game_id"], log, rules, ab_overlay) for game in active]
                for future in concurrent.futures.as_completed(futures):
                    try:
                        future.result()
                    except Exception as error:
                        print("对局线程异常:", sanitize_text(repr(error)))
                        traceback.print_exc()
                continue
            time.sleep(2)
        ranking = (info or {}).get("ranking") or []
        print("排名:", json.dumps(ranking, ensure_ascii=False))
        rooms_done += 1
        if args.rooms and not args.room and rooms_done >= args.rooms:
            print("已打满 %d 房（--rooms），不再匹配新房，退出。" % rooms_done)
            if hasattr(log, "close"):
                log.close(timeout=10.0)
            return
        if args.once or args.room:
            # --room 模式打完即退: 无 --once 的 --room 曾整夜循环自动连场（僵尸 bot 事故）
            if hasattr(log, "close"):
                log.close(timeout=10.0)
            return
        time.sleep(5)


if __name__ == "__main__":
    main()
