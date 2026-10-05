"""阶段二（离线版）：在真实对局的决策点上跑不限时的蒙特卡洛，看"MC 最优"在
哪些牌型/局面分组里显著不同于"真实选择"（我方的实际打法 / 高手的实际打法）。

    python3 tools/mc_offline.py --max-points 6 --n 64 --jobs 2     # 冒烟（<1 分钟）
    python3 tools/mc_offline.py                                    # 全量，交给用户执行（预计耗时见汇总开头）

======================================================================
流程
======================================================================
1. **抽决策点**（单进程，缓存到 ``tools/.cache/mc_offline/points_*.pkl``）：
   对 ``tools/models/events`` 语料逐局强制回放（``RoundEngine``），在我方（OUR）和
   高手（MASTERS）的每次"摸牌后/吃碰后要出牌"的时刻生成**该座位视角的服务端快照**
   （``mj.sim.snapshot.build_snapshot``），并读出这个座位接下来真实做的动作。
   不用 ``logs/*.jsonl`` 对齐——事件流里高手和我方的动作都有，快照由引擎直接生成。
   三类（一个点只归一类，优先级 ②>①>③）：
     ① 有财神的出牌：手里有财神、真实动作是弃牌；
     ② 胡/飘/弃胡：此刻能自摸（真实动作 hu 或弃牌=弃胡），或真实动作是自由打出财神（飘）；
     ③ 门清路线选择：副露 0、七对向听 <=3、没有能胡/没有财神的出牌。
   每个（我方/高手 × 类别）桶最多 ``--max-points`` 个，每个文件每桶最多 ``--per-file`` 个。
2. **MC**（多进程，结果逐点追加到 ``tools/.cache/mc_offline/results_*.jsonl``，可续跑）：
   每个候选动作（真实动作 + 距离最优的几个备选，胡/飘类含胡/弃胡/打财神）在**同一批**
   ``--n`` 个补全牌局上用 ``mj.mc.slim`` 推演到本局结束；路线（R_A/R_B/R_C/R_D，
   没财神只有 R_A/R_C）每个候选在前 ``--pilot`` 局上选一次、之后固定，避免反复在噪声
   上选极值；配对差用选路之后的局数算。
   价值 = 本局我方得分增量 + 连庄价值 V(round_no)（胡牌时，``models/dealer_value.json``）。
3. 输出 ``reports/mc_offline.tsv``（每点一行）+ <=40 行汇总：按
   （财神数 x 副露数 x 离胡距离 x 墙剩余档 x 局号档）分组，给出"MC 显著推翻真实选择"
   （配对差 > 1.96 倍标准误）的比例、平均得分差，并列出前 10 个组。

注意：推演里对手策略是 ``mj.mc.slim.DEFAULT_PARAMS`` 的未校准默认值（还没跟
``reports/sim_calibrate.json`` 的高手曲线拟合），所以绝对得分差只作相对参考，
结论要看"哪些分组稳定地显著"，不要把单个点的差当真值。
"""
import argparse
import glob
import hashlib
import json
import os
import pickle
import random
import sys
import time
from collections import Counter, defaultdict
from multiprocessing import Pool

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
os.environ.setdefault("MJ_WEIGHTS_NO_FILE", "1")

from baotou_funnel import MASTERS, OUR  # noqa: E402
from mining_common import discover_files, mark_auto_discards, merge_rounds, seat_names  # noqa: E402
from mj.mc.determinize import determinize_batch  # noqa: E402
from mj.mc.fast import hu_distance, is_hu  # noqa: E402
from mj.mc.slim import Slim, play_out  # noqa: E402
from mj.shanten import pair_shanten  # noqa: E402
from mj.sim.engine import IllegalActionError, RoundEngine  # noqa: E402
from mj.sim.snapshot import build_snapshot  # noqa: E402
from mj.tiles import JOKER, JOKER_IDX, TILE_INDEX, to_counts  # noqa: E402

CACHE_DIR = os.path.join(ROOT, "tools", ".cache", "mc_offline")
PARAMS_PATH = os.path.join(ROOT, "models", "mc_opp_params.json")
REPORT = os.path.join(ROOT, "reports", "mc_offline.tsv")
CATS = ("joker_discard", "hu_piao", "menqing_route")

_DEALER_V = None


