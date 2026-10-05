"""G1 返修第 3c 条 + G2.1 快照构造联调：把我方 logs/*.jsonl 决策快照（线上
真实 snapshot，schema 见 mj/state.py）和 mj.sim.engine 强制回放到**同一个
服务端 seq** 时、经 mj.sim.snapshot.build_snapshot 转换后的状态逐字段
比对——不再用启发式候选点对齐（第一版工具的教训，见下面"对齐方法"）。

    python3 tools/sim_snapshot_check.py --limit 20    # 冒烟（<1 分钟）
    python3 tools/sim_snapshot_check.py               # 全量，交给用户执行

支持增量续跑：按 (game_id, round_no) 缓存到 tools/.cache/sim_snapshot_check.pkl
（签名=这个分组的 decision_id 列表 + events 文件 mtime/size + 代码版本），
重跑时只重算变化过的分组。``--no-cache`` 强制全部重算。

终端只打印一个 ≤40 行的汇总块；完整不一致明细（不再截断成"最多 40 条"）
写到 ``reports/sim_snapshot_check_mismatches.tsv``，按 field 分类，想看某一
类的明细自己 grep：
    grep -P '\tresponding_seats\t' reports/sim_snapshot_check_mismatches.tsv | head -20

======================================================================
对齐方法：精确 seq 关联，不再猜候选点
======================================================================
第一版工具按"日志里第 N 条决策 = 回放里我方座位第 N 次真实动作"、后来改成
"手牌内容 + wall_remaining 启发式"对齐，两版都验证过站不住：服务端的
response_peng/response_chi 阶段不是每次真实弃牌都会暴露给客户端（只在本
座位至少有一种非过牌选项时才会问），导致"决策记录"是真实响应窗口的一个
**子集**，手牌还可能连续多次不变，光靠内容匹配会挑错候选点（wall_remaining/
melds/responding_seats 混在一起错，典型的"对齐误差"信号）。

真正精确的关联链路（``mj/bot.py`` 的轮询循环 + ``mj/logging.py`` 的落盘
逻辑决定）：
1. 每条 ``kind=="decision"`` 记录带一个 ``state_hash``（规范化 DecisionState
   的哈希，``mj/logging.py::DecisionLog.action`` 用 ``_state_hash(_normalize_state(...))``
   算出来的）。
2. 每条 ``kind=="state_request_metric"`` 记录**用同一个函数**算
   ``state_hash``（``mj/bot.py`` 里 ``log.state_request_metric(...,
   state_hash=_state_hash(_normalize_state(snapshot, rules=rules)))``），
   还带着这次轮询问服务端"到第几号事件"的 ``returned_seq``。
3. 所以：给定一条 decision，在同一个 ``game_id`` 下找 ``state_hash`` 相同、
   时间戳不晚于这条 decision 的 ``state_request_metric``，取其中
   ``returned_seq`` 最大的一条，就是这条 decision 的真实服务端事件号。
4. ``tools/models/events/<game_id>.json`` 里每个事件自带全局单调递增的
   ``seq`` 字段（跨整场比赛不重置）。找到这个 round 的事件里最后一个
   ``seq <= returned_seq`` 的位置，把回放推进到那里，用
   ``mj.sim.snapshot.build_snapshot`` 生成这一刻我方座位看到的快照，逐
   字段跟 decision 记录比对。``responding_seats`` 的不一致（178682 条，
   全部 ``response_peng``、全部"日志比引擎多"）已经排查清楚、钉死成
   已知语义差异，不是资格判定本身有问题，见
   ``mj/sim/snapshot.py`` 模块 docstring 里 ``responding_seats`` 那一条。
5. 找不到匹配 ``state_request_metric`` 的 decision 记为"对不上号"，单独
   计数，要求 <1%（任务验收线）。

已知不能比对的字段：``discards``（``logs/*.jsonl`` 落盘的 ``decision``
记录本身没有这个字段，见 ``mj/sim/snapshot.py`` 模块 docstring）。
"""
import argparse
import glob
import hashlib
import json
import os
import pickle
import sys
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from mining_common import mark_auto_discards, merge_rounds  # noqa: E402
from mj.sim.engine import IllegalActionError, RoundEngine  # noqa: E402
from mj.sim.snapshot import build_snapshot  # noqa: E402
from mj.state import canonical_chain_count, canonical_piao  # noqa: E402

