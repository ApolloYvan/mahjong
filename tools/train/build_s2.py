"""S2 数据集：高手的决策（用另一半房间选人，见 player_stats.py），出牌/胡飘全量保留，"过"降采样并按 1/keep 加权。

    python3 tools/train/player_stats.py && python3 tools/train/build_s2.py --smoke
    caffeinate -i nice -n 15 python3 tools/train/build_s2.py --jobs 6 --max-minutes 60

- 房间奇偶 p 的对局，只取 ``masters_from[1-p]``（权重 1.0）和 ``top_all``（补充，权重 ``--top-all-weight``，默认 0.5）
  这些玩家的决策；other 全体不用；我们自己的账号不用。
- 头：``D`` 摸牌阶段非胡牌态（出牌/自己杠）；``H`` 摸牌阶段胡牌态（胡/飘/弃胡，``yh`` 0/1/2，弃胡/飘时同时有 ``yd``=打的牌）；
  ``R`` 响应窗口（过/碰/明杠/吃低/中/高，``yr`` 0..5）。
- 价值目标 ``val`` = 本局得分增量 + 连庄价值（该座位这局胡了才加 ``V(局号)``，同 mj/mc/decide.py 的 MC 口径）。
- 每条记录 ``w`` = 玩家权重 / keep 概率（"过"被降采样 -> 权重放大，保证响应头的边缘分布无偏）。
- 切分：``room % 10 < 2`` 为验证集（按房间）。分片 ``tools/.cache/train/parts_s2/{train,val}/<file>.jsonl``，可续跑。
"""
import argparse
import json
import os
import sys
import time
from collections import Counter
from multiprocessing import Pool

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "tools", "train"))
os.environ.setdefault("MJ_WEIGHTS_NO_FILE", "1")

from build_dataset import encode_record, iter_decisions, room_of, split_of  # noqa: E402
from mining_common import discover_files  # noqa: E402
from mj.nn import encode as E  # noqa: E402

MASTERS_JSON = os.path.join(ROOT, "tools", ".cache", "train", "masters.json")
PARTS = os.path.join(ROOT, "tools", ".cache", "train", "parts_s2")
R_INDEX = {E.A_PASS: 0, E.A_PENG: 1, E.A_GANG_MING: 2, E.A_CHI_LOW: 3, E.A_CHI_MID: 4, E.A_CHI_HIGH: 5}
_CFG = {}
_V = None


def dealer_value(round_no):
    global _V
    if _V is None:
        try:
            with open(os.path.join(ROOT, "models", "dealer_value.json"), encoding="utf-8") as f:
                _V = json.load(f).get("V") or {}
        except (OSError, ValueError):
            _V = {}
    return float(_V.get(str(round_no), 0.0))


def _init_worker(cfg):
    _CFG.update(cfg)


def tier_weight(name, parity):
    cfg = _CFG
    if name in cfg["masters_from"].get(str(1 - parity), ()):
        return 1.0
    if name in cfg["top_all"]:
        return cfg["top_all_weight"]
    return 0.0


def to_s2_row(rec, parity):
    row = encode_record(rec)
    y = row["y"]
    snap_phase = row["phase"]
    legal = row["legal"]
    ret = rec["ret"]
    val = None
    if ret.get("delta") is not None:
        val = ret["delta"] + (dealer_value(rec["round_no"]) if ret.get("win") else 0.0)
    out = {"id": row["id"], "room": row["room"], "split": row["split"], "i": row["i"], "v": row["v"],
           "legal": legal, "val": val, "w": tier_weight(rec["name"], parity) / rec["keep_prob"],
           "tier": "m" if rec["name"] in _CFG["masters_from"].get(str(1 - parity), ()) else "t"}
    if snap_phase == "draw":
        if E.A_HU in legal:
            out["head"] = "H"
            out["yh"] = 0 if y == E.A_HU else (1 if y == E.JOKER_IDX else 2)
            if y < 34:
                out["yd"] = y
        else:
            out["head"] = "D"
            out["yd"] = 34 if y == E.A_GANG_SELF else y   # D 头 35 个输出：34 出牌 + 下标 34 = 自己杠
    else:
        out["head"] = "R"
        out["yr"] = R_INDEX[y]
    out["joker"] = rec["snapshot"]["my_hand"].count("白") > 0
    return out




