"""S3 搜索改进器（专家）：基础策略 = **生产**（我方后续每一步都调用生产 ``choose_action``），对手 = 高手校准的 slim
（+ 胡牌率补足表，``models/mc_opp_params.json``）。在真实对局的决策状态上，对"生产前 K 个出牌候选"做单步策略改进：

    python3 tools/train/search_s3.py collect --n 200            # 抽状态（优先：有财神出牌 / 门清路线），秒~分钟级
    python3 tools/train/search_s3.py bench --seconds 60          # 实测单次推演耗时 -> 每 1000 个状态在 N vCPU 上要几小时
    caffeinate -i nice -n 15 python3 tools/train/search_s3.py run --jobs 6 --max-minutes 60   # 跑搜索，可断线续跑

方法（同 mj/mc/decide.py 的离线口径，但我方后续策略是生产而不是弱贪心）：
1. 状态：真实对局回放里任意玩家的"摸牌后出牌"决策（``iter_decisions``）；跳过胡牌态、抓打圈受限、生产选了杠的状态。
   采样按类别配额：有财神 50% / 门清路线 30% / 其它 20%（"胡飘"状态 S2' 暂不管，沿用生产）。
2. 候选 = ``mj.nn.cands.production_candidates``（生产前 K 名，第一个一定是生产选择 P）。
3. **选择批**：``--n-sel`` 副补全牌局（``determinize_batch``，CRN：所有候选共用同一批牌局、同一个对手随机数种子）上，
   每个候选推演到终局，取"除 P 外均值最高"的候选 alt。
4. **检验批**：另一批 ``--n-test`` 副**独立**牌局，**所有候选**与 P 做配对差（得到每个候选的优势 adv 和标准误）；alt 的差 >
   ``z`` 倍标准误（默认 z=2）且 >0 才算"改进"。选择和检验用不同牌局，避免"挑最大值又拿它当证据"。
5. 价值 = 我方得分增量 + 连庄价值 V(局号)（胡了才加），同 MC。
输出（jsonl，每状态一条）：``id / cls / prod / cands / x（逐候选特征，mj.nn.cands）/ adv（每个候选相对生产动作的优势，
**分/局**，检验批上的配对差，adv[0]=0）/ adv_se / y,improved（alt 的下标/是否 z 倍标准误以上的改进，兼容旧格式）/ diff,se（alt 的）/
sel_means / n_sel / n_test / base``。S2' 的训练目标是 adv（回归"相对生产的优势"，不再在生产评分上加修正，见 train_resid.py）。

我方一步推演的"生产决策"是 ``slim.to_snapshot`` 出的服务端 schema 快照上调用 ``mj.bot.choose_action``（slim 与 RoundEngine
逐步对照 0 不一致，快照输出同样逐字段对照）。**注意**：这里的生产用 ``frozen_file_weights``（读 models/weights.json，同 arena2）。
"""
import argparse
import hashlib
import json
import os
import random
import sys
import time
from collections import Counter, defaultdict
from multiprocessing import Pool

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "tools", "train"))
os.environ.setdefault("MJ_WEIGHTS_NO_FILE", "1")

from build_dataset import iter_decisions  # noqa: E402
from mining_common import discover_files  # noqa: E402
from mj import bot  # noqa: E402
from mj.fit import frozen_file_weights  # noqa: E402
from mj.mc import decide  # noqa: E402
from mj.mc.chain_tracker import ChainTracker  # noqa: E402
from mj.mc.determinize import determinize_batch  # noqa: E402
from mj.mc.slim import DEFAULT_PARAMS, DRAW as DEAL, RESP, _draw_action, _resp_action  # noqa: E402
from mj.nn import cands as C  # noqa: E402
from mj.shanten import pair_shanten  # noqa: E402
from mj.sim.engine import IllegalActionError  # noqa: E402
from mj.tiles import JOKER, TILE_INDEX, to_counts  # noqa: E402

CACHE = os.environ.get("MJ_SEARCH_DIR") or os.path.join(ROOT, "tools", ".cache", "search")   # 冒烟用 MJ_SEARCH_DIR 隔离
STATES = os.path.join(CACHE, "states.jsonl")
RESULTS = os.path.join(CACHE, "results.jsonl")
CLASS_SHARE = (("joker", 0.5), ("menqing", 0.3), ("other", 0.2))
_W = {}