CACHE_PATH = os.path.join(ROOT, "tools", ".cache", "sim_snapshot_check.pkl")
_MAX_BACKTRACK = 5   # returned_seq 边界自纠最多往前退几格，见 main() 里的实证注释。
REPORT_PATH = os.path.join(ROOT, "reports", "sim_snapshot_check_mismatches.tsv")


def _find_events_file(game_id):
    for pattern in ("tools/models/events/%s.json", "models/events/%s.json"):
        path = os.path.join(ROOT, pattern % game_id)
        if os.path.exists(path):
            return path
    return None


def load_decisions(log_paths):
    """返回 {(game_id, round_no): [(time, payload), ...]}，按文件出现顺序。"""
    by_round = defaultdict(list)
    for path in log_paths:
        with open(path, encoding="utf-8") as source:
            for line in source:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("kind") != "decision":
                    continue
                p = obj["payload"]
                key = (p.get("game_id"), p.get("round_no"))
                by_round[key].append((obj.get("time", ""), p))
    return by_round


def load_metrics(log_paths):
    """返回 {(game_id, state_hash): [(time, returned_seq), ...]}（按时间排序）。"""
    index = defaultdict(list)
    for path in log_paths:
        with open(path, encoding="utf-8") as source:
            for line in source:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("kind") != "state_request_metric":
                    continue
                p = obj["payload"]
                sh = p.get("state_hash")
                if sh is None or p.get("returned_seq") is None:
                    continue
                key = (p.get("game_id"), sh)
                index[key].append((obj.get("time", ""), p.get("returned_seq")))
    for key in index:
        index[key].sort()
    return index


def _resolve_returned_seq(metrics_index, game_id, state_hash, decision_time):
    """同一个规范化 state_hash 可能被连续好几次轮询都命中（内容没变，服务端
    事件号却在推进——多半是别的座位的过牌不改变我方看到的归一化状态）。
    取其中最小的 returned_seq，不是最大的：state_hash 本身就包含
    responding_seats（见 mj/observability.py::_STATE_HASH_FIELDS），如果
    真的有别的座位过牌导致 responding_seats 变化，hash 必然跟着变——所以
    同一个 hash 反复出现，代表的是"这个状态从被观察到的那一刻起就没变过"，
    最小的 returned_seq 最贴近这个状态真正成立的那个事件边界；取最大值
    会把后续几次"服务端事件号已经往前走、但快照内容还没跟着刷新"的陈旧
    轮询也算进来，导致回放多吃进本不该算的别家响应事件（真实证据：
    a_cd674f9de243_r1_b2_t0 第 1 局，seat3 弃牌 seq=164 开碰窗口问
    [0,1,2]，取 max 会命中 seq=166（seat1/seat2 已经各自 pass 一次之后
    的轮询），回放出来的 responding_seats 变成只剩 [0]）。"""
    candidates = metrics_index.get((game_id, state_hash))
    if not candidates:
        return None, "no_metric_for_state_hash"
    eligible = [seq for (t, seq) in candidates if t <= decision_time]
    if not eligible:
        return None, "metric_exists_but_all_after_decision_time"
    return min(eligible), None


