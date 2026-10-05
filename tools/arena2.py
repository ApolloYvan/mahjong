"""G2：真正的自对弈对战平台——四个座位各自调用生产 ``mj.bot.choose_action``
（不是简化策略），引擎只负责状态机和合法性检查（``mj.sim.engine``），快照
用 ``mj.sim.snapshot.build_snapshot`` 生成（跟 3c 用同一份、已经用 99 万条
真实决策核对过的转换逻辑）。

    python3 tools/arena2.py smoke                                   # 冒烟：2 局，<1 分钟
    python3 tools/arena2.py ab --a A.json --b B.json --matches 20 --jobs 8 --layout 1v3
    python3 tools/arena2.py seatbias --matches 20 --jobs 8          # 对战平台自检，长任务
    python3 tools/arena2.py bench --seconds 30                      # 吞吐量，长任务

======================================================================
设计要点
======================================================================
- **按座位覆盖权重**：``mj.fit.weights_overlay`` 是模块级全局状态（不是
  线程安全的每座位隔离），本工具靠"单线程、每次只处理一个座位的一次决策"
  来保证安全——每次调用 ``mj.bot.choose_action`` 前后精确地 enter/exit
  一次 ``weights_overlay(seat_weights[seat])``，不会有两个座位的覆盖同时
  生效。多进程跑多场对战时（``--jobs``）每个进程有自己的模块全局状态，
  互不影响。
- **对战开始时冻结文件权重**：``mj.fit.frozen_file_weights()`` 包住整场
  ``Arena2Match.play()``，避免长对战期间用户中途改 ``models/weights.json``
  导致前后局配置不一致，也省掉重复磁盘 I/O。
- **同牌**：``mj.sim.engine.Match`` 每局牌墙固定用
  ``build_wall(seed * 1000 + round_no)``，跟座位、跟谁坐哪个 config 无关——
  所以只要种子一样，不管这个种子下跑哪种排法（A/B 怎么分配座位），第 N 局
  的牌墙都完全相同，天然满足"同牌"要求，不用额外处理。
- **A/B 配对检验（``ab``）**：给定权重覆盖文件 A、B（留空或 ``{}`` 表示
  "当前配置"，即不覆盖，直接走 ``models/weights.json``/默认权重），按
  ``--layout`` 把 A、B 分配到 4 个座位跑若干种子；用**显式标签**
  "A"/"B" 归属座位/分数，不再用 ``id(overlay)``（两份内容相同的配置会
  让 ``id()`` 失去区分意义——见旧版 ``_config_scores`` 的教训）。
    - ``2v2``：C(4,2)=6 种"哪两个座位是 A"的排法，每个种子全跑一遍。
    - ``1v3``：A 分别坐 4 个座位各跑一遍（4 种排法），对应比赛里
      "我们 1 家 vs 其余 3 家"的真实局面。
  统计以**种子**为单位聚类（同一个种子内所有排法的结果先聚合成一个
  "这个种子的 A−B 配对差"，再跨种子算均值和 95% 置信区间）——种子内的
  多个排法不是独立观测（共享同一副牌墙集合），拿种子做聚类单位才不会
  低估方差。
- **``seatbias``**（原 ``aa``，改名）：A=B=当前配置的特化调用，检验对战
  平台本身有没有系统性座位偏差，不是策略强弱判定。
- **非法动作计数，按动作类型分列**：``choose_action`` 返回的动作被引擎判
  非法时，按"打算做什么"（discard/hu/gang/peng/chi/unknown）分别计数，
  用一个"必然合法"的兜底动作替上（摸牌阶段兜底弃摸到的那张，响应阶段
  兜底 pass），不让对局卡死。非法动作不应该发生——production 代码本身
  应该只产生合法动作，一旦出现说明快照喂错了什么，或生产代码本身有 bug。
- **缓存续跑**：``ab``/``seatbias`` 按种子写入 ``tools/.cache/arena2/``
  （每个种子一个 json 文件），重跑时跳过已完成的种子，只补跑新增的。
- 长任务只打印结果文件路径 + 一个 ≤40 行汇总，明细都在文件里；支持
  ``--jobs`` 多进程跑，``smoke``/单场调试保持单进程可调试。
"""
import argparse
import contextlib
import glob
import hashlib
import itertools
import json
import math
import os
import statistics
import sys
import time
from collections import Counter
from multiprocessing import Pool

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ.setdefault("MJ_WEIGHTS_NO_FILE", "1")   # 冒烟/CI 默认不碰真实 weights.json；真对战由调用方决定

from mj.bot import choose_action, hu_result  # noqa: E402
from mj.fit import frozen_file_weights, weights_overlay  # noqa: E402
from mj.mc.chain_tracker import ChainTracker  # noqa: E402
from mj.rules import payout  # noqa: E402
from mj.sim.engine import IllegalActionError, Match  # noqa: E402
from mj.sim.snapshot import build_snapshot  # noqa: E402

MAX_STEPS_PER_ROUND = 3000   # 防御性上限：真实语料单局事件数从没见过接近这个量级
CACHE_DIR = os.path.join(ROOT, "tools", ".cache", "arena2")


def _resolve_self_gang_kind(engine, seat, tile):
    for m in engine.melds[seat]:
        if m.get("kind") == "peng" and m["tiles"][0] == tile:
            return "bu"
    return "an"


def _apply_draw_action(engine, seat, action, stats, snapshot=None, illegal_log=None):
    kind = (action or {}).get("action", "unknown")
    try:
        if not action:
            raise IllegalActionError("choose_action 在 draw 阶段返回 None")
        if kind == "hu":
            engine.apply_hu(seat)
        elif kind == "gang":
            engine.apply_gang(seat, action["tile"], _resolve_self_gang_kind(engine, seat, action["tile"]))
        elif kind == "discard":
            engine.apply_discard(seat, action["tile"])
        else:
            raise IllegalActionError("draw 阶段未知动作 %r" % action)
        return
    except (IllegalActionError, KeyError) as exc:
        stats["illegal_draw_%s" % kind] += 1
        _log_illegal(illegal_log, "draw", seat, snapshot, action, exc)
    # 兜底绝不能再抛异常：drawn_tile 这一刻一定在手里就打它（刚摸到的那张
    # 永远合法，不管上面为什么失败）；如果连这个都不在手里（不应该发生，
    # 但别再让兜底本身变成第二次崩溃），退到手牌最后一张；手牌空了就什么
    # 都不做（整局本身已经处于不该出现的状态，留给上层 MAX_STEPS_PER_ROUND
    # 兜底掐断，不在这里伪造一个不存在的动作）。
    hand = engine.hands[seat]
    fallback_tile = engine.drawn_tile if engine.drawn_tile in hand else (hand[-1] if hand else None)
    if fallback_tile is not None:
        try:
            engine.apply_discard(seat, fallback_tile)
        except IllegalActionError as exc:
            stats["illegal_fallback_draw"] += 1
            _log_illegal(illegal_log, "draw_fallback", seat, snapshot, {"action": "discard", "tile": fallback_tile}, exc)