# ---------------------------------------------------------------- 状态抽样

def classify(snap):
    """有财神出牌 / 门清路线 / 其它；不是"摸牌后出牌、非胡牌态、非抓打圈受限"返回 None。"""
    if snap.get("phase") != "draw" or not snap.get("drawn_tile"):
        return None
    me = snap["seat"]
    god = snap.get("god") or {}
    if god.get("catch_play") and god.get("god_discarder_seat") != me:
        return None
    hand = snap["my_hand"]
    mg = len(snap["melds"][me])
    from mj.mc.fast import is_hu
    if is_hu(to_counts(hand), mg):
        return None
    if JOKER in hand:
        return "joker"
    if mg == 0 and pair_shanten(to_counts(hand)) <= 3:
        return "menqing"
    return "other"


def cmd_collect(args):
    os.makedirs(CACHE, exist_ok=True)
    have = {}
    if os.path.exists(STATES):
        with open(STATES, encoding="utf-8") as f:
            for line in f:
                r = json.loads(line)
                have[r["id"]] = r["cls"]
    quota = {c: int(round(args.n * s)) for c, s in CLASS_SHARE}
    got = Counter(have.values())
    files = discover_files()
    random.Random(args.seed).shuffle(files)
    started = time.time()
    deadline = started + args.max_minutes * 60 if args.max_minutes else None
    new = 0
    with open(STATES, "a", encoding="utf-8") as out:
        for path in files:
            if all(got[c] >= quota[c] for c in quota) or (deadline and time.time() > deadline):
                break
            per_file = Counter()
            for rec in iter_decisions(path, keep=args.keep, seed=args.seed):
                if rec["action"].get("action") != "discard":
                    continue
                snap = rec["snapshot"]
                cls = classify(snap)
                if cls is None or got[cls] >= quota[cls] or per_file[cls] >= args.per_file or rec["id"] in have:
                    continue
                got[cls] += 1
                per_file[cls] += 1
                new += 1
                out.write(json.dumps({"id": rec["id"], "cls": cls, "snapshot": snap, "ctx": rec["ctx"]},
                                     ensure_ascii=False, separators=(",", ":")) + "\n")
    print("=== search_s3 collect（%.0fs） ===" % (time.time() - started))
    print("新增状态 %d；累计 %s；配额 %s；文件 %s" % (new, dict(got), quota, STATES))


# ---------------------------------------------------------------- 推演（我方=生产，对手=slim）

def _init(opp_path):
    params, ok = decide.load_opp_params(opp_path)
    if params is None:
        raise SystemExit("没有对手校准参数 %s（先跑 phase2_batch.sh mccal）" % (opp_path or decide.PARAMS_PATH))
    _W["opp"] = params
    _W["p"] = {**DEFAULT_PARAMS, **params}
    _W["ok"] = ok
    ctxm = frozen_file_weights()
    ctxm.__enter__()          # 进程内一直冻结（读一次 models/weights.json）
    _W["frozen"] = ctxm


def _apply_our_draw(st, our, tracker):
    snap = st.to_snapshot(our)
    action = bot.choose_action(snap, None) or {"action": "discard", "tile": snap["drawn_tile"] or snap["my_hand"][-1]}
    kind = action.get("action")
    try:
        if kind == "hu":
            st.apply_hu(our)
        elif kind == "gang":
            t = TILE_INDEX[action["tile"]]
            st.apply_gang(our, t, "an" if st.counts[our][t] == 4 else "bu")
        else:
            st.apply_discard(our, TILE_INDEX[action["tile"]])
    except (IllegalActionError, KeyError):
        hand = snap["my_hand"]
        fallback = snap["drawn_tile"] if snap["drawn_tile"] in hand else hand[-1]
        action = {"action": "discard", "tile": fallback}
        st.apply_discard(our, TILE_INDEX[fallback])
    tracker.observe(snap, action)