def _replay_to_seq(rnd, meta, target_seq, start_scores):
    """强制回放到"最后一个 seq<=target_seq 的事件"为止（含），返回
    (engine_or_None, error)。``start_scores``：这一局开局时的累计分（前面
    所有局 meta["scores"] 增量的和——真实 decision 记录里的 scores 是整场
    比赛累计分，不是单局增量）。"""
    hands = rnd.get("start_hands")
    if not hands or len(hands) != 4 or not all(hands):
        return None, "no_hands"
    dealer = meta.get("dealer", rnd.get("dealer"))
    if dealer is None:
        return None, "no_dealer"
    engine = RoundEngine.from_known_hands(hands, dealer, rnd["round_no"], scores=list(start_scores))
    events = rnd["events"]
    # 庄家起手 14 张、服务端从不为这张隐藏摸牌记 tile_drawn 事件，但真实
    # 决策快照证实服务端把这张牌当 drawn_tile 报给庄家的第一次决策（样本：
    # a_b478b2cbc5db_r1_b8_t0 第 1 局，returned_seq=0，drawn_tile="9w"，
    # 庄家手里恰好有 3 张"9w"）。无条件先摆上，回放真正处理到庄家第一次
    # 弃牌/摸牌时会被正常覆盖/清空，不碰 drawn_count/wall_remaining。
    engine.drawn_tile = engine.hands[dealer][-1]
    engine.active_tile = engine.hands[dealer][-1]
    if len(events) == 1 and events[0].get("type") == "round_ended":
        engine.could_self_draw_hu = bool(engine.can_self_draw_hu())
    auto_idx = mark_auto_discards(events)
    stop_at = -1
    for i, ev in enumerate(events):
        if ev.get("seq", 0) <= target_seq:
            stop_at = i
        else:
            break
    for i in range(stop_at + 1):
        ev = events[i]
        kind = ev.get("type")
        seat = ev.get("seat")
        tile = ev.get("tile")
        data = ev.get("data") or {}
        try:
            if kind == "tile_drawn":
                engine.step_draw(forced_tile=tile)
            elif kind == "tile_discarded":
                engine.apply_discard(seat, tile)
            elif kind == "chi":
                used = list(data.get("tiles") or [])
                if tile in used:
                    used.remove(tile)
                engine.apply_claim(seat, "chi", used, auto_draw=False)
            elif kind == "peng":
                engine.apply_claim(seat, "peng", None, auto_draw=False)
            elif kind == "gang":
                sub = data.get("kind")
                if sub == "ming":
                    engine.apply_claim(seat, "gang", None, auto_draw=False)
                else:
                    engine.apply_gang(seat, tile, sub, auto_draw=False)
            elif kind == "pass":
                engine.apply_pass(seat)
            elif kind == "timeout":
                if data.get("kind") == "response":
                    engine.apply_pass(seat)
            elif kind == "round_ended":
                if not data.get("draw"):
                    engine.apply_hu(meta.get("winner"))
        except IllegalActionError as exc:
            return None, "illegal:%s" % exc
        except (ValueError, KeyError, IndexError) as exc:
            return None, "exception:%r" % exc
    return engine, None


_MELD_KEY = lambda m: (m.get("kind"), sorted(m.get("tiles") or []))  # noqa: E731


def _normalize_melds_for_compare(melds):
    return [sorted((dict(m) for m in seat_melds), key=_MELD_KEY) for seat_melds in melds]


