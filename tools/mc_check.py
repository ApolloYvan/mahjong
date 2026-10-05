"""阶段二 §2/§4/§6 的验收工具：正确性、校准、测速，三个长任务子命令。

    python3 tools/mc_check.py equiv --limit 2000       # 冒烟，<1 分钟
    python3 tools/mc_check.py equiv                      # 全量 100 万手，交给用户执行
    python3 tools/mc_check.py calibrate                  # slim 对手：拟合策略参数 + 胡牌率补足表，依赖 reports/sim_calibrate.json
    python3 tools/mc_check.py speed --seconds 20          # 冒烟；--seconds 更大交给用户执行
    taskpolicy -c background python3 tools/mc_check.py speed --seconds 60   # 验收用这个（能效核，见下）

2026-10-01 按阶段一实测调整：能效核比性能核慢 3.8 倍，速度验收按能效核算
（比赛机器不保证分到性能核），出牌/胡飘预算上限从 1.5s 收紧到 1.0s（见
``mj/mc/decide.py::DEFAULT_CONFIG`` 的注释）。不加 ``taskpolicy`` 直接跑
``speed`` 测的是性能核，数字会比真实验收线好看，别拿那个数字当达标依据。

======================================================================
``equiv`` 的范围（如实标注跟任务书字面的差异）
======================================================================
任务书原话有两项：
1. "fast.py 与 mj.rules、mj.shanten 在 100 万手随机牌上逐一比对，不一致=0"
   ——这项适用，本工具做（见 ``cmd_equiv``）。
2. "随机生成 1 万局完整对局，推演状态和 RoundEngine 逐局比对胜者/番数/得分"
   ——**不适用**：``mj/mc/rollout.py`` 的 ``RolloutState`` 直接就是
   ``mj.sim.engine.RoundEngine``（见该文件模块 docstring 里"设计取舍"一段：
   没有另外写一套"计数数组"状态机），推演本身跟 RoundEngine 是同一份代码，
   这项比对在这个架构下是重言式，测不出任何东西，本工具不做假的对比、
   如实说明为什么跳过（终端汇总里会打印这一条）。
"""
import argparse
import json
import os
import random
import sys
import time
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ.setdefault("MJ_WEIGHTS_NO_FILE", "1")

from mj.mc import decide  # noqa: E402
from mj.mc.fast import hu_distance, is_hu  # noqa: E402
from mj.mc.rollout import GreedyPolicy  # noqa: E402
from mj.rules import _wins_any  # noqa: E402
from mj.shanten import pair_shanten, shanten  # noqa: E402
from mj.tiles import ALL_TILES, to_counts  # noqa: E402

REPORT_DIR = os.path.join(ROOT, "reports")


def _random_hand(rng, n):
    pool = []
    for t in ALL_TILES:
        pool.extend([t] * 4)
    rng.shuffle(pool)
    return pool[:n]


def cmd_equiv(args):
    rng = random.Random(0)
    n = args.limit or 1_000_000
    mismatches = []
    started = time.time()
    for i in range(n):
        meld_groups = rng.randint(0, 4)
        size13 = 13 - 3 * meld_groups
        if size13 < 0:
            continue
        hand13 = _random_hand(rng, size13)
        counts13 = to_counts(hand13)
        expected_dist = shanten(counts13, meld_groups)
        if meld_groups == 0:
            expected_dist = min(expected_dist, pair_shanten(counts13))
        got_dist = hu_distance(counts13, meld_groups)
        if got_dist != expected_dist:
            mismatches.append(("hu_distance", i, meld_groups, hand13, expected_dist, got_dist))

        hand14 = _random_hand(rng, size13 + 1)
        counts14 = to_counts(hand14)
        expected_hu = bool(_wins_any(counts14, meld_groups))
        got_hu = is_hu(counts14, meld_groups)
        if got_hu != expected_hu:
            mismatches.append(("is_hu", i, meld_groups, hand14, expected_hu, got_hu))
    elapsed = time.time() - started

    os.makedirs(REPORT_DIR, exist_ok=True)
    out_path = os.path.join(REPORT_DIR, "mc_check_equiv.tsv")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("check\ti\tmeld_groups\thand\texpected\tgot\n")
        for row in mismatches:
            f.write("\t".join(str(x) for x in row) + "\n")

    print("=== mc_check equiv（完整明细见 %s） ===" % out_path)
    print("样本数 %d，耗时 %.1fs，不一致 %d（验收线 0）" % (n, elapsed, len(mismatches)))
    print("跳过项：'推演状态 vs RoundEngine 逐局比对'——rollout.py 直接用 RoundEngine 本身，"
          "同一份代码跟自己比是重言式，见模块 docstring")


CAL_CHECKPOINTS = (6, 10, 14)
CURVE_KS = tuple(range(2, 21, 2))       # sim_calibrate.json 里累计胡牌曲线的检查点（按自己第几摸）
TOPUP_LEN = 21                          # 补足表按自己第几摸下标（0 不用，1..20）
FAN_BUCKETS = ((1, "1"), (2, "2"), (4, "4"), (8, "8"), (16, "16+"))
PARAMS_PATH = os.path.join(ROOT, "models", "mc_opp_params.json")
SMOKE_PARAMS_PATH = os.path.join(ROOT, "tools", ".cache", "mc_opp_params_smoke.json")   # --quick 的产物，只给 mc_offline --params 冒烟用
SEED_BASE, SEED_REFINE, SEED_VERIFY = 777000, 800000, 990000


