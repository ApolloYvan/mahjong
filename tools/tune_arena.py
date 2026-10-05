"""黑盒联合调参：以 arena2 同牌 1v3 A/B 为目标，逐代搜索 ~39 个出牌/爆头/路线权重。

为什么这样设计（2026-10-04）：单项消融全部无效，说明单个开关已在局部最优；联合调参是唯一还没试的手段。
目标噪声大（每种子 A−B 标准差 ~1.7 分/局），所以：
- 同牌配对（CRN）：候选与基线坐同一副牌；参数没改到任何决策的种子差值严格为 0。
- 逐代"连续减半"：N1 个候选各跑 S1 个种子 → 前 K2 名加 S2 个种子 → 前 K3 名加 S3 个种子；
- 防赢家诅咒：本代最优者必须在**全新的验证种子**上 V 个种子再测，z≥--val-z 才接受，基线随之更新；
- 收尾：累计改动 vs 原生产（{}）在**专用保留种子**上测，这是唯一可以对外报告的数字。
对手 = 生产本身（自对弈），有"打赢自己"的风险——最终必须用实战 30 房 A/B 确认才能换线上权重。

    python3 tools/tune_arena.py --smoke                                  # 冒烟：单进程，约 1 分钟
    caffeinate -i -s nice -n 15 python3 tools/tune_arena.py --preset lite --max-minutes 480    # 本机（插电）
    python3 tools/tune_arena.py --preset cloud --jobs 126 --max-minutes 420                   # 云主机（128 vCPU）
    python3 tools/tune_arena.py --final --holdout 3000 --jobs N          # 只做收尾：当前累计改动 vs 生产
    python3 tools/tune_arena.py --status                                 # 看进度

断点续跑：状态在 tools/.cache/tune/（state.json + 每个种子结果 results.jsonl），重跑同一命令自动接着跑。
输出：reports/tune_arena.txt（每代一行 + 收尾），累计改动写 tools/overlays/tuned.json。
"""
import argparse
import hashlib
import json
import math
import os
import random
import statistics
import subprocess
import sys
import time
from multiprocessing import Pool

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import arena2  # noqa: E402  （会设 MJ_WEIGHTS_NO_FILE=1；对局内 frozen_file_weights 读 models/weights.json 当基线）
from mj.fit import frozen_file_weights, load_weights  # noqa: E402

STATE_DIR = os.path.join(ROOT, "tools", ".cache", "tune")
STATE = os.path.join(STATE_DIR, "state.json")
RESULTS = os.path.join(STATE_DIR, "results.jsonl")
REPORT = os.path.join(ROOT, "reports", "tune_arena.txt")
OVERLAY_OUT = os.path.join(ROOT, "tools", "overlays", "tuned.json")

# (参数, 步长下限, 下界)；步长 = sigma * max(|当前值|, 步长下限)
PARAMS = [(k, 30, None) for k in ("fd_uke", "fd_uke3", "fd_pair_far", "fd_pair_near", "fd_hon_iso", "fd_hon_pair",
                                  "fd_hon_trip", "fd_term", "fd_e28", "fd_isolated", "fd_seen", "fd_pair_route")]
PARAMS += [(k, 30, None) for k in ("fj_uke", "fj_uke3", "fj_pair_far", "fj_pair_near", "fj_hon_iso", "fj_hon_pair",
                                   "fj_hon_trip", "fj_term", "fj_e28", "fj_isolated", "fj_seen", "fj_pair_route",
                                   "fj_bt_after", "fj_bt_dist", "fj_plan_gain", "fj_mq_bt", "fj_disc_joker")]
PARAMS += [("baotou_slow1", 500, 0), ("baotou_slow1_dealer", 500, 0), ("baotou_faster", 500, 0),
           ("baotou_no_slow", 500, 0), ("baotou_hold_slack", 2, 0), ("menqing_baotou_weight", 50, 0),
           ("pair_route", 100, 0), ("seven_pairs_route", 100, 0), ("rev_margin", 0.05, 0), ("rev_margin_abs", 0.2, 0)]