def _log_illegal(illegal_log, phase, seat, snapshot, action, exc):
    if illegal_log is None:
        return
    illegal_log.append({
        "phase": phase, "seat": seat, "action": action, "reason": str(exc),
        "snapshot": snapshot,
    })


def _apply_response_action(engine, seat, action, stats, claims_per_seat=None, snapshot=None, illegal_log=None):
    kind = (action or {}).get("action", "pass")
    try:
        if not action or kind == "pass":
            engine.apply_pass(seat)
            return
        if kind == "peng":
            engine.apply_claim(seat, "peng", None)
        elif kind == "gang":
            engine.apply_claim(seat, "gang", None)
        elif kind == "chi":
            engine.apply_claim(seat, "chi", action["tiles"])
        else:
            raise IllegalActionError("response 阶段未知动作 %r" % action)
        if claims_per_seat is not None:
            claims_per_seat[(seat, kind)] += 1
        return
    except (IllegalActionError, KeyError) as exc:
        stats["illegal_response_%s" % kind] += 1
        _log_illegal(illegal_log, "response", seat, snapshot, action, exc)
    try:
        engine.apply_pass(seat)   # pass 在响应窗口里对"取出这个 seat 时它还在 responding_seats 里"必然合法
    except IllegalActionError as exc:
        stats["illegal_fallback_response"] += 1
        _log_illegal(illegal_log, "response_fallback", seat, snapshot, {"action": "pass"}, exc)


def play_round(engine, choose_fn, stats, round_meta=None, illegal_log=None):
    """驱动一整局：每个决策点用 ``choose_fn(seat, snapshot)`` 换取生产
    代码的真实动作并应用。``choose_fn`` 由调用方按座位包一层
    ``weights_overlay``。``round_meta``（可选 dict）：填入
    ``draws_per_seat``（Counter，每座位本局摸牌次数，供 G3a 校准算
    "这是本座位第几次摸牌"用）和 ``claims_per_seat``（Counter，键
    ``(seat, "chi"/"peng"/"gang")``，本局每座位吃/碰/杠各几次）。
    ``illegal_log``（可选 list）：每次非法动作追加一条
    ``{"phase","seat","action","reason","snapshot"}``，供
    ``cmd_illegal_audit`` 之类的调用方写 ``reports/arena2_illegal.tsv``。"""
    draws_per_seat = Counter()
    claims_per_seat = Counter()
    if round_meta is not None:
        round_meta["draws_per_seat"] = draws_per_seat
        round_meta["claims_per_seat"] = claims_per_seat
    steps = 0
    while not engine.finished:
        steps += 1
        if steps > MAX_STEPS_PER_ROUND:
            stats["aborted_max_steps"] += 1
            break
        if engine.phase == "draw":
            seat = engine.turn
            if engine.needs_draw():   # 碰/吃之后 drawn_tile 也是 None 但不该摸牌，见 RoundEngine.needs_draw
                engine.step_draw()
                draws_per_seat[seat] += 1
                if engine.finished:
                    break
            snapshot = build_snapshot(engine, seat)
            action = choose_fn(seat, snapshot)
            _apply_draw_action(engine, seat, action, stats, snapshot=snapshot, illegal_log=illegal_log)
        elif engine.phase == "response":
            for seat in list(engine.responding_seats):
                if engine.phase != "response" or seat not in engine.responding_seats:
                    continue
                snapshot = build_snapshot(engine, seat)
                action = choose_fn(seat, snapshot)
                _apply_response_action(engine, seat, action, stats, claims_per_seat,
                                       snapshot=snapshot, illegal_log=illegal_log)
        else:
            break
    return engine


def _win_category(engine):
    """胡牌那一刻（``engine.apply_hu`` 刚返回，胜负已判但状态还没进入
    下一局）从 ``engine.detail``（``mj.rules.evaluate`` 的真实文本标签，见
    该函数 docstring 的口径核对记录）和链路旗标里提取分类，供 ``ab``/
    ``seatbias`` 报告"爆头/七对/财飘/杠开/4个白板 占胡比例"用。

    "财飘"没有独立的 detail 文本标签（财神相关的加成要么走"4个白板"、
    要么并进"动作链x%d"），这里按"这次胡牌吃到的动作链倍数来自纯飘
    （chain_has_piao 为真、chain_has_gang 为假）"来判定——是本工具对"财飘
    占胡"这个统计口径的定义，不是服务端自己给的标签，如实标注。"""
    detail = engine.detail or []
    return {
        "baotou": "爆头" in detail,
        "qidui": any(d.startswith("七对") or d.startswith("豪华七对") for d in detail),
        "piao_chain": bool(engine.chain_has_piao and not engine.chain_has_gang),
        "gang_kai": "杠开" in detail,
        "four_white": "4个白板" in detail,
    }


MC_KEYS = ("mc_hu_enabled", "mc_discard_enabled", "mc_menqing_enabled", "nn_policy_enabled", "nn_resid_enabled")   # 需要 ctx（飘数/链含杠）的开关


def mc_seed(match_seed, decision_idx):
    """MC 随机种子 = f(对局种子, 决策序号)，跟进程/哈希随机化无关，可复现。"""
    return int(hashlib.md5(("%s:%s" % (match_seed, decision_idx)).encode()).hexdigest()[:8], 16)