def _sim_chunk(job):
    """跑 [seed0, seed0+n) 局 slim 四家自对弈，返回可相加的原始计数。``params`` 里有 ``topup`` 就带补足。"""
    from mj.mc.slim import Slim, play_out
    from mj.sim.engine import build_wall
    params, seed0, n = job
    raw = {"n": n, "draws": 0, "claims": [0, 0, 0], "rows": {"d": 0, "n": 0},
           "win_at": {"d": Counter(), "n": Counter()}, "reach": {"d": Counter(), "n": Counter()},
           "fan": {"s": Counter(), "v": Counter()}}
    for seed in range(seed0, seed0 + n):
        st = Slim.from_wall(build_wall(seed), seed % 4, 1, [0, 0, 0, 0])
        play_out(st, random.Random(seed), params=params)
        raw["rows"]["d"] += 1
        raw["rows"]["n"] += 3
        for i in range(3):
            raw["claims"][i] += st.claim_n[i]
        for seat in range(4):
            side = "d" if seat == st.dealer else "n"
            for k in range(1, min(st.draws[seat], 20) + 1):
                raw["reach"][side][k] += 1       # 该座位摸到过第 k 张（局在它第 k 摸时还没结束）
        if st.is_draw:
            raw["draws"] += 1
        else:
            side = "d" if st.winner == st.dealer else "n"
            raw["win_at"][side][st.draws[st.winner]] += 1
            raw["fan"]["v" if st.virtual else "s"][min(st.fan, 16)] += 1
    return raw


def _merge_raw(raws):
    out = {"n": 0, "draws": 0, "claims": [0, 0, 0], "rows": {"d": 0, "n": 0},
           "win_at": {"d": Counter(), "n": Counter()}, "reach": {"d": Counter(), "n": Counter()},
           "fan": {"s": Counter(), "v": Counter()}}
    for r in raws:
        out["n"] += r["n"]
        out["draws"] += r["draws"]
        for i in range(3):
            out["claims"][i] += r["claims"][i]
        for side in "dn":
            out["rows"][side] += r["rows"][side]
            out["win_at"][side].update(r["win_at"][side])
            out["reach"][side].update(r["reach"][side])
        for k in "sv":
            out["fan"][k].update(r["fan"][k])
    return out


def _stats(raw):
    """原始计数 -> 校准口径（跟 sim_calibrate.json 同口径：分母=全部座位·局，庄/闲分开）。"""
    n = raw["n"]
    wins = sum(raw["fan"]["s"].values()) + sum(raw["fan"]["v"].values())
    fan2 = sum(c for f, c in list(raw["fan"]["s"].items()) + list(raw["fan"]["v"].items()) if f >= 2)
    cum, reach = {}, {}
    for side in "dn":
        tot = 0
        cur, rc = {}, [0.0] * TOPUP_LEN
        for k in range(0, 21):
            if k:
                # 还没胡过的座位里，局在第 k 摸时仍在继续的比例（unconditional hazard = 它 x 条件 hazard）
                alive = raw["rows"][side] - tot
                rc[k] = raw["reach"][side].get(k, 0) / alive if alive > 0 else 0.0
            tot += raw["win_at"][side].get(k, 0)
            cur[k] = tot / raw["rows"][side] if raw["rows"][side] else 0.0
        cum[side], reach[side] = cur, rc
    return {
        "draw_rate": raw["draws"] / n, "fan2plus": fan2 / wins if wins else 0.0,
        "chi": raw["claims"][0] / n, "peng": raw["claims"][1] / n, "gang": raw["claims"][2] / n,
        "cum_dealer": {k: cum["d"][k] for k in range(21)}, "cum_nondealer": {k: cum["n"][k] for k in range(21)},
        "reach": reach, "wins": wins, "virtual_share": sum(raw["fan"]["v"].values()) / wins if wins else 0.0,
        "fan_strategy": dict(raw["fan"]["s"]), "fan_virtual": dict(raw["fan"]["v"]),
    }


def slim_stats(params, n, seed0=SEED_BASE):
    """单进程：自对弈 n 局的校准口径统计（冒烟/测试用）。"""
    return _stats(_sim_chunk((params, seed0, n)))


def _run_sim(pool, params, n, seed0, chunk=250):
    jobs = [(params, seed0 + i, min(chunk, n - i)) for i in range(0, n, chunk)]
    return _stats(_merge_raw(pool.map(_sim_chunk, jobs, chunksize=1)))


def _g(d, k):
    v = d.get(str(k))
    if v is None:
        v = d.get(k)
    return v[0] if v is not None else 0.0