def dealer_value(round_no):
    global _DEALER_V
    if _DEALER_V is None:
        try:
            with open(os.path.join(ROOT, "models", "dealer_value.json"), encoding="utf-8") as f:
                _DEALER_V = json.load(f).get("V") or {}
        except (OSError, ValueError):
            _DEALER_V = {}
    return float(_DEALER_V.get(str(round_no), 0.0))


# ====================================================================== 1. 抽决策点

def _apply_event(engine, ev):
    kind, seat, tile = ev.get("type"), ev.get("seat"), ev.get("tile")
    data = ev.get("data") or {}
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
        if data.get("kind") == "ming":
            engine.apply_claim(seat, "gang", None, auto_draw=False)
        else:
            engine.apply_gang(seat, tile, data.get("kind"), auto_draw=False)
    elif kind == "pass" or (kind == "timeout" and data.get("kind") == "response"):
        engine.apply_pass(seat)


def _classify(engine, seat, action):
    """返回类别名或 None。``action``：("discard", tile)/("hu", None)/("gang", tile)。"""
    hand = engine.hands[seat]
    mg = engine.meld_groups(seat)
    hu_now = engine.can_self_draw_hu() is not None if engine.drawn_tile is not None else False
    free_piao = (action[0] == "discard" and action[1] == JOKER
                 and ((not engine.catch_play) or seat == engine.god_discarder_seat))
    if hu_now or action[0] == "hu" or free_piao:
        return "hu_piao"
    if action[0] != "discard":
        return None
    if JOKER in hand:
        return "joker_discard"
    if mg == 0 and pair_shanten(to_counts(hand)) <= 3:
        return "menqing_route"
    return None


def scan_file(task):
    """返回 [point dict]。``task`` = (path, rate_seed, per_file, targets_mode)。"""
    path, seed, per_file = task
    try:
        with open(path, encoding="utf-8") as f:
            game = json.load(f)
    except (OSError, ValueError):
        return []
    names = seat_names(game)
    if len(names) != 4:
        return []
    who = {}
    for s, n in enumerate(names):
        if n == OUR:
            who[s] = "ours"
        elif n in MASTERS:
            who[s] = "master"
    if not who:
        return []
    info = {r.get("round_no"): r for r in game.get("rounds") or []}
    rng = random.Random("%s:%s" % (seed, os.path.basename(path)))
    got = Counter()
    out = []
    for rnd in merge_rounds(game):
        meta = info.get(rnd["round_no"])
        hands = rnd.get("start_hands")
        if not meta or not hands or len(hands) != 4 or rnd.get("truncated") or not all(hands):
            continue
        start_scores = [0, 0, 0, 0]
        for prior in sorted(k for k in info if isinstance(k, int) and k < rnd["round_no"]):
            d = info[prior].get("scores")
            if isinstance(d, list) and len(d) == 4:
                start_scores = [a + b for a, b in zip(start_scores, d)]
        engine = RoundEngine.from_known_hands(hands, meta.get("dealer", rnd.get("dealer")),
                                              rnd["round_no"], scores=start_scores)
        events = rnd["events"]
        auto = mark_auto_discards(events)
        pending = None   # (seat, 该座位视角快照, 类别)
        for i, ev in enumerate(events):
            kind, seat = ev.get("type"), ev.get("seat")
            if pending is not None and seat == pending[0] and (
                    (kind == "tile_discarded" and i not in auto) or kind == "gang"
                    or (kind == "round_ended" and not (ev.get("data") or {}).get("draw")
                        and ev.get("seat") == pending[0])):
                action = ("hu", None) if kind == "round_ended" else (
                    ("gang", ev.get("tile")) if kind == "gang" else ("discard", ev.get("tile")))
                pseat, snap, cat = pending
                pending = None
                if cat and got[(who[pseat], cat)] < per_file and rng.random() < 0.5:
                    got[(who[pseat], cat)] += 1
                    out.append({"id": "%s:%d:%d" % (os.path.basename(path), rnd["round_no"], i),
                                "who": who[pseat], "cat": cat, "seat": pseat, "round_no": rnd["round_no"],
                                "snapshot": snap, "action": action})
            if kind == "round_ended":
                break
            try:
                _apply_event(engine, ev)
            except (IllegalActionError, ValueError, KeyError, IndexError):
                break
            if engine.finished:
                break
            if engine.phase == "draw" and engine.turn in who and (
                    (kind == "tile_drawn" and seat == engine.turn) or kind in ("chi", "peng")):
                if engine.drawn_tile is not None or kind in ("chi", "peng"):
                    nxt = _peek_action(events, i + 1, engine.turn, auto)
                    if nxt is None:
                        continue
                    cat = _classify(engine, engine.turn, nxt)
                    if cat:
                        pending = (engine.turn, build_snapshot(engine, engine.turn), cat)
    return out