class Arena2Match:
    """一场 8 局对战，四个座位各自的策略由 ``seat_weights``（座位号 ->
    权重覆盖 dict，缺省座位用默认权重）决定。``self.history`` 每局一条
    记录，``scores`` 是单局增量（不是累计）。"""

    def __init__(self, seed, seat_weights=None, rules=None, rounds=8, track_timing=False, collect_illegal=False):
        self.seed = seed
        self.seat_weights = seat_weights or {}
        self.rules = rules or {}
        self.match = Match(seed, rounds=rounds)
        self.stats = Counter()
        self.history = []
        self.track_timing = track_timing
        self.decision_times = []   # 秒，仅 track_timing=True 时填充
        self.illegal_log = [] if collect_illegal else None
        self._dec = 0                      # 决策序号（全桌所有座位的每次 choose 调用），MC 随机种子用
        self._trackers = {}                # 座位 -> ChainTracker（MC 输入：飘出的财神数/链是否含杠）
        self.mc_ms = []                    # 每次 MC 实际参与的决策的耗时（毫秒）
        self.nn_ms = []                    # 每次网络策略实际给出动作的推理耗时（毫秒）
        self._s1 = {}                      # (局号, 座位) -> {"fan","dealer"}：S1 弃胡（非爆头可胡但打了牌）事件，每局每座位只记一次

    def _choose_fn(self, seat, snapshot):
        overlay = self.seat_weights.get(seat)
        self._dec += 1
        started = time.perf_counter() if self.track_timing else None
        mc_ctx = None
        if overlay and any(overlay.get(k) for k in MC_KEYS):
            # arena 模式：固定推演次数、不按时间停；随机种子由（对局种子, 决策序号）推出，可复现。
            tracker = self._trackers.setdefault(seat, ChainTracker())
            mc_ctx = {"fixed_n": int(overlay.get("mc_fixed_n", 128)), "seed": mc_seed(self.seed, self._dec),
                      **tracker.ctx(snapshot)}
        if overlay:
            with weights_overlay(overlay):
                action = (choose_action(snapshot, self.rules, mc_ctx=mc_ctx) if mc_ctx is not None
                          else choose_action(snapshot, self.rules))
        else:
            action = choose_action(snapshot, self.rules)
        if snapshot.get("phase") == "draw" and snapshot.get("drawn_tile") and action and action.get("action") == "discard":
            key = (snapshot.get("round_no"), seat)
            if key not in self._s1:
                with (weights_overlay(overlay) if overlay else contextlib.nullcontext()):
                    res = hu_result(snapshot, self.rules)
                if res and not res.get("baotou"):
                    self._s1[key] = {"fan": res.get("fan", 1), "dealer": snapshot.get("dealer") == seat}
        if mc_ctx is not None:
            self._trackers[seat].observe(snapshot, action)
            rlog = mc_ctx.get("resid_log")
            if rlog is not None and rlog.get("cands"):
                self.stats["resid_calls"] += 1
                self.stats["resid_overridden"] += 1 if rlog.get("pick") else 0
                self.nn_ms.append(rlog.get("ms", 0.0))
            elif rlog is not None and rlog.get("fallback"):
                self.stats["resid_fallback"] += 1
            nlog = mc_ctx.get("nn_log")
            if nlog is not None:
                self.stats["nn_calls"] += 1
                if nlog.get("fallback"):
                    self.stats["nn_fb_%s" % nlog["fallback"].split(":")[0]] += 1
                else:
                    self.stats["nn_used"] += 1
                    self.nn_ms.append(nlog.get("ms", 0.0))
            log = mc_ctx.get("mc_log")
            if log:
                self.stats["mc_calls"] += 1
                self.mc_ms.append(log.get("elapsed_ms", 0.0))
                if log.get("overridden"):
                    self.stats["mc_overridden"] += 1
                else:
                    self.stats["mc_fb_%s" % (log.get("fallback_reason") or "none").split(":")[0]] += 1
        if started is not None:
            self.decision_times.append(time.perf_counter() - started)
        return action

    def play(self):
        prev_scores = [0, 0, 0, 0]
        with frozen_file_weights():
            for _ in range(self.match.rounds):
                round_meta = {}

                def actor(e):
                    play_round(e, self._choose_fn, self.stats, round_meta=round_meta,
                              illegal_log=self.illegal_log)

                engine = self.match.play_round_with_actor(actor)
                cats = _win_category(engine) if (not engine.is_draw and engine.winner is not None) else {}
                delta = [a - b for a, b in zip(engine.scores, prev_scores)]
                prev_scores = list(engine.scores)
                winner_draw_index = None
                winner_meld_count = None
                if not engine.is_draw and engine.winner is not None:
                    winner_draw_index = round_meta["draws_per_seat"].get(engine.winner, 0)
                    winner_meld_count = engine.meld_groups(engine.winner)
                self.history.append({
                    "round_no": engine.round_no, "dealer": engine.dealer, "winner": engine.winner,
                    "is_draw": engine.is_draw, "fan": engine.fan, "detail": list(engine.detail or []),
                    "delta_scores": delta, "cats": cats,
                    "winner_draw_index": winner_draw_index, "winner_meld_count": winner_meld_count,
                    "claims_per_seat": {"%d_%s" % k: v for k, v in round_meta["claims_per_seat"].items()},
                    "s1": {s_: self._s1.pop((engine.round_no, s_)) for s_ in range(4) if (engine.round_no, s_) in self._s1},
                })
        return self


# ---------------------------------------------------------------- smoke

def cmd_smoke(args):
    """冒烟：跑 2 场小对战（每场 2 局），确认整条链路（choose_action 真实
    调用、非法动作兜底、权重覆盖/冻结）能跑通，不代表策略强弱结论。"""
    started = time.time()
    total_stats = Counter()
    all_illegal = []
    for seed in range(2):
        m = Arena2Match(seed, rounds=2, collect_illegal=True).play()
        total_stats.update(m.stats)
        all_illegal.extend(m.illegal_log)
        print("seed=%d 最终分数=%s 非法动作=%s" % (seed, m.match.scores, dict(m.stats)))
    print("\n耗时 %.1fs，累计非法动作：%s" % (time.time() - started, dict(total_stats)))
    if total_stats["aborted_max_steps"]:
        print("警告：出现过单局步数超限（>%d），说明对局没有正常终止，需要排查" % MAX_STEPS_PER_ROUND)
    illegal_cats = _write_illegal_report(all_illegal)
    print("非法动作根因（完整见 reports/arena2_illegal.tsv）：%s" % (dict(illegal_cats) or "无"))


# ---------------------------------------------------------------- 权重文件

def _load_overlay_file(path):
    """空路径/空文件/``{}`` 都表示"当前配置"（不覆盖），返回 None；否则
    返回权重覆盖 dict。"""
    if not path:
        return None
    with open(path, encoding="utf-8") as f:
        text = f.read().strip()
    if not text:
        return None
    obj = json.loads(text)
    return obj or None