def _cal_targets(masters):
    fd = masters["fan_distribution"]
    return {
        "draw_rate": masters["draw_rate"][0],
        "fan2plus": 1.0 - _g(fd, "1"),
        "chi": masters["claims_per_round"]["chi"][0], "peng": masters["claims_per_round"]["peng"][0],
        "gang": masters["claims_per_round"]["gang"][0],
        "cum_dealer": {k: (_g(masters["cumulative_hu_curve_dealer"], k) if k else 0.0) for k in range(21)
                       if k == 0 or k in CURVE_KS},
        "cum_nondealer": {k: (_g(masters["cumulative_hu_curve_nondealer"], k) if k else 0.0) for k in range(21)
                          if k == 0 or k in CURVE_KS},
        "fan_dist": {f: _g(fd, key) for f, key in FAN_BUCKETS},
    }


def _bucket_hazards(cum):
    """累计曲线（只在偶数摸牌数有值）-> 逐摸的 hazard 表（长度 TOPUP_LEN，下标=自己第几摸）。
    每个两摸桶 (k-2,k] 内 r=(F(k)-F(k-2))/(1-F(k-2)) 是两摸合计的条件胡牌率，折成每摸 1-(1-r)^0.5。"""
    row = [0.0] * TOPUP_LEN
    for k in CURVE_KS:
        prev = cum[k - 2] if k - 2 in cum else 0.0
        r = (cum[k] - prev) / (1.0 - prev) if prev < 1.0 else 0.0
        h = 1.0 - (1.0 - max(0.0, min(1.0, r))) ** 0.5
        row[k - 1] = row[k] = h
    return row


def _feasible_curves(tg):
    """高手曲线是"高手座位"的，四家同时按它走会出现 庄+3×闲 >1（不可能）。拟合用的目标曲线在每个检查点对庄/闲
    同时下移 g_k（让最终 庄+3×闲 @20摸 = 1-真实流局率，庄闲偏差相同即最大偏差最小；g_k 按累计量比例渐进，
    早期几乎不动）。只用于拟合/挑表；最终验收仍然拿原始高手曲线比 <=3pp。返回 (庄, 闲, g@20)。"""
    cap = 1.0 - tg["draw_rate"]
    d, n = dict(tg["cum_dealer"]), dict(tg["cum_nondealer"])
    s20 = d[20] + 3 * n[20]
    g20 = max(0.0, (s20 - cap) / 4.0)
    if g20 <= 0:
        return d, n, 0.0
    for k in CURVE_KS:
        g = g20 * (tg["cum_dealer"][k] + 3 * tg["cum_nondealer"][k]) / s20
        d[k], n[k] = tg["cum_dealer"][k] - g, tg["cum_nondealer"][k] - g
    return d, n, g20


def _strategy_loss(st, tg):
    """第一步（策略参数）的目标：流局率、吃碰次数尽量接近真实；胡牌曲线不在这里拟合（由补足表负责）。"""
    loss = (st["draw_rate"] - tg["draw_rate"]) ** 2
    for k in ("chi", "peng"):
        loss += ((st[k] - tg[k]) / 10.0) ** 2   # 次数/10 ≈ 百分点量级
    return loss


def _cal_one(job):
    params, n = job
    return params, slim_stats(params, n)


def _fan_cum(dist):
    tot = sum(dist.values()) or 1.0
    acc, out = 0.0, []
    for f, _ in FAN_BUCKETS:
        acc += dist.get(f, 0.0) / tot
        out.append([f, acc])
    out[-1][1] = 1.0
    return out


def _compensated_fan(target, st):
    """虚拟自摸的番数分布 = 高手分布，但要扣掉策略自己胡出来的那部分偏差，
    使"策略胡 + 虚拟胡"合起来的番数分布贴近高手：q = (T - w_s*D_s) / w_v，截到 >=0 再归一。"""
    s = st["fan_strategy"]
    ns, nv = sum(s.values()), sum(st["fan_virtual"].values())
    if ns + nv == 0 or nv == 0:
        return dict(target)
    ws = ns / (ns + nv)
    q = {}
    for f, _ in FAN_BUCKETS:
        ds = s.get(f, 0) / ns if ns else 0.0
        q[f] = max(0.0, (target[f] - ws * ds) / (1.0 - ws))
    tot = sum(q.values())
    return {f: v / tot for f, v in q.items()} if tot > 0 else dict(target)


def _topup_table(p_d, p_n, fan_q):
    return {"p": {"d": [round(x, 5) for x in p_d], "n": [round(x, 5) for x in p_n]},
            "fan": {"d": _fan_cum(fan_q), "n": _fan_cum(fan_q)}}


def _within(st, tg):
    ok = {}
    for cp in CAL_CHECKPOINTS:
        ok["cum_dealer@%d" % cp] = abs(st["cum_dealer"][cp] - tg["cum_dealer"][cp]) <= 0.03
        ok["cum_nondealer@%d" % cp] = abs(st["cum_nondealer"][cp] - tg["cum_nondealer"][cp]) <= 0.03
    ok["fan2plus"] = abs(st["fan2plus"] - tg["fan2plus"]) <= 0.03
    return ok