def process_file(task):
    path, pass_keep, seed = task
    base = os.path.basename(path)
    room, rh = room_of(base)
    parity = rh % 2
    split = split_of(rh)
    out_dir = os.path.join(PARTS, split)
    out_path = os.path.join(out_dir, base + ".jsonl")
    if os.path.exists(out_path):
        return base, None
    os.makedirs(out_dir, exist_ok=True)

    def keep(name, phase, action):
        if tier_weight(name, parity) <= 0:
            return 0.0
        return pass_keep if action.get("action") == "pass" else 1.0

    st = Counter()
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for rec in iter_decisions(path, keep=keep, seed=seed):
            try:
                row = to_s2_row(rec, parity)
            except (KeyError, ValueError, TypeError):
                st["error"] += 1
                continue
            st["n"] += 1
            st["head_" + row["head"]] += 1
            st["tier_" + row["tier"]] += 1
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    os.replace(tmp, out_path)
    return base, dict(st, split=split)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=int(os.environ.get("MJ_JOBS", "6")))
    ap.add_argument("--pass-keep", type=float, default=0.2)
    ap.add_argument("--top-all-weight", type=float, default=0.5)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--limit-files", type=int, default=None)
    ap.add_argument("--max-minutes", type=float, default=None)
    ap.add_argument("--smoke", action="store_true")
    args = ap.parse_args()
    global PARTS
    if args.smoke:
        args.limit_files = 150
        PARTS = os.path.join(ROOT, "tools", ".cache", "train", "parts_s2_smoke")
    try:
        with open(MASTERS_JSON, encoding="utf-8") as f:
            m = json.load(f)
    except (OSError, ValueError):
        print("没有 %s——先跑 tools/train/player_stats.py" % MASTERS_JSON)
        sys.exit(2)
    _CFG.update(masters_from={k: set(v) for k, v in m["masters_from"].items()}, top_all=set(m["top_all"]),
                top_all_weight=args.top_all_weight)
    files = discover_files(limit=args.limit_files)
    started = time.time()
    deadline = started + args.max_minutes * 60 if args.max_minutes else None
    tasks = [(p, args.pass_keep, args.seed) for p in files]
    tot, done, skipped, stopped = Counter(), 0, 0, False
    per_split = Counter()
    # macOS 多进程是 spawn：子进程重新 import 本模块，_CFG 在子进程里是空的，
    # 必须经 initializer 传过去（冒烟走单进程，所以没暴露）。
    pool = (Pool(args.jobs, initializer=_init_worker, initargs=(dict(_CFG),))
            if args.jobs > 1 and not args.smoke else None)
    results = pool.imap_unordered(process_file, tasks, chunksize=2) if pool else map(process_file, tasks)
    try:
        for base, st in results:
            if st is None:
                skipped += 1
            else:
                done += 1
                per_split[st["split"]] += st.get("n", 0)
                tot.update({k: v for k, v in st.items() if k != "split"})
            if deadline and time.time() > deadline:
                stopped = True
                break
    finally:
        if pool:
            pool.terminate()
            pool.join()
    print("=== build_s2（%s，%.0fs；分片 %s） ===" % ("冒烟" if args.smoke else "全量", time.time() - started, PARTS))
    print("文件 %d：新处理 %d，已有跳过 %d%s" % (len(files), done, skipped, "；到 --max-minutes 提前停，重跑续跑" if stopped else ""))
    print("新增记录 %d（训练 %d / 验证 %d）；头：%s；高手 %d / 补充 %d；出错 %d" % (
        tot["n"], per_split["train"], per_split["val"], {k[5:]: v for k, v in tot.items() if k.startswith("head_")},
        tot["tier_m"], tot["tier_t"], tot["error"]))


if __name__ == "__main__":
    main()