# ---------------------------------------------------------------- 排法

def _layout_2v2():
    """返回 [(a_seats, b_seats), ...]，C(4,2)=6 种"哪两个座位是 A"的排法。"""
    seats = (0, 1, 2, 3)
    out = []
    for a_seats in itertools.combinations(seats, 2):
        b_seats = tuple(s for s in seats if s not in a_seats)
        out.append((a_seats, b_seats))
    return out


def _layout_1v3():
    """返回 [(a_seats, b_seats), ...]，A 分别坐 4 个座位各一次。"""
    seats = (0, 1, 2, 3)
    return [((a,), tuple(s for s in seats if s != a)) for a in seats]


LAYOUTS = {"2v2": _layout_2v2, "1v3": _layout_1v3}


def _seat_weights_and_labels(a_seats, b_seats, a_overlay, b_overlay):
    seat_weights = {}
    labels = {}
    for s in a_seats:
        seat_weights[s] = a_overlay
        labels[s] = "A"
    for s in b_seats:
        seat_weights[s] = b_overlay
        labels[s] = "B"
    return seat_weights, labels


# ---------------------------------------------------------------- ab / seatbias 核心

def _run_ab_seed(seed, a_overlay, b_overlay, layout_name, rounds, rules):
    """一个种子下，某个 layout 的全部排法各跑一场，返回按标签聚合的
    (round_records, match_summaries, stats, illegal_log)。``round_records``：
    [{"label":"A"/"B", "seat":int, "arrangement":int, "round_no":int,
      "delta_score":float, "is_winner":bool, "is_draw":bool, "fan":int,
      "cats":{...}}, ...]。``match_summaries``：每场对战（一个排法）结束
      时每个座位的累计得分 + 标签，用来算"第一名率"。``illegal_log``：
      这个种子里发生的全部非法动作记录（见 ``_log_illegal``），供
      ``reports/arena2_illegal.tsv`` 用——目标是 0 条，不是 0 条就说明
      快照/引擎有偏差，见 ``cmd_ab``/``cmd_seatbias`` 的根因汇总。"""
    layout_fn = LAYOUTS[layout_name]
    arrangements = layout_fn()
    round_records = []
    match_summaries = []
    stats = Counter()
    illegal_log = []
    extras = {"mc_ms": [], "nn_ms": []}
    for arr_idx, (a_seats, b_seats) in enumerate(arrangements):
        seat_weights, labels = _seat_weights_and_labels(a_seats, b_seats, a_overlay, b_overlay)
        m = Arena2Match(seed, seat_weights=seat_weights, rules=rules, rounds=rounds,
                        collect_illegal=True).play()
        stats.update(m.stats)
        illegal_log.extend(m.illegal_log)
        extras["mc_ms"].extend(m.mc_ms)
        extras["nn_ms"].extend(m.nn_ms)
        final_scores = list(m.match.scores)
        rank = sorted(range(4), key=lambda s: -final_scores[s])
        best = max(final_scores)
        for seat in range(4):
            match_summaries.append({
                "label": labels[seat], "seat": seat, "seed": seed, "arrangement": arr_idx,
                "final_score": final_scores[seat], "is_first": final_scores[seat] == best,
            })
        for rec in m.history:
            for seat in range(4):
                cl = rec.get("claims_per_seat") or {}
                round_records.append({
                    "label": labels[seat], "seat": seat, "seed": seed, "arrangement": arr_idx,
                    "is_dealer": rec["dealer"] == seat, "s1": (rec.get("s1") or {}).get(seat),
                    "chi": cl.get("%d_chi" % seat, 0), "peng": cl.get("%d_peng" % seat, 0), "gang": cl.get("%d_gang" % seat, 0),
                    "round_no": rec["round_no"], "delta_score": rec["delta_scores"][seat],
                    "is_winner": rec["winner"] == seat, "is_draw": rec["is_draw"],
                    "fan": rec["fan"] if rec["winner"] == seat else None,
                    "cats": rec["cats"] if rec["winner"] == seat else None,
                })
    return round_records, match_summaries, stats, illegal_log, extras


def _cache_path(tag, seed):
    return os.path.join(CACHE_DIR, "%s_seed%d.json" % (tag, seed))


def _load_cached_seed(tag, seed):
    path = _cache_path(tag, seed)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _save_cached_seed(tag, seed, round_records, match_summaries, stats, illegal_log, extras=None):
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = _cache_path(tag, seed)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"round_records": round_records, "match_summaries": match_summaries,
                   "stats": dict(stats), "illegal_log": illegal_log,
                   "extras": extras or {}}, f, ensure_ascii=False)
    os.replace(tmp, path)


_AB_WORKER_ARGS = {}   # 子进程通过 initializer 拿到不可 pickle 友好传参的固定配置


def _ab_worker_init(a_overlay, b_overlay, layout_name, rounds, rules):
    _AB_WORKER_ARGS.update(a_overlay=a_overlay, b_overlay=b_overlay,
                            layout_name=layout_name, rounds=rounds, rules=rules)


def _ab_worker(seed):
    a = _AB_WORKER_ARGS["a_overlay"]
    b = _AB_WORKER_ARGS["b_overlay"]
    layout_name = _AB_WORKER_ARGS["layout_name"]
    rounds = _AB_WORKER_ARGS["rounds"]
    rules = _AB_WORKER_ARGS["rules"]
    round_records, match_summaries, stats, illegal_log, extras = _run_ab_seed(seed, a, b, layout_name, rounds, rules)
    return seed, round_records, match_summaries, dict(stats), illegal_log, extras