def _compare(engine_snap, decision, engine_piao):
    """返回逐字段不一致列表 [(field, engine_val, log_val), ...]。
    ``engine_piao``：引擎自己精确算出来的 ``piao_count[seat]``——不在
    ``build_snapshot`` 的原始快照里（服务端从不下发这个字段，见
    ``mj.sim.snapshot`` 模块 docstring），单独传进来只为了跟真实日志的
    （恒为 0 的）``piao`` 做一次对比，确认这条已知问题的样本数，不代表
    这个字段真的能从快照里读到。"""
    mismatches = []
    god_seat_log = decision.get("god", {}).get("god_discarder_seat", -1)
    checks = [
        ("wall_remaining", engine_snap["wall_remaining"], decision.get("wall_remaining")),
        ("phase", engine_snap["phase"], decision.get("phase")),
        ("turn", engine_snap["turn"], decision.get("turn")),
        ("dealer", engine_snap["dealer"], decision.get("dealer")),
        ("round_no", engine_snap["round_no"], decision.get("round_no")),
        ("scores", engine_snap["scores"], decision.get("scores")),
        ("hand", sorted(engine_snap["my_hand"]), sorted(decision.get("hand") or [])),
        ("melds", _normalize_melds_for_compare(engine_snap["melds"]),
         _normalize_melds_for_compare(decision.get("melds") or [[], [], [], []])),
        ("catch_play", engine_snap["god"]["catch_play"], bool(decision.get("god", {}).get("catch_play"))),
        ("god_discarder_seat", engine_snap["god"]["god_discarder_seat"], god_seat_log),
        ("chain_count", engine_snap["god"]["chain_count"], canonical_chain_count(decision)),
        ("piao", engine_piao, canonical_piao(decision)),
    ]
    if engine_snap["phase"] == "draw":
        checks.append(("drawn_tile", engine_snap["drawn_tile"], decision.get("drawn_tile") or ""))
    else:
        checks.append(("responding_seats", engine_snap["responding_seats"],
                       sorted(decision.get("responding_seats") or [])))
    for field, engine_val, log_val in checks:
        if engine_val != log_val:
            mismatches.append((field, engine_val, log_val))
    return mismatches


def _process_group(game_id, round_no, decisions, metrics_index, game_cache):
    """核对一个 (game_id, round_no) 分组，返回 (stats:Counter,
    mismatches:[(decision_id, seat, log_phase, eng_phase, field, ev, lv), ...])。"""
    stats = Counter()
    mismatches = []
    path = _find_events_file(game_id)
    if path is None:
        stats["skipped_no_events_file"] += len(decisions)
        return stats, mismatches
    if path in game_cache:
        game = game_cache[path]
    else:
        try:
            with open(path, encoding="utf-8") as source:
                game = json.load(source)
        except (OSError, ValueError):
            game = None
        game_cache[path] = game
    if game is None:
        stats["skipped_bad_events_file"] += len(decisions)
        return stats, mismatches
    info = {r.get("round_no"): r for r in game.get("rounds") or []}
    meta = info.get(round_no)
    target = None
    for rnd in merge_rounds(game):
        if rnd["round_no"] == round_no:
            target = rnd
            break
    if not meta or target is None or target.get("truncated"):
        stats["skipped_no_round_match"] += len(decisions)
        return stats, mismatches

    start_scores = [0, 0, 0, 0]
    for prior_no in sorted(r for r in info if isinstance(r, int) and r < round_no):
        delta = info[prior_no].get("scores")
        if isinstance(delta, list) and len(delta) == 4:
            start_scores = [a + b for a, b in zip(start_scores, delta)]

    seq_cache = {}
    for decision_time, d in decisions:
        seat = d.get("seat")
        returned_seq, err = _resolve_returned_seq(metrics_index, game_id, d.get("state_hash"), decision_time)
        if returned_seq is None:
            stats["unaligned_%s" % err] += 1
            continue
        log_phase = d.get("phase")

        def _get(seq):
            if seq not in seq_cache:
                seq_cache[seq] = _replay_to_seq(target, meta, seq, start_scores)
            return seq_cache[seq]

        engine, replay_err = _get(returned_seq)
        if engine is None:
            stats["unaligned_replay_%s" % replay_err] += 1
            continue
        snap = build_snapshot(engine, seat)
        log_turn = d.get("turn")

        def _window_matches(s):
            # 选点判据**只看** phase + turn（phase 本身已经区分了窗口类型：
            # draw / response_peng / response_chi）——不许用 responding_seats
            # 或任何其它字段是否一致来决定退不退、退几格，否则这一步选点就
            # 会"提前偷看"要比对的字段，把 responding_seats 的真实分歧焊死
            # 成 0，制造假阳性（见任务指令"禁止以其余字段是否一致作为回退
            # 条件"）。
            return s["phase"] == log_phase and s["turn"] == log_turn

        # returned_seq 的边界不是 100% 精确（见模块 docstring）：对某些
        # response_peng/response_chi 决策，"seq<=returned_seq" 会多算进
        # 几个其实还没生效的事件（通常是别的座位紧跟着的 pass，观察到过
        # 退 1 格和退 2 格才对上的真实样本，见 _resolve_returned_seq 的
        # 实证注释），表现为 phase/turn 对不上。这里只用 phase+turn 这两个
        # "窗口身份"字段做自纠：从 returned_seq 往前退，最多退
        # ``_MAX_BACKTRACK`` 格，找到第一个 phase+turn 都对上的就用那个；
        # 选定之后，包括 responding_seats 在内的所有字段都按普通比对处理，
        # 不参与选点、如实计入不一致数。一路退到底还是没对上（或者已经
        # 是 seq=0）就还是用最原始的结果。
        back_used = 0
        if not _window_matches(snap):
            for back in range(1, _MAX_BACKTRACK + 1):
                if returned_seq - back < 0:
                    break
                engine2, replay_err2 = _get(returned_seq - back)
                if engine2 is None:
                    continue
                snap2 = build_snapshot(engine2, seat)
                if _window_matches(snap2):
                    engine, snap = engine2, snap2
                    back_used = back
                    break
            else:
                back_used = None   # 退到底也没对上窗口身份，仍用最原始结果
        if back_used:
            stats["seq_off_by_one_corrected"] += 1
        bucket = "unmatched" if back_used is None else ("%d" % back_used if back_used < 3 else ">=3")
        stats["seq_backtrack_%s" % bucket] += 1
        stats["aligned"] += 1
        eng_phase = snap["phase"]
        for field, ev, lv in _compare(snap, d, engine.piao_count[seat]):
            stats["mismatch_%s" % field] += 1
            if field == "drawn_tile" and log_phase == "draw" and engine.turn == seat:
                stats["mismatch_drawn_tile_strict_draw_turn"] += 1
            mismatches.append((d.get("decision_id"), seat, log_phase, eng_phase, field, ev, lv))
    return stats, mismatches