def _peek_action(events, start, seat, auto):
    for j in range(start, min(len(events), start + 12)):
        ev = events[j]
        if ev.get("seat") != seat:
            if ev.get("type") in ("round_ended",):
                break
            continue
        t = ev.get("type")
        if t == "tile_discarded" and j not in auto:
            return ("discard", ev.get("tile"))
        if t == "gang":
            return ("gang", ev.get("tile"))
        if t == "round_ended":
            return ("hu", None)
    return None


def collect_points(args):
    sig = hashlib.md5(("%s|%s|%s|%s" % (args.max_points, args.per_file, args.seed, args.limit_files)).encode()
                      ).hexdigest()[:10]
    path = os.path.join(CACHE_DIR, "points_%s.pkl" % sig)
    if os.path.exists(path):
        with open(path, "rb") as f:
            return pickle.load(f), sig
    files = discover_files(limit=args.limit_files)
    random.Random(args.seed).shuffle(files)
    buckets = defaultdict(list)
    full = lambda: all(len(buckets[(w, c)]) >= args.max_points for w in ("ours", "master") for c in CATS)  # noqa: E731
    tasks = [(p, args.seed, args.per_file) for p in files]
    with Pool(args.jobs) as pool:
        for done, pts in enumerate(pool.imap(scan_file, tasks, chunksize=2), 1):
            for pt in pts:
                b = buckets[(pt["who"], pt["cat"])]
                if len(b) < args.max_points:
                    b.append(pt)
            if done % 200 == 0:
                print("  抽点：已扫 %d / %d 个文件，桶：%s" % (
                    done, len(files), {"%s/%s" % k: len(v) for k, v in sorted(buckets.items())}), flush=True)
            if full():
                pool.terminate()
                break
    points = [pt for b in buckets.values() for pt in b]
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(path, "wb") as f:
        pickle.dump(points, f)
    return points, sig


# ====================================================================== 2. MC

def slim_from_snapshot(snap, deal, our_seat):
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
              136 - 52 - (server_wall + 1))
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
    st.catch_play = bool(god.get("catch_play"))
    g = god.get("god_discarder_seat", -1)
    st.god_discarder_seat = None if g is None or g < 0 else g
    return st


def _candidates(snap, action, hu_ok, k=5):
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

    add(tuple(action))
    if hu_ok:
        add(("hu", None))
        if snap.get("drawn_tile"):
            add(("discard", snap["drawn_tile"]))
    if JOKER in hand:
        add(("discard", JOKER))
    for _d, t in scored[:k]:
        add(("discard", t))
    return cands


def _apply_candidate(st, seat, cand):
    kind, tile = cand
    if kind == "hu":
        st.apply_hu(seat)
    elif kind == "gang":
        t = TILE_INDEX[tile]
        st.apply_gang(seat, t, "bu" if t in st.pengs[seat] else "an")
    else:
        st.apply_discard(seat, TILE_INDEX[tile])


def _routes(hand):
    return ["R_A", "R_B", "R_C", "R_D"] if JOKER in hand else ["R_A", "R_C"]


def _value(st, seat, before):
    v = st.scores[seat] - before
    if not st.is_draw and st.winner == seat:
        v += dealer_value(st.round_no)
    return v


def _mean_se(v):
    n = len(v)
    if n < 2:
        return (sum(v) / n if n else 0.0), float("inf")
    m = sum(v) / n
    return m, (sum((x - m) ** 2 for x in v) / (n - 1) / n) ** 0.5