def _run_ab(tag, seeds, a_overlay, b_overlay, layout_name, rounds, rules, jobs, no_cache, max_minutes=None):
    """跑（或从缓存读）每个种子，返回 (all_round_records, all_match_summaries, total_stats,
    all_illegal_log, 本次实际新跑了多少个种子, 耗时, 复用缓存的种子数, done_seeds, extras)。
    ``max_minutes``：到点就停（不再取新种子，已在跑的丢弃），用**已完成**的种子出结果；
    ``done_seeds`` 是实际参与统计的种子（缓存命中 + 新完成）。"""
    all_round_records = []
    all_match_summaries = []
    total_stats = Counter()
    all_illegal_log = []
    extras = {"mc_ms": [], "nn_ms": []}
    done_seeds = []
    todo = []
    reused = 0
    for seed in seeds:
        cached = None if no_cache else _load_cached_seed(tag, seed)
        if cached is not None:
            all_round_records.extend(cached["round_records"])
            all_match_summaries.extend(cached["match_summaries"])
            total_stats.update(cached["stats"])
            all_illegal_log.extend(cached.get("illegal_log") or [])
            extras["mc_ms"].extend((cached.get("extras") or {}).get("mc_ms") or [])
            extras["nn_ms"].extend((cached.get("extras") or {}).get("nn_ms") or [])
            done_seeds.append(seed)
            reused += 1
        else:
            todo.append(seed)

    started = time.time()
    deadline = started + max_minutes * 60 if max_minutes else None

    def take(seed, rr, ms, st, il, ex):
        all_round_records.extend(rr)
        all_match_summaries.extend(ms)
        total_stats.update(st)
        all_illegal_log.extend(il)
        extras["mc_ms"].extend(ex.get("mc_ms") or [])
        extras["nn_ms"].extend(ex.get("nn_ms") or [])
        done_seeds.append(seed)
        if not no_cache:
            _save_cached_seed(tag, seed, rr, ms, st, il, ex)

    new_done = 0
    if todo:
        if jobs > 1:
            pool = Pool(jobs, initializer=_ab_worker_init,
                        initargs=(a_overlay, b_overlay, layout_name, rounds, rules))
            try:
                it = pool.imap_unordered(_ab_worker, todo, chunksize=1)
                while True:
                    if deadline is not None and time.time() >= deadline:
                        break
                    try:
                        res = it.next(timeout=1.0) if deadline is not None else it.next()
                    except StopIteration:
                        break
                    except Exception as exc:   # multiprocessing.TimeoutError：继续等/检查截止时间
                        if exc.__class__.__name__ == "TimeoutError":
                            continue
                        raise
                    take(*res)
                    new_done += 1
            finally:
                pool.terminate()
                pool.join()
        else:
            _ab_worker_init(a_overlay, b_overlay, layout_name, rounds, rules)
            for seed in todo:
                if deadline is not None and time.time() >= deadline:
                    break
                take(*_ab_worker(seed))
                new_done += 1
    elapsed = time.time() - started
    return (all_round_records, all_match_summaries, total_stats, all_illegal_log, new_done, elapsed, reused,
            sorted(done_seeds), extras)


def _mean_ci95(values):
    """正态近似的 95%CI（z=1.96）——纯标准库没有 t 分布表，种子数较少
    （比如 <30）时这是一个近似，如实标注，不是精确的 t 检验。"""
    n = len(values)
    if n == 0:
        return 0.0, 0.0, 0.0
    mean = statistics.mean(values)
    if n < 2:
        return mean, 0.0, 0.0
    se = statistics.stdev(values) / math.sqrt(n)
    return mean, se, 1.96 * se


def _per_seed_diff(round_records):
    """按种子聚合：diff_seed = 这个种子下所有 A 标签(座位,局)的场均单局
    得分 − 所有 B 标签的场均单局得分。种子内的多个排法共享同一副牌墙
    集合，不是独立观测，所以先在种子内部聚合成一个数，再跨种子取均值/CI，
    不能直接把每条 round_record 当独立样本平均。"""
    by_seed = {}
    for r in round_records:
        by_seed.setdefault(r["seed"], {"A": [], "B": []})[r["label"]].append(r["delta_score"])
    diffs = []
    for seed in sorted(by_seed):
        a_vals = by_seed[seed]["A"]
        b_vals = by_seed[seed]["B"]
        if a_vals and b_vals:
            diffs.append(statistics.mean(a_vals) - statistics.mean(b_vals))
    return diffs


def _rate_diff(match_summaries, key):
    by_label = {"A": [], "B": []}
    for m in match_summaries:
        by_label[m["label"]].append(1.0 if m[key] else 0.0)
    a = statistics.mean(by_label["A"]) if by_label["A"] else 0.0
    b = statistics.mean(by_label["B"]) if by_label["B"] else 0.0
    return a, b, a - b


def _hu_category_table(round_records):
    """返回 {"A": {...}, "B": {...}}，每个标签下：胜率、平均番数（限胡牌局）、
    各分类占胡比例（分母=该标签胜局数）。"""
    out = {}
    for label in ("A", "B"):
        rows = [r for r in round_records if r["label"] == label]
        total_rounds = len(rows)
        wins = [r for r in rows if r["is_winner"]]
        n_win = len(wins)
        win_rate = n_win / total_rounds if total_rounds else 0.0
        avg_fan = statistics.mean([w["fan"] for w in wins]) if wins else 0.0
        cats = {}
        for key in ("baotou", "qidui", "piao_chain", "gang_kai", "four_white"):
            n = sum(1 for w in wins if w["cats"] and w["cats"].get(key))
            cats[key] = (n / n_win if n_win else 0.0, n)
        out[label] = {"total_rounds": total_rounds, "n_win": n_win, "win_rate": win_rate,
                      "avg_fan": avg_fan, "cats": cats}
    return out


def _role_table(round_records):
    """按 标签(A/B) x 角色(庄/闲)：每局得分、胜率、平均番数（限胡牌局）、每局吃/碰/杠次数、局数。"""
    out = {}
    for label in ("A", "B"):
        for role, flag in (("庄", True), ("闲", False)):
            rows = [r for r in round_records if r["label"] == label and r.get("is_dealer") == flag]
            n = len(rows)
            wins = [r for r in rows if r["is_winner"]]
            out[(label, role)] = {
                "n": n, "score": statistics.mean([r["delta_score"] for r in rows]) if n else 0.0,
                "win": len(wins) / n if n else 0.0,
                "fan": statistics.mean([w["fan"] for w in wins]) if wins else 0.0,
                "chi": sum(r.get("chi", 0) for r in rows) / n if n else 0.0,
                "peng": sum(r.get("peng", 0) for r in rows) / n if n else 0.0,
                "gang": sum(r.get("gang", 0) for r in rows) / n if n else 0.0}
    return out


def _s1_table(round_records):
    """S1 弃胡（非爆头可胡但没胡）：按标签的触发次数、最终自己胡的比例、相对"当场胡"的净得分差/次。"""
    out = {}
    for label in ("A", "B"):
        rows = [r for r in round_records if r["label"] == label and r.get("s1")]
        n = len(rows)
        won = sum(1 for r in rows if r["is_winner"])
        net = sum(r["delta_score"] - payout(r["s1"]["fan"], dealer=r["s1"]["dealer"]) for r in rows)
        out[label] = {"n": n, "won": won, "net": net}
    return out