PRESETS = {
    #         N1   S1   K2  S2   K3  S3   V     sigma  k_max
    "cloud": (128, 150, 24, 450, 6, 1200, 1500, 0.30, 4),
    "local": (24, 80, 6, 240, 2, 600, 600, 0.30, 3),     # 本机一代 ~5100 种子 ≈ 4.3h（M5 6 进程实测 ~3s/种子）
    "lite": (12, 60, 4, 180, 1, 400, 400, 0.30, 3),      # 本机一代 ~2200 种子 ≈ 1.9h，一晚约 4 代
    "smoke": (3, 2, 2, 2, 1, 2, 2, 0.30, 2),
}
SEED_BASE_GEN = 100000      # 第 g 代用 [SEED_BASE_GEN + g*20000, ...)
SEED_BASE_VAL = 600000      # 验证种子，第 g 代 [SEED_BASE_VAL + g*5000, ...)
SEED_BASE_HOLD = 900000     # 收尾保留种子（只在 --final 用）

_W = {}


def _init(rounds):
    _W["rounds"] = rounds


def _key(overlay):
    return hashlib.md5(json.dumps(overlay or {}, sort_keys=True).encode()).hexdigest()[:10]


def _task(args):
    a_ov, b_ov, seed = args
    rr = arena2._run_ab_seed(seed, a_ov or None, b_ov or None, "1v3", _W["rounds"], {})[0]
    diffs = arena2._per_seed_diff(rr)
    return _key(a_ov), _key(b_ov), seed, (diffs[0] if diffs else 0.0)


class Store:
    """(A 键, B 键, 种子) -> 差值，落盘可续跑。"""

    def __init__(self):
        self.d = {}
        if os.path.exists(RESULTS):
            with open(RESULTS, encoding="utf-8") as f:
                for line in f:
                    try:
                        a, b, s, v = json.loads(line)
                        self.d[(a, b, s)] = v
                    except ValueError:
                        pass
        self.f = open(RESULTS, "a", encoding="utf-8")

    def get(self, a, b, s):
        return self.d.get((a, b, s))

    def put(self, a, b, s, v):
        self.d[(a, b, s)] = v
        self.f.write(json.dumps([a, b, s, v]) + "\n")
        self.f.flush()


class Runner:
    def __init__(self, jobs, rounds, deadline):
        self.store = Store()
        self.deadline = deadline
        self.pool = Pool(jobs, initializer=_init, initargs=(rounds,)) if jobs > 1 else None
        if not self.pool:
            _init(rounds)

    def out_of_time(self):
        return self.deadline is not None and time.time() > self.deadline

    def run(self, pairs_seeds):
        """pairs_seeds: [(a_overlay, b_overlay, [seeds])]。缺的种子补跑；到时间返回 False。"""
        todo = []
        for a, b, seeds in pairs_seeds:
            ka, kb = _key(a), _key(b)
            todo += [(a, b, s) for s in seeds if self.store.get(ka, kb, s) is None]
        if not todo:
            return True
        it = self.pool.imap_unordered(_task, todo, chunksize=1) if self.pool else map(_task, todo)
        for ka, kb, s, v in it:
            self.store.put(ka, kb, s, v)
            if self.out_of_time():
                if self.pool:
                    self.pool.terminate()
                    self.pool = None
                return False
        return True

    def stats(self, a, b, seeds):
        ka, kb = _key(a), _key(b)
        vals = [v for v in (self.store.get(ka, kb, s) for s in seeds) if v is not None]
        if not vals:
            return 0.0, float("inf"), 0, 0.0
        m = statistics.mean(vals)
        se = statistics.stdev(vals) / math.sqrt(len(vals)) if len(vals) > 1 else float("inf")
        nz = sum(1 for v in vals if v != 0) / len(vals)
        return m, se, len(vals), nz