def evaluate_point(job):
    pt, n, pilot, k, params = job
    snap, seat = pt["snapshot"], pt["seat"]
    mg = len((snap.get("melds") or [[], [], [], []])[seat])
    c14 = to_counts(snap["my_hand"])
    hu_ok = bool(snap.get("drawn_tile")) and is_hu(c14, mg)
    cands = _candidates(snap, pt["action"], hu_ok, k)
    prod = cands.index(tuple(pt["action"]))
    seed = int(hashlib.md5(pt["id"].encode()).hexdigest()[:8], 16)
    deals = determinize_batch(snap, n, seed)
    routes = _routes(snap["my_hand"])
    rng = random.Random(seed ^ 0x5EED)
    bases = [slim_from_snapshot(snap, d, seat) for d in deals]
    before = snap.get("scores")[seat] if snap.get("scores") else 0
    vals, chosen, dead = [], [], set()
    for ci, cand in enumerate(cands):
        best_route, best_mean, per_route_pilot = routes[0], None, {}
        for r in routes:
            v = []
            for st0 in bases[:pilot]:
                st = st0.clone()
                try:
                    _apply_candidate(st, seat, cand)
                except IllegalActionError:
                    dead.add(ci)
                    break
                play_out(st, rng, our_seat=seat, route=r, params=params)
                v.append(_value(st, seat, before))
            if ci in dead:
                break
            m = sum(v) / len(v)
            per_route_pilot[r] = m
            if best_mean is None or m > best_mean:
                best_mean, best_route = m, r
        chosen.append(best_route)
        if ci in dead:
            vals.append([])
            continue
        v = []
        for st0 in bases[pilot:]:
            st = st0.clone()
            _apply_candidate(st, seat, cand)
            play_out(st, rng, our_seat=seat, route=best_route, params=params)
            v.append(_value(st, seat, before))
        vals.append(v)
    live = [i for i in range(len(cands)) if i not in dead and vals[i]]
    if prod not in live:
        return None
    # 选最优和测差距用**不同**的牌局（前半选、后半测）：同一批样本上又选又比会把
    # "挑出来的最大值"的噪声当成真实优势，平均差永远为正。
    h = min(len(vals[i]) for i in live) // 2
    sel = {i: sum(vals[i][:h]) / h for i in live}
    best = max(live, key=lambda i: sel[i])
    means = {i: sum(vals[i][h:]) / len(vals[i][h:]) for i in live}
    diffs = [a - b for a, b in zip(vals[best][h:], vals[prod][h:])]
    dm, dse = _mean_se(diffs) if best != prod else (0.0, 0.0)
    return {
        "id": pt["id"], "who": pt["who"], "cat": pt["cat"], "round_no": snap["round_no"],
        "jokers": min(c14[JOKER_IDX], 2), "melds": min(mg, 2),
        "dist": min(hu_distance(tuple(_pre13(c14, snap)), mg), 3),
        "wall": snap.get("wall_remaining"), "production": "%s:%s" % cands[prod], "mc_best": "%s:%s" % cands[best],
        "n_cands": len(live), "prod_mean": means[prod], "best_mean": means[best], "diff": dm, "se": dse,
        "sig": bool(best != prod and dse > 0 and dm > 1.96 * dse), "best_route": chosen[best],
    }


def _pre13(c14, snap):
    c = list(c14)
    dt = snap.get("drawn_tile")
    if dt:
        c[TILE_INDEX[dt]] -= 1
    return c


def _run_job(job):
    try:
        return evaluate_point(job)
    except Exception as exc:   # noqa: BLE001
        return {"id": job[0]["id"], "error": repr(exc)}


# ====================================================================== 3. 汇总

def _wall_bin(w):
    w = w or 0
    return "w>=70" if w >= 70 else "50-69" if w >= 50 else "30-49" if w >= 30 else "w<30"


def _round_bin(r):
    return "r1-2" if r <= 2 else "r3-5" if r <= 5 else "r6-8"