def _print_s1_table(round_records):
    t = _s1_table(round_records)
    if not (t["A"]["n"] or t["B"]["n"]):
        return
    parts = []
    for label in ("A", "B"):
        d = t[label]
        parts.append("%s 触发 %d 次，最终胡 %d（%.0f%%），相对当场胡净 %+.1f/次" % (
            label, d["n"], d["won"], 100.0 * d["won"] / d["n"] if d["n"] else 0.0, d["net"] / d["n"] if d["n"] else 0.0))
    print("S1 弃胡（非爆头可胡但没胡）：" + "；".join(parts))


def _role_diff(round_records, flag):
    """庄/闲分别的 A-B 配对差（按种子聚类）。"""
    return _mean_ci95(_per_seed_diff([r for r in round_records if r.get("is_dealer") == flag]))


def _print_role_table(round_records):
    t = _role_table(round_records)
    print("按角色（每局场均）：")
    for role in ("庄", "闲"):
        a, b = t[("A", role)], t[("B", role)]
        print("  %s  得分 A %+.2f B %+.2f | 胜率 A %.3f B %.3f | 均番 A %.2f B %.2f | 吃/碰/杠 A %.2f/%.2f/%.2f B %.2f/%.2f/%.2f（局数 A %d B %d）" % (
            role, a["score"], b["score"], a["win"], b["win"], a["fan"], b["fan"], a["chi"], a["peng"], a["gang"],
            b["chi"], b["peng"], b["gang"], a["n"], b["n"]))
    for role, flag in (("庄", True), ("闲", False)):
        m, se, half = _role_diff(round_records, flag)
        print("  %s A−B：%+.3f 95%%CI [%+.3f, %+.3f]" % (role, m, m - half, m + half))


def _illegal_by_type(stats):
    return {k: v for k, v in stats.items() if k.startswith("illegal_")}


def _print_ab_summary(tag, a_path, b_path, layout_name, seeds, round_records, match_summaries,
                       total_stats, new_runs, elapsed, reused, out_path, jobs):
    diffs = _per_seed_diff(round_records)
    mean, se, half = _mean_ci95(diffs)
    lo, hi = mean - half, mean + half
    first_a, first_b, first_diff = _rate_diff(match_summaries, "is_first")
    hu_table = _hu_category_table(round_records)

    print("=== %s（A=%s B=%s layout=%s，完整明细见 %s） ===" % (
        tag, a_path or "<当前配置>", b_path or "<当前配置>", layout_name, out_path))
    print("种子数 %d（本次新跑 %d，复用缓存 %d），新跑耗时 %.1fs（%.2fs/种子，--jobs %d）" % (
        len(seeds), new_runs, reused, elapsed, elapsed / max(1, new_runs), jobs))
    print("配对差 A−B（每局场均分，按种子聚类）：均值 %+.3f，95%%CI [%+.3f, %+.3f]（半宽 %.3f）%s" % (
        mean, lo, hi, half, "—— 置信区间包含 0" if lo <= 0 <= hi else "—— 置信区间**不**包含 0"))
    print("第一名率：A %.3f  B %.3f  差 %+.3f" % (first_a, first_b, first_diff))
    for label in ("A", "B"):
        t = hu_table[label]
        print("  [%s] 胜率 %.3f（%d/%d 局），平均番数 %.2f" % (
            label, t["win_rate"], t["n_win"], t["total_rounds"], t["avg_fan"]))
        cat_str = "  ".join("%s %.1f%%(n=%d)" % (k, v[0] * 100, v[1]) for k, v in t["cats"].items())
        print("       占胡比例：%s" % cat_str)
    illegal = _illegal_by_type(total_stats)
    print("非法动作（按类型）：%s" % (illegal or "无"))
    if total_stats.get("aborted_max_steps"):
        print("警告：出现过单局步数超限（>%d），说明对局没有正常终止，需要排查" % MAX_STEPS_PER_ROUND)

    if half > 0 and len(diffs) >= 2:
        std = statistics.stdev(diffs)
        per_seed_sec = elapsed / max(1, new_runs)
        for target in (0.3, 0.5):
            n_needed = math.ceil((1.96 * std / target) ** 2) if target > 0 else 0
            n_needed = max(n_needed, 1)
            est_sec = n_needed * per_seed_sec / max(1, jobs)
            print("达到 ±%.1f 分/局分辨率约需 %d 个种子，--jobs %d 下预计耗时 %.0f 分钟" % (
                target, n_needed, jobs, est_sec / 60.0))


def _pct_ms(sorted_vals, p):
    if not sorted_vals:
        return 0.0
    return sorted_vals[min(len(sorted_vals) - 1, int(len(sorted_vals) * p))]


def _print_mc_summary(total_stats, extras):
    """MC 参与情况：推翻生产选择的比例、回落率（技术性回落：超时/局数不足/没校准/异常等）、
    单次 MC 决策耗时 p50/p99。只有 A 组（开了 mc 开关的座位）会产生这些计数。"""
    r_calls = total_stats.get("resid_calls", 0)
    if r_calls or total_stats.get("resid_fallback", 0):
        nms = sorted(extras.get("nn_ms") or [])
        print("生产+修正出牌模型：评估 %d 次，改动生产选择 %d（%.1f%%），回落 %d；耗时 p50 %.1fms p99 %.1fms" % (
            r_calls, total_stats.get("resid_overridden", 0), 100.0 * total_stats.get("resid_overridden", 0) / max(1, r_calls),
            total_stats.get("resid_fallback", 0), _pct_ms(nms, 0.5), _pct_ms(nms, 0.99)))
    n_calls = total_stats.get("nn_calls", 0)
    if n_calls:
        used = total_stats.get("nn_used", 0)
        fb = {k[6:]: v for k, v in total_stats.items() if k.startswith("nn_fb_")}
        nms = sorted(extras.get("nn_ms") or [])
        print("网络策略决策 %d 次：采用 %d（%.1f%%），回落生产 %d（%.1f%%）%s；推理耗时 p50 %.1fms p99 %.1fms max %.1fms" % (
            n_calls, used, 100.0 * used / n_calls, n_calls - used, 100.0 * (n_calls - used) / n_calls,
            ("，原因 %s" % fb) if fb else "", _pct_ms(nms, 0.5), _pct_ms(nms, 0.99), nms[-1] if nms else 0.0))
    calls = total_stats.get("mc_calls", 0)
    if not calls:
        return
    over = total_stats.get("mc_overridden", 0)
    keep = total_stats.get("mc_fb_not_significant", 0)
    tech = calls - over - keep
    ms = sorted(extras.get("mc_ms") or [])
    print("MC 决策 %d 次：推翻生产选择 %d（%.1f%%），MC 评估后保持生产(差异不显著) %d（%.1f%%），"
          "技术性回落 %d（%.1f%%）" % (calls, over, 100.0 * over / calls, keep, 100.0 * keep / calls,
                                      tech, 100.0 * tech / calls))
    fb = {k[len("mc_fb_"):]: v for k, v in total_stats.items() if k.startswith("mc_fb_") and k != "mc_fb_not_significant"}
    if fb:
        print("  回落原因：%s" % dict(fb))
    print("  单次 MC 决策耗时：p50 %.0fms  p99 %.0fms  max %.0fms（arena 固定 n，不是线上 1s 预算下的耗时）" % (
        _pct_ms(ms, 0.5), _pct_ms(ms, 0.99), ms[-1] if ms else 0.0))