def _code_version():
    paths = [os.path.abspath(__file__),
             os.path.join(ROOT, "mj", "sim", "engine.py"),
             os.path.join(ROOT, "mj", "sim", "snapshot.py")]
    digest = hashlib.md5()
    for p in paths:
        with open(p, "rb") as f:
            digest.update(f.read())
    return digest.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="只处理前 N 个 (game_id, round_no) 分组（冒烟）")
    ap.add_argument("--logs", default=None, help="逗号分隔的 log 文件列表，默认 logs/*.jsonl")
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()

    log_paths = args.logs.split(",") if args.logs else sorted(glob.glob(os.path.join(ROOT, "logs", "*.jsonl")))
    print("读取 %d 个决策日志文件……" % len(log_paths))
    by_round = load_decisions(log_paths)
    metrics_index = load_metrics(log_paths)
    print("共 %d 个 (game_id, round_no) 分组带决策记录" % len(by_round))

    keys = list(by_round.keys())
    if args.limit:
        keys = keys[:args.limit]

    code_ver = _code_version()
    cache = {}
    if not args.no_cache:
        try:
            with open(CACHE_PATH, "rb") as f:
                saved = pickle.load(f)
            if saved.get("ver") == code_ver:
                cache = saved.get("groups") or {}
        except (OSError, ValueError, EOFError, pickle.UnpicklingError, AttributeError):
            cache = {}

    def group_sig(game_id, round_no, decisions):
        path = _find_events_file(game_id)
        ev_sig = None
        if path and os.path.exists(path):
            st = os.stat(path)
            ev_sig = (st.st_mtime, st.st_size)
        return (ev_sig, tuple(d.get("decision_id") for _, d in decisions))

    game_cache = {}
    total_stats = Counter()
    all_mismatches = []
    total_decisions = 0
    fresh = {}
    reused = 0
    for done, (game_id, round_no) in enumerate(keys, 1):
        decisions = by_round[(game_id, round_no)]
        total_decisions += len(decisions)
        sig = group_sig(game_id, round_no, decisions)
        hit = cache.get((game_id, round_no))
        if hit and hit[0] == sig:
            stats, mismatches = hit[1], hit[2]
            reused += 1
        else:
            stats, mismatches = _process_group(game_id, round_no, decisions, metrics_index, game_cache)
            fresh[(game_id, round_no)] = (sig, stats, mismatches)
        total_stats.update(stats)
        for m in mismatches:
            all_mismatches.append((game_id, round_no) + m)
        if done % 2000 == 0 or done == len(keys):
            print("  已处理 %d / %d 个分组（复用缓存 %d）" % (done, len(keys), reused), flush=True)

    if not args.no_cache:
        merged = {k: cache[k] for k in cache if k not in fresh}
        merged.update(fresh)
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "wb") as out:
            pickle.dump({"ver": code_ver, "groups": merged}, out, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, CACHE_PATH)

    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as out:
        out.write("game_id\tround_no\tdecision_id\tseat\tlog_phase\teng_phase\tfield\tengine_value\tlog_value\n")
        for row in all_mismatches:
            out.write("\t".join(str(x) for x in row) + "\n")

    unaligned = sum(v for k, v in total_stats.items() if k.startswith("unaligned_"))
    aligned = total_stats["aligned"]
    print("\n=== 3c 精确 seq 对齐核对结果（完整明细见 %s） ===" % REPORT_PATH)
    print("分组数 %d，决策记录总数 %d，成功对齐 %d，对不上号 %d（%.2f%%，验收线 <1%%）" % (
        len(keys), total_decisions, aligned, unaligned, 100.0 * unaligned / total_decisions if total_decisions else 0.0))
    for k in sorted(total_stats):
        if k.startswith("unaligned_") or k.startswith("skipped_"):
            print("  %-45s %d" % (k, total_stats[k]))
    print("seq 边界自纠次数（选点只看 phase+turn，往前退几格才对上）：%d" % total_stats["seq_off_by_one_corrected"])
    print("  按退的格数分布（0=不用退，unmatched=退到底 phase+turn 仍对不上、用最原始结果）：")
    for bucket in ("0", "1", "2", ">=3", "unmatched"):
        n = total_stats.get("seq_backtrack_%s" % bucket, 0)
        if n:
            print("    退 %-10s %d" % (bucket, n))

    print("\n逐字段不一致（分母=对齐数 %d）：" % aligned)
    fields = ("wall_remaining", "phase", "turn", "dealer", "round_no", "scores", "hand", "melds",
              "catch_play", "god_discarder_seat", "chain_count", "piao", "drawn_tile", "responding_seats")
    for field in fields:
        bad = total_stats["mismatch_%s" % field]
        if aligned:
            print("  %-20s %8d 次（%6.3f%%）" % (field, bad, 100.0 * bad / aligned))
    strict = total_stats["mismatch_drawn_tile_strict_draw_turn"]
    strict_denom = total_stats["mismatch_drawn_tile"]
    print("其中 drawn_tile 的严格子集（真实 phase=draw 且 turn==本座位）不一致：%d" % strict)

    print("\n按 field 分类查看明细，例如：")
    print("  grep -P '\\tresponding_seats\\t' %s | head -20" % REPORT_PATH)
    print("  grep -P '\\tdrawn_tile\\t' %s | awk -F'\\t' '$5==\"draw\" && $6==\"draw\"' | head -20" % REPORT_PATH)


if __name__ == "__main__":
    main()