def _apply_our_resp(st, our):
    snap = st.to_snapshot(our)
    action = bot.choose_action(snap, None) or {"action": "pass", "tile": ""}
    kind = action.get("action")
    try:
        if kind == "peng":
            st.apply_claim(our, "peng")
        elif kind == "gang":
            st.apply_claim(our, "gang")
        elif kind == "chi":
            st.apply_claim(our, "chi", sorted(TILE_INDEX[t] for t in action.get("tiles") or []))
        else:
            st.apply_pass(our)
    except (IllegalActionError, KeyError):
        st.apply_pass(our)


def rollout(snap, ctx, deal, our, first_tile, seed):
    """从决策点出发：我方先打 ``first_tile``，之后每一步都是生产；对手 slim。返回我方价值（得分增量 + 连庄价值）。"""
    st = decide.slim_from_snapshot(snap, deal, our, ctx)
    st.enable_tracking(snap.get("discards"), snap.get("melds"), snap.get("last_discard") or "")
    before = st.scores[our]
    rng = random.Random(seed)
    p = _W["p"]
    tp = p.get("topup")
    base = st.drawn_count // 4 if st.drawn_count and not any(st.draws) else 0
    tracker = ChainTracker()
    tracker.round_no = snap.get("round_no")
    tracker.piao = int((ctx or {}).get("piao_count") or 0)
    tracker.has_gang = bool((ctx or {}).get("chain_has_gang"))
    st.apply_discard(our, TILE_INDEX[first_tile])
    tracker.observe(snap, {"action": "discard", "tile": first_tile})
    steps = 0
    while not st.finished and steps < 3000:
        steps += 1
        if st.phase == DEAL:
            seat = st.turn
            if st.needs_draw():
                st.step_draw()
                if st.finished:
                    break
            if seat == our:
                _apply_our_draw(st, our, tracker)
            else:
                _draw_action(st, seat, rng, p, "R_A", tp, base)
        elif st.phase == RESP:
            for seat in list(st.responding):
                if st.phase != RESP or seat not in st.responding:
                    continue
                if seat == our:
                    _apply_our_resp(st, our)
                else:
                    _resp_action(st, seat, rng, p, "R_A")
        else:
            break
    v = st.scores[our] - before
    if not st.is_draw and st.winner == our:
        v += decide._dealer_value(st.round_no)
    return v


def _mean_se(v):
    n = len(v)
    if n < 2:
        return (sum(v) / n if n else 0.0), float("inf")
    m = sum(v) / n
    return m, (sum((x - m) ** 2 for x in v) / (n - 1) / n) ** 0.5


def evaluate_state(task):
    st, k, n_sel, n_test, z = task
    snap, ctx, sid = st["snapshot"], st["ctx"], st["id"]
    try:
        prod = bot._choose_action_production(snap)
        if not prod or prod.get("action") != "discard":
            return {"id": sid, "skip": "production_not_discard"}
        cs = C.production_candidates(snap, prod_tile=prod["tile"], k=k)
        if len(cs) < 2:
            return {"id": sid, "skip": "single_candidate"}
        xs = C.candidate_features(snap, cs, ctx)
        our = snap["seat"]
        seed = int(hashlib.md5(sid.encode()).hexdigest()[:8], 16)
        t0 = time.time()
        deals = determinize_batch(snap, n_sel, seed)
        sel = [[rollout(snap, ctx, d, our, c["tile"], seed ^ (i * 7919 + 1)) for i, d in enumerate(deals)] for c in cs]
        means = [sum(v) / len(v) for v in sel]
        alt = max(range(1, len(cs)), key=lambda i: means[i])
        deals2 = determinize_batch(snap, n_test, seed + 1)
        vp = []
        vc = [[] for _ in cs]
        for i, d in enumerate(deals2):
            s2 = seed ^ (i * 104729 + 3)
            vp.append(rollout(snap, ctx, d, our, cs[0]["tile"], s2))
            for j in range(1, len(cs)):
                vc[j].append(rollout(snap, ctx, d, our, cs[j]["tile"], s2))
        # 检验批上每个候选相对生产的优势（配对差，单位：分/局）与标准误——独立于选择批，无选择偏差；adv[0]=0 是生产自己
        adv, adv_se = [0.0], [0.0]
        for j in range(1, len(cs)):
            m, se_j = _mean_se([a - b for a, b in zip(vc[j], vp)])
            adv.append(m)
            adv_se.append(se_j)
        dm, se = adv[alt], adv_se[alt]
        improved = dm > 0 and dm > z * se
        return {"id": sid, "cls": st["cls"], "prod": prod["tile"], "cands": [c["tile"] for c in cs],
                "x": [[round(v, 5) for v in x] for x in xs], "y": alt if improved else 0, "improved": bool(improved),
                "adv": [round(a, 4) for a in adv], "adv_se": [round(a, 4) if a != float("inf") else 99.0 for a in adv_se],
                "alt": alt, "diff": round(dm, 4), "se": round(se, 4), "sel_means": [round(m, 3) for m in means],
                "n_sel": n_sel, "n_test": n_test, "base": round(sum(vp) / len(vp), 3), "secs": round(time.time() - t0, 2)}
    except Exception as exc:   # noqa: BLE001
        return {"id": sid, "error": repr(exc)}