def summarize(results, errors, elapsed_note):
    print("=== mc_offline 汇总（明细 %s） ===" % REPORT)
    print(elapsed_note)
    print("有效点 %d，出错 %d" % (len(results), errors))
    for who in ("ours", "master"):
        for cat in CATS:
            rs = [r for r in results if r["who"] == who and r["cat"] == cat]
            if rs:
                sig = [r for r in rs if r["sig"]]
                print("  %-6s %-14s n=%4d  MC 显著推翻 %5.1f%%  平均得分差(MC最优-真实) %+.3f  (显著点上 %+.3f)" % (
                    who, cat, len(rs), 100.0 * len(sig) / len(rs), sum(r["diff"] for r in rs) / len(rs),
                    (sum(r["diff"] for r in sig) / len(sig)) if sig else 0.0))
    groups = defaultdict(list)
    for r in results:
        groups[(r["jokers"], r["melds"], r["dist"], _wall_bin(r["wall"]), _round_bin(r["round_no"]))].append(r)
    rows = []
    for key, rs in groups.items():
        sig = [r for r in rs if r["sig"]]
        rows.append((len(sig), len(rs), key, sum(r["diff"] for r in rs) / len(rs)))
    rows.sort(key=lambda x: (-x[0], -x[1]))
    print("前 10 组（财神数,副露数,离胡距离,墙剩余档,局号档）按显著推翻点数排序：")
    for nsig, n, key, md in rows[:10]:
        print("  财神%d 副露%d 距离%d %-6s %-5s  n=%4d  推翻 %4d (%5.1f%%)  平均差 %+.3f" % (
            key[0], key[1], key[2], key[3], key[4], n, nsig, 100.0 * nsig / n, md))


def load_params(allow_uncalibrated, path=None):
    """对手策略参数来自 ``tools/mc_check.py calibrate``（拟合高手曲线）。没有校准文件就拒绝运行：
    未校准的对手偏慢，会让 MC 系统性高估"慢而大"的路线（见模块 docstring）。"""
    try:
        with open(path or PARAMS_PATH, encoding="utf-8") as f:
            obj = json.load(f)
        return obj["params"], bool(obj.get("all_within_3pp"))
    except (OSError, ValueError, KeyError):
        if allow_uncalibrated:
            print("警告：没有 %s，使用未校准的默认对手参数（--allow-uncalibrated，只该用于冒烟）" % PARAMS_PATH)
            return None, False
        print("拒绝运行：找不到 %s。先跑 tools/mc_check.py calibrate（依赖 reports/sim_calibrate.json）；"
              "冒烟可加 --allow-uncalibrated。" % PARAMS_PATH)
        sys.exit(2)