def _normalize_reason(reason):
    """把 ``IllegalActionError`` 的消息文本归一化成一个分类 key（去掉座位号、
    牌名等具体值），供"按动作类型 × 拒绝原因"汇总用。只保留第一个冒号/
    数字之前的中文描述片段——足够区分"弃牌时机不对"/"手里没有 X"/
    "抓打圈内..."这几类，不需要真的解析消息结构。"""
    import re
    text = re.sub(r"[0-9]+", "N", reason)
    text = re.sub(r"[一-鿿]*[wbt]|[东南西北中发白]", "<tile>", text)
    return text[:40]


def _write_illegal_report(illegal_log):
    """追加写 ``reports/arena2_illegal.tsv``（追加而不是覆盖：``ab``/
    ``seatbias``/``smoke`` 调用方各自跑各自的，攒着看总体情况），返回
    按 (action, reason 归一化) 分类的计数 Counter，供终端汇总。"""
    if not illegal_log:
        return Counter()
    os.makedirs(os.path.join(ROOT, "reports"), exist_ok=True)
    path = os.path.join(ROOT, "reports", "arena2_illegal.tsv")
    is_new = not os.path.exists(path)
    cats = Counter()
    with open(path, "a", encoding="utf-8") as f:
        if is_new:
            f.write("phase\tseat\taction\treason\tsnapshot_json\n")
        for row in illegal_log:
            action_kind = (row["action"] or {}).get("action", "?")
            f.write("%s\t%s\t%s\t%s\t%s\n" % (
                row["phase"], row["seat"], action_kind, row["reason"],
                json.dumps(row["snapshot"], ensure_ascii=False)))
            cats[(action_kind, _normalize_reason(row["reason"]))] += 1
    return cats


def _code_sig():
    """缓存键里带上"决策相关代码+对手校准文件+模型文件"的哈希：这些变了，旧缓存的对局结果就不能复用。
    （mj/ 下所有 .py + arena2 自己 + models/ 里 mc/nn 的参数文件；weights.json 不进哈希——它由 frozen 读取，改了请 --no-cache。）"""
    h = hashlib.md5()
    paths = []
    for dirpath, _dirs, files in os.walk(os.path.join(ROOT, "mj")):
        paths.extend(os.path.join(dirpath, f) for f in sorted(files) if f.endswith(".py"))
    paths.sort()
    paths += [os.path.join(ROOT, "tools", "arena2.py")] + [os.path.join(ROOT, "models", m) for m in
                                                          ("mc_opp_params.json", "nn_policy.json", "nn_resid.json")]
    for path in paths:
        try:
            with open(path, "rb") as f:
                h.update(f.read())
        except OSError:
            h.update(path.encode())
    return h.hexdigest()[:6]


def cmd_ab(args):
    a_overlay = _load_overlay_file(args.a)
    b_overlay = _load_overlay_file(args.b)
    seeds = list(range(args.seed, args.seed + args.matches))
    tag = "ab_%s_%s_%s_%s" % (
        os.path.basename(args.a) if args.a else "current",
        os.path.basename(args.b) if args.b else "current",
        args.layout, _code_sig())
    (round_records, match_summaries, total_stats, illegal_log, new_runs, elapsed, reused,
     done_seeds, extras) = _run_ab(tag, seeds, a_overlay, b_overlay, args.layout, args.rounds, {}, args.jobs,
                                   args.no_cache, max_minutes=getattr(args, "max_minutes", None))
    if len(done_seeds) < len(seeds):
        print("到 --max-minutes 时间上限停止：完成 %d / %d 个种子，只用已完成的种子出结果。" % (
            len(done_seeds), len(seeds)))
    seeds = done_seeds
    if not seeds:
        print("没有任何完成的种子，无法出结果（--max-minutes 太短？）")
        return []

    os.makedirs(os.path.join(ROOT, "reports"), exist_ok=True)
    out_path = os.path.join(ROOT, "reports", "arena2_ab_%d.json" % int(time.time()))
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"a": args.a, "b": args.b, "layout": args.layout, "seeds": seeds,
                   "round_records": round_records, "match_summaries": match_summaries,
                   "stats": dict(total_stats)}, f, ensure_ascii=False)
    illegal_cats = _write_illegal_report(illegal_log)

    _print_ab_summary("A/B 配对检验", args.a, args.b, args.layout, seeds, round_records,
                      match_summaries, total_stats, new_runs, elapsed, reused, out_path, args.jobs)
    _print_role_table(round_records)
    _print_s1_table(round_records)
    _print_mc_summary(total_stats, extras)
    if getattr(args, "tag", None):
        diffs = _per_seed_diff(round_records)
        mean, se, half = _mean_ci95(diffs)
        t = _role_table(round_records)
        os.makedirs(os.path.join(ROOT, "reports", "suite"), exist_ok=True)
        with open(os.path.join(ROOT, "reports", "suite", "%s_%s.json" % (args.tag, args.layout)), "w", encoding="utf-8") as f:
            json.dump({"tag": args.tag, "layout": args.layout, "a": args.a, "b": args.b, "n_seeds": len(seeds),
                       "mean": mean, "lo": mean - half, "hi": mean + half,
                       "dealer": _role_diff(round_records, True), "non_dealer": _role_diff(round_records, False),
                       "roles": {"%s_%s" % k: v for k, v in t.items()}, "s1": _s1_table(round_records)},
                      f, ensure_ascii=False, indent=1)
    print("\n非法动作根因（按 动作类型/拒绝原因 分类，完整快照见 reports/arena2_illegal.tsv，验收线 0 条）：")
    if illegal_cats:
        for (action_kind, reason), n in illegal_cats.most_common(10):
            print("  %-8s %-42s %d" % (action_kind, reason, n))
    else:
        print("  （本次 0 条）")
    return round_records