def load_states(limit=None):
    out = []
    if not os.path.exists(STATES):
        return out
    with open(STATES, encoding="utf-8") as f:
        for line in f:
            out.append(json.loads(line))
            if limit and len(out) >= limit:
                break
    return out


def cmd_run(args):
    states = load_states(args.limit)
    if not states:
        print("没有状态文件 %s（先跑 collect）" % STATES)
        sys.exit(2)
    done = set()
    if os.path.exists(RESULTS) and not args.no_cache:
        with open(RESULTS, encoding="utf-8") as f:
            for line in f:
                try:
                    done.add(json.loads(line)["id"])
                except (ValueError, KeyError):
                    pass
    todo = [s for s in states if s["id"] not in done]
    started = time.time()
    deadline = started + args.max_minutes * 60 if args.max_minutes else None
    tasks = [(s, args.k, args.n_sel, args.n_test, args.z) for s in todo]
    stats = Counter()
    secs = []
    pool = Pool(args.jobs, initializer=_init, initargs=(args.opp_params,)) if args.jobs > 1 else None
    if not pool:
        _init(args.opp_params)
    results = pool.imap_unordered(evaluate_state, tasks, chunksize=1) if pool else map(evaluate_state, tasks)
    stopped = False
    try:
        with open(RESULTS, "a", encoding="utf-8") as out:
            for r in results:
                if "error" in r:
                    stats["error"] += 1
                    print("  出错 %s：%s" % (r["id"], r["error"][:120]))
                elif "skip" in r:
                    stats["skip_" + r["skip"]] += 1
                else:
                    stats["n"] += 1
                    stats["improved"] += r["improved"]
                    stats["cls_" + r["cls"]] += 1
                    stats["cls_improved_" + r["cls"]] += r["improved"]
                    secs.append(r["secs"])
                    out.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
                    out.flush()
                if deadline and time.time() > deadline:
                    stopped = True
                    break
    finally:
        if pool:
            pool.terminate()
            pool.join()
    print("=== search_s3 run（%.0fs；K=%d n_sel=%d n_test=%d z=%.1f） ===" % (time.time() - started, args.k, args.n_sel,
                                                                        args.n_test, args.z))
    print("状态 %d（已有 %d，本次待算 %d）%s" % (len(states), len(done), len(todo), "；到 --max-minutes 提前停，重跑续跑" if stopped else ""))
    n = stats["n"]
    print("本次完成 %d：改进 %d（%.1f%%）；按类别 %s；跳过 %s；出错 %d" % (
        n, stats["improved"], 100.0 * stats["improved"] / max(1, n),
        {c: "%d/%d" % (stats["cls_improved_" + c], stats["cls_" + c]) for c, _ in CLASS_SHARE},
        {k[5:]: v for k, v in stats.items() if k.startswith("skip_")} or "无", stats["error"]))
    if secs:
        secs.sort()
        print("单状态耗时（单核）p50 %.1fs p90 %.1fs；输出 %s" % (secs[len(secs) // 2], secs[int(len(secs) * 0.9)], RESULTS))


# ---------------------------------------------------------------- 测速

def _bench_one(task):
    st, seed = task
    snap, ctx = st["snapshot"], st["ctx"]
    prod = bot._choose_action_production(snap)
    if not prod or prod.get("action") != "discard":
        return None
    deal = determinize_batch(snap, 1, seed)[0]
    t = time.perf_counter()
    rollout(snap, ctx, deal, snap["seat"], prod["tile"], seed)
    return time.perf_counter() - t


def cmd_bench(args):
    states = load_states(args.limit or 400)
    if not states:
        print("没有状态文件 %s（先跑 collect --n 200）" % STATES)
        sys.exit(2)
    started = time.time()
    times = []
    tasks = [(states[i % len(states)], i) for i in range(100000)]
    pool = Pool(args.jobs, initializer=_init, initargs=(args.opp_params,)) if args.jobs > 1 else None
    if not pool:
        _init(args.opp_params)
    it = pool.imap_unordered(_bench_one, tasks, chunksize=1) if pool else map(_bench_one, tasks)
    try:
        for t in it:
            if t is not None:
                times.append(t)
            if time.time() - started > args.seconds:
                break
    finally:
        if pool:
            pool.terminate()
            pool.join()
    elapsed = time.time() - started
    times.sort()
    mean = sum(times) / len(times)
    print("=== search_s3 bench（%d 进程，%.0fs，完成 %d 次推演） ===" % (args.jobs, elapsed, len(times)))
    print("单次推演（我方=生产，对手=slim，打到终局）：均值 %.2fs p50 %.2fs p90 %.2fs p99 %.2fs；整体吞吐 %.2f 次/秒（%d 进程）" % (
        mean, times[len(times) // 2], times[int(len(times) * 0.9)], times[min(len(times) - 1, int(len(times) * 0.99))],
        len(times) / elapsed, args.jobs))
    print("每 1000 个状态所需（核·小时 / 在 N vCPU 上的墙钟小时；--cloud-speed=%.2f 表示云上单核速度相对本机）：" % args.cloud_speed)
    print("%-26s %8s %10s %9s %9s %9s" % ("配置(K,n_sel,n_test)", "推演/状态", "核·小时", "64 vCPU", "128 vCPU", "256 vCPU"))
    for k, ns, nt in ((4, 16, 48), (5, 32, 128), (5, 64, 256)):
        r = k * ns + (k - 1) * nt + nt     # 选择批 K 个候选 + 检验批 P 和其余 K-1 个候选
        core_h = 1000 * r * mean / args.cloud_speed / 3600
        print("%-26s %8d %10.1f %8.2fh %8.2fh %8.2fh" % ("K=%d n_sel=%d n_test=%d" % (k, ns, nt), r, core_h,
                                                        core_h / 64, core_h / 128, core_h / 256))
    print("（另有每状态的生产候选/特征提取开销约 10~100ms，相对推演可忽略；估算按本次测得的均值推演耗时线性外推）")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    pc = sub.add_parser("collect")
    pc.add_argument("--n", type=int, default=200)
    pc.add_argument("--keep", type=float, default=0.15)
    pc.add_argument("--per-file", type=int, default=4)
    pc.add_argument("--seed", type=int, default=0)
    pc.add_argument("--max-minutes", type=float, default=None)
    pc.set_defaults(func=cmd_collect)
    for name, fn in (("run", cmd_run), ("bench", cmd_bench)):
        p = sub.add_parser(name)
        p.add_argument("--jobs", type=int, default=int(os.environ.get("MJ_JOBS", "6")))
        p.add_argument("--limit", type=int, default=None)
        p.add_argument("--opp-params", default=None)
        p.set_defaults(func=fn)
        if name == "run":
            p.add_argument("--k", type=int, default=C.K_DEFAULT)
            p.add_argument("--n-sel", type=int, default=32)
            p.add_argument("--n-test", type=int, default=128)
            p.add_argument("--z", type=float, default=2.0)
            p.add_argument("--max-minutes", type=float, default=None)
            p.add_argument("--no-cache", action="store_true")
        else:
            p.add_argument("--seconds", type=float, default=60.0)
            p.add_argument("--cloud-speed", type=float, default=0.7)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