def cmd_calibrate(args):
    import itertools
    from multiprocessing import Pool
    src = os.path.join(REPORT_DIR, "sim_calibrate.json")
    if not os.path.exists(src):
        print("找不到 %s——先跑 tools/sim_calibrate.py（阶段一 §3），拟合标靶依赖它。" % src)
        sys.exit(2)
    with open(src, encoding="utf-8") as f:
        masters = json.load(f).get("高手")
    if not masters:
        print("sim_calibrate.json 里没有'高手'分组，无法拟合。")
        sys.exit(2)
    tg = _cal_targets(masters)
    hz = args.hazard_rounds
    if args.quick:
        axes = {"p_peng": (0.5, 0.9), "p_chi": (0.5,), "p_flat": (0.3,), "noise": (0.0,), "piao_prob": (0.5,)}
    else:
        axes = {"p_peng": (0.5, 0.7, 0.9), "p_chi": (0.3, 0.5, 0.7), "p_flat": (0.1, 0.3, 0.5),
                "noise": (0.0, 0.3), "piao_prob": (0.3, 0.6)}
    grid = [dict(zip(axes, vals)) for vals in itertools.product(*axes.values())]
    started = time.time()
    with Pool(args.jobs) as pool:
        # ① 策略参数：不补足，目标=流局率、吃碰次数
        res = pool.map(_cal_one, [(g, args.rounds) for g in grid], chunksize=1)
        res.sort(key=lambda r: _strategy_loss(r[1], tg))
        params, st0 = res[0]
        # ② h_策略：选定参数、不补足的推演里实测；与高手 hazard 一起算初始补足表
        base = _run_sim(pool, params, hz, SEED_BASE)
        fd, fn, fit_scale = _feasible_curves(tg)
        h_m = {"d": _bucket_hazards(fd), "n": _bucket_hazards(fn)}
        h_s = {"d": _bucket_hazards(base["cum_dealer"]), "n": _bucket_hazards(base["cum_nondealer"])}
        p_top = {s: [max(0.0, (h_m[s][k] - h_s[s][k]) / (1.0 - h_s[s][k])) for k in range(TOPUP_LEN)] for s in "dn"}
        fan_q = dict(tg["fan_dist"])
        history, best = [], None
        # ③ 补足后实测（refine+1 轮，每轮新种子）：逐桶用（高手 hazard - 实测 hazard）修正补足表，番数分布按实测的
        #    策略/虚拟占比修正（带阻尼）；挑这几轮里最大偏差最小的一张表，最终验收再用一批没参与修正的种子
        for it in range(args.refine + 3):
            table = _topup_table(p_top["d"], p_top["n"], fan_q)
            st = _run_sim(pool, {**params, "topup": table}, hz, SEED_REFINE + it * 100000)
            gap = max([abs(st["cum_dealer"][cp] - fd[cp]) for cp in CAL_CHECKPOINTS] + [abs(st["cum_nondealer"][cp] - fn[cp]) for cp in CAL_CHECKPOINTS]
                      + [abs(st["fan2plus"] - tg["fan2plus"])])
            history.append({"iter": it, "max_gap": round(gap, 4), "fan2plus": st["fan2plus"],
                            "virtual_share": st["virtual_share"]})
            if best is None or gap < best[0]:
                best = (gap, table)
            if it == args.refine + 2:
                break
            if it < args.refine:         # 前 refine 轮：p 和番数分布一起修正；之后 2 轮冻结 p，只修番数分布（消除滞后）
                h_sim = {"d": _bucket_hazards(st["cum_dealer"]), "n": _bucket_hazards(st["cum_nondealer"])}
                for s in "dn":
                    # 修正量按"局仍在继续的比例"放大：晚摸时多数局已结束，同样的无条件 hazard 差要更大的条件概率
                    p_top[s] = [max(0.0, min(0.9, p_top[s][k] + 0.7 * (h_m[s][k] - h_sim[s][k]) / max(st["reach"][s][k], 0.1)))
                                for k in range(TOPUP_LEN)]
                new_q = _compensated_fan(tg["fan_dist"], st)
                fan_q = {f: 0.5 * fan_q[f] + 0.5 * new_q[f] for f in fan_q}
            else:
                fan_q = _compensated_fan(tg["fan_dist"], st)
        topup = best[1]
        final_params = {**params, "topup": topup}
        # ④ 验收：新种子实测，不按构造视为通过
        st = _run_sim(pool, final_params, hz, SEED_VERIFY)
    ok = _within(st, tg)
    cap = {k: tg["cum_dealer"][k] + 3 * tg["cum_nondealer"][k] for k in CURVE_KS}
    infeasible = [k for k, v in cap.items() if v > 1.0]
    out = {"infeasible_ks": infeasible, "seat_sum_targets": cap, **{"params": final_params, "strategy_params": params, "strategy_fit": st0, "unpadded": base, "achieved": st,
           "targets": tg, "within_3pp": ok, "all_within_3pp": all(ok.values()), "quick": bool(args.quick),
           "rounds_per_grid_point": args.rounds, "hazard_rounds": hz, "refine_history": history,
           "h_master": h_m, "h_strategy": h_s, "fit_scale": fit_scale,
           "note": "胡牌率补足：对手(非我方座位)摸牌后策略没胡，按 (庄/闲, 自己第几摸) 的概率触发虚拟自摸；"
                   "分档只到庄/闲×摸牌序号（sim_calibrate.json 没有按副露数分的高手 hazard，也没有分庄闲的番数分布）"}}
    os.makedirs(REPORT_DIR, exist_ok=True)
    with open(os.path.join(REPORT_DIR, "mc_calibrate.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, default=str)
    dest = SMOKE_PARAMS_PATH if args.quick else PARAMS_PATH
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "w", encoding="utf-8") as f:
        json.dump({"params": final_params, "all_within_3pp": out["all_within_3pp"],
                   "within_3pp": ok, "h_strategy": h_s, "h_master": h_m,
                   "note": "tools/mc_check.py calibrate 产物：策略参数 + 胡牌率补足表(topup)，标靶=reports/sim_calibrate.json 高手组"},
                  f, ensure_ascii=False, indent=1)
    print("=== mc_check calibrate（slim + 胡牌率补足；网格 %d 组 x %d 局，补足测量 %d 局 x (1+%d+3) 轮，%.0fs；明细 reports/mc_calibrate.json） ===" % (
        len(grid), args.rounds, hz, args.refine, time.time() - started))
    print("策略参数：%s%s" % ({k: v for k, v in params.items()},
                          "（--quick 冒烟，写到 %s，不是正式校准文件）" % SMOKE_PARAMS_PATH if args.quick else "  -> 已写 models/mc_opp_params.json"))
    print("补足前策略自己：第6/10/14摸累计 庄 %s 闲 %s；2番+ %.3f；流局 %.4f" % (
        "/".join("%.3f" % base["cum_dealer"][k] for k in (6, 10, 14)),
        "/".join("%.3f" % base["cum_nondealer"][k] for k in (6, 10, 14)), base["fan2plus"], base["draw_rate"]))
    print("补足后最终验收（新种子实测；虚拟自摸占胡 %.1f%%）：" % (100 * st["virtual_share"]))
    print("%-18s %8s %8s %6s" % ("指标", "实测", "目标", "<=3pp"))
    for cp in CAL_CHECKPOINTS:
        for key in ("cum_dealer", "cum_nondealer"):
            print("%-18s %8.3f %8.3f %6s" % ("%s@%d摸" % (key, cp), st[key][cp], tg[key][cp],
                                            "是" if ok["%s@%d" % (key, cp)] else "**否**"))
    print("%-18s %8.3f %8.3f %6s" % ("2番+占胡", st["fan2plus"], tg["fan2plus"], "是" if ok["fan2plus"] else "**否**"))
    print("%-18s %8.4f %8.4f %6s" % ("流局率", st["draw_rate"], tg["draw_rate"], "（报告）"))
    for k in ("chi", "peng", "gang"):
        print("%-18s %8.2f %8.2f %6s" % ("每局%s次数" % k, st[k], tg[k], "（报告；补足让对局更早结束）"))
    if infeasible:
        print("拟合用目标曲线=高手曲线下移（@20摸 庄闲各 -%.1fpp，让庄+3×闲<=1-流局率）；验收仍按原始高手曲线判 <=3pp" % (100 * fit_scale))
        print("注意：第 %s 摸的目标 庄+3×闲 累计胡牌率之和 >1（高手这条曲线是'高手座位'的，四家都按高手水平不可能同时满足），"
              "该处偏差有结构性下限。" % ",".join(map(str, infeasible)))
    print("全部 <=3pp：%s" % ("是（新种子实测）" if out["all_within_3pp"] else "**否**——未达标，mc_offline 的结论要打折扣"))


def cmd_speed_live(args):
    """真实快照上测 ``mc_override`` 的实时耗时分布与回落率（时间模式，cap 1.0s，从调用瞬间计时）。
    快照来自 tools/mc_offline.py 缓存的决策点（真实日志回放出来的，三类：胡飘/有财神出牌/门清出牌），
    三个开关全开。性能核/能效核各跑一次（后者前面加 taskpolicy -c background）。单进程，不抢核。"""
    import glob
    import pickle
    from mj.fit import weights_overlay
    pts_files = sorted(glob.glob(os.path.join(ROOT, "tools", ".cache", "mc_offline", "points_*.pkl")),
                       key=os.path.getmtime)
    if not pts_files:
        print("没有 tools/.cache/mc_offline/points_*.pkl（真实决策点缓存）——先跑 tools/mc_offline.py。")
        sys.exit(2)
    with open(pts_files[-1], "rb") as f:
        points = pickle.load(f)
    points = [p for p in points if p["action"][0] in ("discard", "hu")]
    random.Random(args.seed).shuffle(points)
    if args.params:
        params, ok = decide.load_opp_params(args.params)
        decide._OPP.update(loaded=True, params=params, ok=ok)
    params, ok = decide.load_opp_params()
    if params is None:
        print("拒绝运行：没有校准文件 %s（先跑 mccal；冒烟用 --params tools/.cache/mc_opp_params_smoke.json）。" % PARAMS_PATH)
        sys.exit(2)
    limit = args.seconds if not args.max_minutes else min(args.seconds, args.max_minutes * 60)
    overlay = {"mc_hu_enabled": 1, "mc_discard_enabled": 1, "mc_menqing_enabled": 1}
    times, reasons, n_over, n_called, by_cls = [], Counter(), 0, 0, Counter()
    started = time.time()
    i = 0
    with weights_overlay(overlay):
        while time.time() - started < limit and points:
            pt = points[i % len(points)]
            i += 1
            kind, tile = pt["action"]
            action = {"action": "hu", "tile": pt["snapshot"].get("drawn_tile") or ""} if kind == "hu" else \
                {"action": "discard", "tile": tile}
            snap = dict(pt["snapshot"])
            ctx = {"t0": time.monotonic()}
            decide.mc_override(snap, action, None, ctx)
            log = ctx.get("mc_log")
            if not log:
                continue
            n_called += 1
            times.append((time.monotonic() - ctx["t0"]) * 1000)
            by_cls[log.get("cls")] += 1
            if log.get("overridden"):
                n_over += 1
            else:
                reasons[(log.get("fallback_reason") or "none").split(":")[0]] += 1
    times.sort()

    def pct(p):
        return times[min(len(times) - 1, int(len(times) * p))] if times else 0.0

    keep = reasons.get("not_significant", 0)
    tech = n_called - n_over - keep
    out = {"label": args.label, "n": n_called, "overridden": n_over, "not_significant": keep, "technical_fallback": tech,
           "reasons": dict(reasons), "ms": {"p50": pct(0.5), "p90": pct(0.9), "p99": pct(0.99),
                                           "max": times[-1] if times else 0.0},
           "cap_s": decide.get_config()["cap_discard_hu_s"], "calibrated_all_3pp": ok, "classes": dict(by_cls)}
    os.makedirs(REPORT_DIR, exist_ok=True)
    with open(os.path.join(REPORT_DIR, "mc_speed_live_%s.json" % (args.label or "run")), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("=== mc_check speed-live [%s]（真实快照 %d 个池，%.0fs，cap %.1fs 从调用瞬间计时） ===" % (
        args.label or "run", len(points), time.time() - started, out["cap_s"]))
    if not n_called:
        print("没有任何 MC 评估发生（开关/候选问题？）")
        return
    print("MC 评估 %d 次：推翻生产 %d（%.1f%%），评估后保持生产(不显著) %d（%.1f%%），技术性回落 %d（%.1f%%）" % (
        n_called, n_over, 100.0 * n_over / n_called, keep, 100.0 * keep / n_called, tech, 100.0 * tech / n_called))
    print("  回落原因：%s" % ({k: v for k, v in reasons.items() if k != "not_significant"} or "无"))
    print("  类别：%s" % dict(by_cls))
    print("  单次耗时 ms：p50 %.0f  p90 %.0f  p99 %.0f  max %.0f" % (pct(0.5), pct(0.9), pct(0.99), times[-1]))
    print("  对手参数 all_within_3pp=%s" % ok)


def cmd_speed(args):
    from mj.sim.engine import RoundEngine, build_wall
    from mj.mc.rollout import run_round
    started = time.time()
    rounds_done = 0
    seed = 0
    round_times = []
    while time.time() - started < args.seconds:
        t0 = time.perf_counter()
        engine = RoundEngine(build_wall(seed=seed + 700000), dealer=seed % 4, round_no=1, scores=[0, 0, 0, 0])
        policies = {s: GreedyPolicy(rng=random.Random(seed * 3 + s)) for s in range(4)}
        run_round(engine, policies, max_steps=3000)
        round_times.append(time.perf_counter() - t0)
        rounds_done += 1
        seed += 1
    elapsed = time.time() - started
    round_times.sort()

    def pct(p):
        if not round_times:
            return 0.0
        return round_times[min(len(round_times) - 1, int(len(round_times) * p))]

    print("=== mc_check speed（单核，%.1fs） ===" % elapsed)
    print("完整推演局数 %d，单局耗时 p50=%.1fms p90=%.1fms p99=%.1fms" % (
        rounds_done, pct(0.5) * 1000, pct(0.9) * 1000, pct(0.99) * 1000))
    n_128 = 128
    est_128_ms = pct(0.5) * n_128 * 1000
    budget_ms = 1000   # 2026-10-01 按阶段一实测调整，从 1500ms 收紧到 1000ms，见模块 docstring
    print("按 p50 估算：3 个候选各跑 128 局需要约 %.0fms（预算线 %dms，%s；"
          "没加 taskpolicy 的话这是性能核的数字，验收要看能效核那次）" % (
        est_128_ms * 3, budget_ms, "达标" if est_128_ms * 3 <= budget_ms else "**不达标**，需要优化或降低 mc_fixed_n"))


COVER = Counter()   # lockstep 覆盖统计：确认等价性测试真的走到了各条规则分支


def _state_sig_engine(e):
    from mj.tiles import TILE_INDEX
    counts = []
    for h in e.hands:
        c = [0] * 34
        for t in h:
            c[TILE_INDEX[t]] += 1
        counts.append(c)
    return (e.finished, e.is_draw, e.winner, e.fan, tuple(e.detail or ()), e.phase if e.phase != "finished" else 2,
            e.turn, e.wall_remaining(), e.chain_count, e.chain_owner, e.catch_play, e.god_discarder_seat,
            tuple(e.scores), tuple(sorted(e.responding_seats)), e.response_stage, tuple(map(tuple, counts)),
            tuple(e.piao_count))


def _state_sig_slim(s):
    return (s.finished, s.is_draw, s.winner, s.fan, tuple(s.detail or ()),
            {0: "draw", 1: "response", 2: 2}[s.phase] if s.phase != 2 else 2,
            s.turn, s.wall_remaining(), s.chain_count, s.chain_owner, s.catch_play, s.god_discarder_seat,
            tuple(s.scores), tuple(sorted(s.responding)), s.response_stage, tuple(map(tuple, s.counts)),
            tuple(s.piao_count))


def _lockstep_round(seed):
    """同一串随机动作同时喂给 RoundEngine 和 Slim，每一步比较状态签名。返回首个不一致描述或 None。"""
    from mj.sim.engine import GANG_FORBIDDEN_REMAINING, CHAIN_CAP, IllegalActionError, RoundEngine, build_wall
    from mj.mc.slim import Slim
    from mj.tiles import JOKER, TILE_INDEX
    rng = random.Random(seed)
    wall = build_wall(seed)
    e = RoundEngine(wall, seed % 4, 1 + seed % 8, [0, 0, 0, 0])
    s = Slim.from_engine(e)

    def check(tag):
        a, b = _state_sig_engine(e), _state_sig_slim(s)
        if a != b:
            names = ("finished", "is_draw", "winner", "fan", "detail", "phase", "turn", "wall", "chain_count",
                     "chain_owner", "catch_play", "god_seat", "scores", "responding", "stage", "counts", "piao")
            diff = [n for n, x, y in zip(names, a, b) if x != y]
            return "seed=%d after %s: %s" % (seed, tag, ",".join(diff))
        # 快照输出：Slim.to_snapshot 与 build_snapshot 逐座位逐字段一致（my_hand 只比多重集：计数数组没有顺序）
        from mj.sim.snapshot import build_snapshot
        for seat in range(4):
            sa, sb = build_snapshot(e, seat), s.to_snapshot(seat)
            ha, hb = sorted(sa.pop("my_hand")), sorted(sb.pop("my_hand"))
            if sa != sb or ha != hb:
                keys = [k for k in sa if sa[k] != sb.get(k)] + (["my_hand"] if ha != hb else [])
                return "seed=%d after %s: snapshot seat %d fields %s" % (seed, tag, seat, keys)
        return None

    for step in range(3000):
        if e.finished:
            break
        if e.phase == "draw":
            seat = e.turn
            if e.needs_draw():
                e.step_draw()
                s.step_draw()
                bad = check("draw")
                if bad or e.finished:
                    return bad
            res = e.can_self_draw_hu()
            if res and rng.random() < 0.85:
                e.apply_hu(seat)
                s.apply_hu(seat)
                COVER['hu_%s' % ('chain' if e.chain_count else 'plain')] += 1
                return check("hu")
            hand = e.hands[seat]
            if e.wall_remaining() > GANG_FORBIDDEN_REMAINING and rng.random() < 0.5:
                opts = [(t, "an") for t in set(hand) if t != JOKER and hand.count(t) == 4]
                opts += [(m["tiles"][0], "bu") for m in e.melds[seat] if m["kind"] == "peng" and m["tiles"][0] in hand]
                if opts:
                    t, kind = rng.choice(opts)
                    COVER['gang_' + kind] += 1
                    e.apply_gang(seat, t, kind)
                    s.apply_gang(seat, TILE_INDEX[t], kind)
                    bad = check("gang")
                    if bad:
                        return bad
                    continue
            restricted = e.catch_play and seat != e.god_discarder_seat
            if restricted:
                cands = [e.drawn_tile] if e.drawn_tile in hand else []
            else:
                cands = sorted(set(hand))
            free_ok = (not e.catch_play) or seat == e.god_discarder_seat
            if e.chain_count >= CHAIN_CAP and free_ok:
                cands = [t for t in cands if t != JOKER]
            if not cands:
                return "seed=%d: 无合法弃牌" % seed
            if JOKER in cands and rng.random() < (0.3 if seed % 2 else 0.1):
                t = JOKER
            elif rng.random() < 0.7 and len(cands) > 1:
                # 多数时候按启发式弃牌，让手牌真的收敛、走到胡牌/结算分支（纯随机弃牌 1000 局只胡 9 次）
                from mj.mc.slim import _pick_discard
                from mj.tiles import INDEX_TILE
                pick_idx = _pick_discard(s.counts[seat], False, rng, 0.5)
                t = INDEX_TILE[pick_idx] if INDEX_TILE[pick_idx] in cands else rng.choice(cands)
            else:
                t = rng.choice(cands)
            COVER['discard_joker' if t == JOKER else 'discard'] += 1
            COVER['catch_play_discard'] += 1 if e.catch_play else 0
            e.apply_discard(seat, t)
            s.apply_discard(seat, TILE_INDEX[t])
            bad = check("discard")
            if bad:
                return bad
        elif e.phase == "response":
            for seat in list(e.responding_seats):
                if e.phase != "response" or seat not in e.responding_seats:
                    continue
                legal = e.legal_responses(seat)
                pick = "pass" if rng.random() < 0.5 or len(legal) == 1 else rng.choice([x for x in legal if x != "pass"])
                COVER['resp_' + pick] += 1
                if pick == "pass":
                    e.apply_pass(seat)
                    s.apply_pass(seat)
                elif pick == "chi":
                    opt = rng.choice(e._chi_options(seat, e.window_tile))
                    e.apply_claim(seat, "chi", list(opt))
                    s.apply_claim(seat, "chi", [TILE_INDEX[x] for x in opt])
                else:
                    e.apply_claim(seat, pick, None)
                    s.apply_claim(seat, pick, None)
                bad = check("response:" + pick)
                if bad:
                    return bad
                if e.finished:
                    break
        else:
            break
    return check("end") if e.finished else "seed=%d: 未结束" % seed


def cmd_slim_equiv(args):
    n = args.limit or 10000
    bad = []
    started = time.time()
    for seed in range(args.seed, args.seed + n):
        try:
            r = _lockstep_round(seed)
        except Exception as exc:   # noqa: BLE001
            r = "seed=%d: 异常 %r" % (seed, exc)
        if r:
            bad.append(r)
    os.makedirs(REPORT_DIR, exist_ok=True)
    path = os.path.join(REPORT_DIR, "mc_slim_equiv.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(bad) + ("\n" if bad else ""))
    print("=== mc_check slim-equiv（明细 %s） ===" % path)
    print("对照局数 %d，不一致 %d（验收线 0），耗时 %.1fs" % (n, len(bad), time.time() - started))
    print("分支覆盖：%s" % dict(sorted(COVER.items())))
    for r in bad[:10]:
        print("  " + r)


def cmd_slim_speed(args):
    from mj.mc.slim import Slim, play_out
    from mj.sim.engine import build_wall
    started = time.time()
    times, seed = [], 0
    while time.time() - started < args.seconds:
        t0 = time.perf_counter()
        st = Slim.from_wall(build_wall(seed + 500000), seed % 4, 1, [0, 0, 0, 0])
        play_out(st, random.Random(seed))
        times.append(time.perf_counter() - t0)
        seed += 1
    times.sort()
    pick = lambda p: times[min(len(times) - 1, int(len(times) * p))] * 1000   # noqa: E731
    print("=== mc_check slim-speed（单核，%.1fs） ===" % (time.time() - started))
    print("局数 %d，单局 p50=%.2fms p90=%.2fms p99=%.2fms（目标 <=5ms，性能核；"
          "能效核用 taskpolicy -c background 再跑一次）" % (len(times), pick(.5), pick(.9), pick(.99)))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_equiv = sub.add_parser("equiv")
    p_equiv.add_argument("--limit", type=int, default=None)
    p_equiv.set_defaults(func=cmd_equiv)

    p_cal = sub.add_parser("calibrate")
    p_cal.add_argument("--rounds", type=int, default=1500, help="每组参数自对弈局数")
    p_cal.add_argument("--hazard-rounds", type=int, default=30000, help="实测 hazard/补足验证每轮的局数（庄座位只占 1/4，噪声下限约 0.6pp）")
    p_cal.add_argument("--refine", type=int, default=6, help="补足表修正轮数（之后再有 3 轮冻结 p 只修番数分布，再 1 轮新种子验收）")
    p_cal.add_argument("--jobs", type=int, default=6)
    p_cal.add_argument("--quick", action="store_true", help="冒烟：小网格，不写 models/mc_opp_params.json")
    p_cal.set_defaults(func=cmd_calibrate)

    p_speed = sub.add_parser("speed")
    p_speed.add_argument("--seconds", type=float, default=20.0)
    p_speed.set_defaults(func=cmd_speed)

    p_se = sub.add_parser("slim-equiv")
    p_se.add_argument("--limit", type=int, default=None)
    p_se.add_argument("--seed", type=int, default=0)
    p_se.set_defaults(func=cmd_slim_equiv)

    p_sl = sub.add_parser("speed-live")
    p_sl.add_argument("--seconds", type=float, default=60.0)
    p_sl.add_argument("--max-minutes", type=float, default=None)
    p_sl.add_argument("--label", default="perf", help="结果文件标签（perf/eff）")
    p_sl.add_argument("--params", default=None, help="校准参数文件（冒烟用）")
    p_sl.add_argument("--seed", type=int, default=0)
    p_sl.set_defaults(func=cmd_speed_live)

    p_ss = sub.add_parser("slim-speed")
    p_ss.add_argument("--seconds", type=float, default=10.0)
    p_ss.set_defaults(func=cmd_slim_speed)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