def _perturb(center, base, rng, sigma, k_max):
    cand = dict(center)
    picks = rng.sample(PARAMS, rng.randint(1, k_max))
    for name, floor, lo in picks:
        cur = cand.get(name, base.get(name))
        if cur is None:
            continue
        step = sigma * max(abs(cur), floor) * rng.gauss(0, 1)
        new = cur + step
        if lo is not None:
            new = max(lo, new)
        if isinstance(base.get(name), int) and not isinstance(base.get(name), bool):
            new = int(round(new))
        else:
            new = round(new, 4)
        if new == cur:
            new = cur + (1 if isinstance(cur, int) else 0.01) * (1 if step >= 0 else -1)
        cand[name] = new
    return cand


def _diff(cand, center):
    return {k: v for k, v in cand.items() if center.get(k) != v}


def _load_state():
    if os.path.exists(STATE):
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    return {"gen": 0, "center": {}, "accepted": [], "log": []}


def _save_state(st):
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE)
    os.makedirs(os.path.dirname(OVERLAY_OUT), exist_ok=True)
    with open(OVERLAY_OUT, "w", encoding="utf-8") as f:
        json.dump(st["center"], f, ensure_ascii=False, indent=1)


def _say(msg):
    print(msg, flush=True)
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    with open(REPORT, "a", encoding="utf-8") as f:
        f.write(msg + "\n")


def generation(st, runner, base, preset, z_val, smoke):
    n1, s1, k2, s2, k3, s3, v, sigma, k_max = preset
    g = st["gen"]
    rng = random.Random("gen%d" % g)
    center = st["center"]
    seeds_all = list(range(SEED_BASE_GEN + g * 20000, SEED_BASE_GEN + g * 20000 + s1 + s2 + s3))
    cands = [_perturb(center, base, rng, sigma, k_max) for _ in range(n1)]
    t0 = time.time()
    if not runner.run([(c, center, seeds_all[:s1]) for c in cands]):
        return False
    ranked = sorted(cands, key=lambda c: -runner.stats(c, center, seeds_all[:s1])[0])
    top = ranked[:k2]
    if not runner.run([(c, center, seeds_all[:s1 + s2]) for c in top]):
        return False
    top = sorted(top, key=lambda c: -runner.stats(c, center, seeds_all[:s1 + s2])[0])[:k3]
    if not runner.run([(c, center, seeds_all) for c in top]):
        return False
    best = max(top, key=lambda c: runner.stats(c, center, seeds_all)[0])
    m, se, n, nz = runner.stats(best, center, seeds_all)
    line = "第 %d 代（%.0f 分钟）：最优 %s 选拔 %+.3f±%.3f（%d 种子，改到决策的种子 %.0f%%）" % (
        g, (time.time() - t0) / 60, json.dumps(_diff(best, center), ensure_ascii=False), m, se, n, 100 * nz)
    accepted = False
    if m > 0:
        val_seeds = list(range(SEED_BASE_VAL + g * 5000, SEED_BASE_VAL + g * 5000 + v))
        if not runner.run([(best, center, val_seeds)]):
            return False
        vm, vse, vn, _ = runner.stats(best, center, val_seeds)
        accepted = vse > 0 and vm / vse >= z_val
        line += "；验证 %+.3f±%.3f（%d 种子，z=%.2f）→ %s" % (vm, vse, vn, vm / vse if vse else 0, "接受" if accepted else "拒绝")
        if accepted:
            st["accepted"].append({"gen": g, "change": _diff(best, center), "val": [vm, vse, vn]})
            st["center"] = best
    else:
        line += "；选拔均值≤0，不验证"
    st["log"].append(line)
    st["gen"] = g + 1
    _save_state(st)
    _say(line)
    return True