def cmd_seatbias(args):
    """原 ``aa``：A=B=当前配置，检验对战平台本身有没有系统性座位偏差
    （不是策略强弱判定）。复用 ``ab`` 的全部统计口径。"""
    args.a = None
    args.b = None
    round_records = cmd_ab(args)
    # A=B 且每个种子跑遍全部排法时，A−B 配对差按对称性恒为 0，检测不出任何偏差——
    # 真正能暴露座位/庄位偏差的是"按座位的均分"（座位 0..3 各自每局场均分，按种子聚类的 95%CI）。
    print("\n按座位均分（每局场均分，按种子聚类 95%CI；某座位 CI 不含 0 = 平台有座位偏差）：")
    by = {}
    for r in round_records:
        by.setdefault((r["seed"], r["seat"]), []).append(r["delta_score"])
    for seat in range(4):
        vals = [statistics.mean(v) for (sd, st), v in by.items() if st == seat]
        mean, se, half = _mean_ci95(vals)
        print("  座位 %d：%+.3f  95%%CI [%+.3f, %+.3f]  %s（%d 个种子）" % (
            seat, mean, mean - half, mean + half, "含 0" if mean - half <= 0 <= mean + half else "**不含 0**", len(vals)))


# ---------------------------------------------------------------- bench

def cmd_bench(args):
    """吞吐量：单核跑 ``--seconds`` 秒，数完成了多少局，换算成"每核每
    小时局数"；同时记录每一步 ``choose_action`` 决策的耗时，报告 p50/p99。
    性能核 vs 能效核的对比由调用方跑两次得到——第二次在命令行前面套
    ``taskpolicy -c background``（macOS 把进程压到能效核调度），本命令
    自己不知道、也不需要知道当前跑在哪种核上，只如实报告测到的数字。"""
    started = time.time()
    rounds_done = 0
    seed = 0
    stats = Counter()
    all_times = []
    limit = min(args.seconds, args.max_minutes * 60) if getattr(args, "max_minutes", None) else args.seconds
    while time.time() - started < limit:
        m = Arena2Match(seed, rounds=8, track_timing=True).play()
        rounds_done += len(m.history)
        stats.update(m.stats)
        all_times.extend(m.decision_times)
        seed += 1
    elapsed = time.time() - started
    per_hour = rounds_done / elapsed * 3600
    all_times.sort()

    def _pct(p):
        if not all_times:
            return 0.0
        idx = min(len(all_times) - 1, int(len(all_times) * p))
        return all_times[idx]

    print("=== 吞吐量（单核，%.1fs） ===" % elapsed)
    print("完成局数 %d，折算每核每小时局数 %.0f" % (rounds_done, per_hour))
    print("决策耗时（choose_action 单次调用，n=%d）：p50 %.1fms  p90 %.1fms  p99 %.1fms  max %.1fms" % (
        len(all_times), _pct(0.50) * 1000, _pct(0.90) * 1000, _pct(0.99) * 1000,
        (all_times[-1] * 1000 if all_times else 0.0)))
    print("非法动作：%s" % (_illegal_by_type(stats) or "无"))
    print("多核估算：--jobs N 大致线性放大到 N × %.0f 局/小时（实际取决于机器）" % per_hour)
    print("能效核对比：另外跑一次 `taskpolicy -c background python3 tools/arena2.py bench --seconds %d`，"
          "两次的局/小时和 p50/p99 对照着看" % int(args.seconds))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_smoke = sub.add_parser("smoke", help="冒烟，<1 分钟")
    p_smoke.set_defaults(func=cmd_smoke)

    p_ab = sub.add_parser("ab", help="A/B 配对检验，长任务")
    p_ab.add_argument("--a", default=None, help="A 组权重覆盖文件，留空=当前配置")
    p_ab.add_argument("--b", default=None, help="B 组权重覆盖文件，留空=当前配置")
    p_ab.add_argument("--matches", type=int, default=20, help="种子数")
    p_ab.add_argument("--seed", type=int, default=0, help="起始种子")
    p_ab.add_argument("--jobs", type=int, default=1)
    p_ab.add_argument("--layout", choices=list(LAYOUTS), default="1v3")
    p_ab.add_argument("--rounds", type=int, default=8)
    p_ab.add_argument("--no-cache", action="store_true")
    p_ab.add_argument("--max-minutes", type=float, default=None, help="时间一到就停，用已完成的种子出结果")
    p_ab.add_argument("--tag", default=None, help="给汇总起名：写 reports/suite/<tag>_<layout>.json（arena_suite 用）")
    p_ab.set_defaults(func=cmd_ab)

    p_seatbias = sub.add_parser("seatbias", help="对战平台自检（原 aa），长任务")
    p_seatbias.add_argument("--matches", type=int, default=20, help="种子数")
    p_seatbias.add_argument("--seed", type=int, default=0, help="起始种子")
    p_seatbias.add_argument("--jobs", type=int, default=1)
    p_seatbias.add_argument("--layout", choices=list(LAYOUTS), default="2v2")
    p_seatbias.add_argument("--rounds", type=int, default=8)
    p_seatbias.add_argument("--no-cache", action="store_true")
    p_seatbias.add_argument("--max-minutes", type=float, default=None, help="时间一到就停，用已完成的种子出结果")
    p_seatbias.set_defaults(func=cmd_seatbias)

    p_bench = sub.add_parser("bench", help="吞吐量测量，长任务")
    p_bench.add_argument("--seconds", type=float, default=30.0)
    p_bench.add_argument("--max-minutes", type=float, default=None, help="时间上限（分钟），小于 --seconds 时以它为准")
    p_bench.set_defaults(func=cmd_bench)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