def route_report(params, n=300):
    """我方各路线在 slim 推演里的成牌率，对照真实我方/高手（只报告，不拟合）：
    每局 seat0 按该路线走、其余三家默认策略，统计 seat0 胡牌率/平均番数，分"起手有财神"子集。"""
    from mj.sim.engine import build_wall
    out = {}
    for route in ("R_A", "R_B", "R_C", "R_D"):
        win = fans = jw = jn = jfans = tot = 0
        for seed in range(n):
            st = Slim.from_wall(build_wall(seed + 31337), 0, 1, [0, 0, 0, 0])
            has_j = st.counts[0][JOKER_IDX] > 0
            play_out(st, random.Random(seed), our_seat=0, route=route, params=params)
            tot += 1
            jn += has_j
            if not st.is_draw and st.winner == 0:
                win += 1
                fans += st.fan
                if has_j:
                    jw += 1
                    jfans += st.fan
        out[route] = (win / tot, fans / win if win else 0.0, jw / jn if jn else 0.0, jfans / jw if jw else 0.0)
    real = {}
    try:
        with open(os.path.join(ROOT, "reports", "sim_calibrate.json"), encoding="utf-8") as f:
            sc = json.load(f)
        for g in ("高手", "我们(当前)"):
            c = sc[g]["cumulative_hu_curve_dealer"]["20"][0] * 0.25 + sc[g]["cumulative_hu_curve_nondealer"]["20"][0] * 0.75
            fd = sc[g]["fan_distribution"]
            mean_fan = sum(fd[k][0] * v for k, v in (("1", 1), ("2", 2), ("4", 4), ("8", 8), ("16+", 16)))
            real[g] = (c, mean_fan)
    except (OSError, ValueError, KeyError):
        pass
    return out, real


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-points", type=int, default=300, help="每个（我方/高手 x 类别）桶最多多少个决策点")
    ap.add_argument("--per-file", type=int, default=3, help="每个文件每桶最多取几个")
    ap.add_argument("--n", type=int, default=512, help="每个候选的补全牌局数")
    ap.add_argument("--pilot", type=int, default=128, help="选路线用的前几局（不计入配对差）")
    ap.add_argument("--topk", type=int, default=5, help="真实选择之外再取几个距离最优的备选")
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--limit-files", type=int, default=None)
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--params", default=None, help="校准参数文件（默认 models/mc_opp_params.json；冒烟用 mc_check calibrate --quick 的产物）")
    ap.add_argument("--max-minutes", type=float, default=None, help="时间一到就停，用已完成的点出汇总")
    ap.add_argument("--allow-uncalibrated", action="store_true", help="没有校准参数也运行（只用于冒烟）")
    args = ap.parse_args()
    opp_params, calibrated_ok = load_params(args.allow_uncalibrated, args.params)
    if args.pilot >= args.n:
        ap.error("--pilot 必须小于 --n")
    os.makedirs(CACHE_DIR, exist_ok=True)

    t0 = time.time()
    points, sig = collect_points(args)
    print("决策点 %d 个：%s" % (len(points), dict(Counter("%s/%s" % (p["who"], p["cat"]) for p in points))))
    rsig = hashlib.md5(("%s|%s|%s|%s|%s|%s" % (sig, args.n, args.pilot, args.topk, args.seed,
                                                json.dumps(opp_params, sort_keys=True))).encode()).hexdigest()[:10]
    rpath = os.path.join(CACHE_DIR, "results_%s.jsonl" % rsig)
    done = {}
    if os.path.exists(rpath) and not args.no_cache:
        with open(rpath, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                done[r["id"]] = r
    todo = [p for p in points if p["id"] not in done]
    print("缓存命中 %d，待算 %d（每点约 %d 候选 x 路线 x %d 局推演）" % (len(done), len(todo), args.topk + 2, args.n))
    errors = 0
    if todo:
        jobs = [(p, args.n, args.pilot, args.topk, opp_params) for p in todo]
        with Pool(args.jobs) as pool, open(rpath, "a", encoding="utf-8") as out:
            deadline = t0 + args.max_minutes * 60 if args.max_minutes else None
            for i, r in enumerate(pool.imap_unordered(_run_job, jobs, chunksize=1), 1):
                if deadline and time.time() > deadline:
                    print("到 --max-minutes 上限，停止（已完成的点照常汇总）", flush=True)
                    pool.terminate()
                    break
                if r is not None:
                    out.write(json.dumps(r, ensure_ascii=False) + "\n")
                    out.flush()
                    done[r["id"]] = r
                if i % 50 == 0:
                    print("  MC：%d / %d（%.0fs）" % (i, len(todo), time.time() - t0), flush=True)
    results = [r for r in done.values() if "error" not in r]
    errors = sum(1 for r in done.values() if "error" in r)
    with open(REPORT, "w", encoding="utf-8") as f:
        cols = ("id", "who", "cat", "round_no", "jokers", "melds", "dist", "wall", "production", "mc_best",
                "n_cands", "prod_mean", "best_mean", "diff", "se", "sig", "best_route")
        f.write("\t".join(cols) + "\n")
        for r in sorted(results, key=lambda r: r["id"]):
            f.write("\t".join(str(r[c]) for c in cols) + "\n")
    note = "总耗时 %.0fs（含抽点；重跑只补算新增点）；对手参数：%s%s" % (
        time.time() - t0, ("策略参数 %s + 胡牌率补足表" % {k: v for k, v in opp_params.items() if k != "topup"}) if opp_params and "topup" in opp_params else (opp_params or "未校准默认值"),
        "" if calibrated_ok or opp_params is None else "（校准未全部达到 <=3pp，见 reports/mc_calibrate.json）")
    summarize(results, errors, note)
    routes, real = route_report(opp_params, 100 if args.allow_uncalibrated else 300)
    print("路线成牌率（slim 推演里 seat0 按路线走，全部起手 / 起手有财神；只报告不拟合）：")
    for r, (w, f, jw, jf) in routes.items():
        print("  %s 胡牌率 %.3f 均番 %.2f | 有财神起手 胡牌率 %.3f 均番 %.2f" % (r, w, f, jw, jf))
    for g, (c, mf) in real.items():
        print("  真实 %-10s 每座位胡牌率 %.3f 均番(按分布估) %.2f" % (g, c, mf))


if __name__ == "__main__":
    main()