def final(st, runner, holdout):
    seeds = list(range(SEED_BASE_HOLD, SEED_BASE_HOLD + holdout))
    center = st["center"]
    if not center:
        _say("收尾：累计改动为空（没有任何一代被接受），生产保持不变。")
        return
    if not runner.run([(center, {}, seeds)]):
        m, se, n, _ = runner.stats(center, {}, seeds)
        _say("收尾（时间到，只完成 %d/%d 个保留种子）：累计改动 vs 生产 %+.3f±%.3f" % (n, holdout, m, se))
        return
    m, se, n, nz = runner.stats(center, {}, seeds)
    _say("收尾：累计改动（%d 项参数）vs 原生产，保留种子 %d 个：%+.3f 分/局，95%%CI [%+.3f, %+.3f]%s" % (
        len(center), n, m, m - 1.96 * se, m + 1.96 * se, "  ← 显著" if m - 1.96 * se > 0 else "  ← 不显著，不建议换线上"))
    _say("  累计改动：%s（已写 %s）" % (json.dumps(center, ensure_ascii=False), os.path.relpath(OVERLAY_OUT, ROOT)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--preset", choices=sorted(PRESETS), default="local")
    ap.add_argument("--jobs", type=int, default=int(os.environ.get("MJ_JOBS", "6")))
    ap.add_argument("--rounds", type=int, default=8)
    ap.add_argument("--max-minutes", type=float, default=None)
    ap.add_argument("--max-gens", type=int, default=999)
    ap.add_argument("--val-z", type=float, default=2.0, help="验证种子上 z≥这个值才接受")
    ap.add_argument("--holdout", type=int, default=None, help="收尾保留种子数（默认 cloud 3000 / local 1000）")
    ap.add_argument("--final", action="store_true", help="只做收尾检验")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--reset", action="store_true", help="清空调参状态从头来（结果缓存保留）")
    args = ap.parse_args()

    if subprocess.run(["pgrep", "-f", "mj[.]bot"], stdout=subprocess.DEVNULL).returncode == 0:
        sys.exit("bot 正在运行，先停 bot 再调参")
    global STATE_DIR, STATE, RESULTS, REPORT, OVERLAY_OUT
    if args.smoke:
        STATE_DIR = os.path.join(ROOT, "tools", ".cache", "tune_smoke")
        STATE, RESULTS = os.path.join(STATE_DIR, "state.json"), os.path.join(STATE_DIR, "results.jsonl")
        REPORT = os.path.join(STATE_DIR, "report.txt")
        OVERLAY_OUT = os.path.join(STATE_DIR, "tuned.json")
        args.preset, args.rounds, args.jobs, args.max_gens = "smoke", 2, 1, 2
        for p in (STATE, RESULTS):
            if os.path.exists(p):
                os.remove(p)
    os.makedirs(STATE_DIR, exist_ok=True)
    if args.reset and os.path.exists(STATE):
        os.remove(STATE)
    st = _load_state()
    if args.status:
        print("已跑 %d 代，接受 %d 次；累计改动 %s" % (st["gen"], len(st["accepted"]), json.dumps(st["center"], ensure_ascii=False)))
        for line in st["log"][-15:]:
            print(" ", line)
        return

    with frozen_file_weights():
        base = load_weights()
    missing = [p for p, _, _ in PARAMS if p not in base]
    preset = PRESETS[args.preset]
    deadline = time.time() + args.max_minutes * 60 if args.max_minutes else None
    runner = Runner(args.jobs, args.rounds, deadline)
    _say("=== tune_arena %s：preset=%s %s，jobs=%d，局数=%d，参数 %d 个%s；从第 %d 代开始，已接受 %d 次 ===" % (
        time.strftime("%m-%d %H:%M"), args.preset, preset, args.jobs, args.rounds, len(PARAMS) - len(missing),
        ("（基线里没有、跳过：%s）" % missing) if missing else "", st["gen"], len(st["accepted"])))
    holdout = args.holdout or (2 if args.smoke else 3000 if args.preset == "cloud" else 1000)
    if args.final:
        final(st, runner, holdout)
        return
    done = 0
    while done < args.max_gens:
        if not generation(st, runner, base, preset, args.val_z, args.smoke):
            _say("到 --max-minutes 时间上限，停在第 %d 代中途（重跑同一命令续跑）。" % st["gen"])
            break
        done += 1
    else:
        if args.smoke:
            final(st, runner, holdout)
    _say("累计：已跑 %d 代，接受 %d 次；收尾检验请运行 --final（用专用保留种子，和调参用的种子不重叠）。" % (st["gen"], len(st["accepted"])))


if __name__ == "__main__":
    main()
