"""S2' 的"高手标签"数据（管线冒烟 + 看一眼"高手的选择落在生产前 K 名里的比例"）：

    python3 tools/train/build_resid.py --smoke
    caffeinate -i nice -n 15 python3 tools/train/build_resid.py --jobs 6 --max-minutes 60

高手 = ``player_stats.py`` 里用另一半房间选出的（权重 1.0）+ 全历史补充名单（权重 ``--top-all-weight``）。
每个"摸牌后出牌、非胡牌态、非抓打圈受限"的决策：生产前 K 个候选 + 逐候选特征（``mj.nn.cands``），标签 = 高手实际打的牌在候选里的
下标（不在前 K 名里的丢弃并计数）。输出分片 ``tools/.cache/train/resid_parts/{train,val}/<file>.jsonl``（可续跑）。
这是模仿标签，不是"改进"标签：真正的训练目标来自 ``search_s3.py``（同一格式）。
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

import build_s2  # noqa: E402
from build_dataset import iter_decisions, room_of, split_of  # noqa: E402
from mining_common import discover_files  # noqa: E402
from mj import bot  # noqa: E402
from mj.fit import frozen_file_weights  # noqa: E402
from mj.nn import cands as C  # noqa: E402

PARTS = os.path.join(ROOT, "tools", ".cache", "train", "resid_parts")


def _init(cfg):
    build_s2._CFG.update(cfg)
    ctx = frozen_file_weights()
    ctx.__enter__()


def process_file(task):
    path, k, seed = task
    base = os.path.basename(path)
    room, rh = room_of(base)
    parity = rh % 2
    split = split_of(rh)
    out_dir = os.path.join(PARTS, split)
    out_path = os.path.join(out_dir, base + ".jsonl")
    if os.path.exists(out_path):
        return base, None
    os.makedirs(out_dir, exist_ok=True)
    st = Counter()
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for rec in iter_decisions(path, keep=lambda nm, ph, ac: 1.0 if (ac.get("action") == "discard" and build_s2.tier_weight(nm, parity) > 0) else 0.0,
                                  seed=seed):
            snap = rec["snapshot"]
            if snap["phase"] != "draw" or rec["action"].get("action") != "discard":
                continue
            try:
                prod = bot._choose_action_production(snap)
                if not prod or prod.get("action") != "discard":
                    st["prod_not_discard"] += 1
                    continue
                cs = C.production_candidates(snap, prod_tile=prod["tile"], k=k)
                if len(cs) < 2:
                    continue
                tiles = [c["tile"] for c in cs]
                st["n_all"] += 1
                st["prod_match"] += rec["action"]["tile"] == prod["tile"]
                if rec["action"]["tile"] not in tiles:
                    st["not_in_topk"] += 1
                    continue
                xs = C.candidate_features(snap, cs, rec["ctx"])
            except Exception:   # noqa: BLE001
                st["error"] += 1
                continue
            st["n"] += 1
            f.write(json.dumps({"id": rec["id"], "room": rh, "split": split, "src": "masters", "cands": tiles,
                                "x": [[round(v, 5) for v in x] for x in xs], "y": tiles.index(rec["action"]["tile"]),
                                "w": build_s2.tier_weight(rec["name"], parity)}, separators=(",", ":")) + "\n")
    os.replace(tmp, out_path)
    return base, dict(st, split=split)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=int(os.environ.get("MJ_JOBS", "6")))
    ap.add_argument("--k", type=int, default=C.K_DEFAULT)
    ap.add_argument("--top-all-weight", type=float, default=0.5)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--limit-files", type=int, default=None)
    ap.add_argument("--max-minutes", type=float, default=None)
    ap.add_argument("--smoke", action="store_true")
    args = ap.parse_args()
    global PARTS
    if args.smoke:
        args.limit_files = None
        PARTS = os.path.join(ROOT, "tools", ".cache", "train", "resid_parts_smoke")
    with open(build_s2.MASTERS_JSON, encoding="utf-8") as f:
        m = json.load(f)
    cfg = {"masters_from": {k: set(v) for k, v in m["masters_from"].items()}, "top_all": set(m["top_all"]),
           "top_all_weight": args.top_all_weight}
    files = discover_files(limit=args.limit_files)
    if args.smoke:   # 冒烟：隔着取 16 个文件（覆盖不同房间，验证集才不会是空的）
        files = files[::max(1, len(files) // 16)][:16]
    started = time.time()
    deadline = started + args.max_minutes * 60 if args.max_minutes else None
    tasks = [(p, args.k, args.seed) for p in files]
    pool = Pool(args.jobs, initializer=_init, initargs=(cfg,)) if args.jobs > 1 and not args.smoke else None
    if not pool:
        _init(cfg)
    it = pool.imap_unordered(process_file, tasks, chunksize=1) if pool else map(process_file, tasks)
    tot, done, skipped, stopped = Counter(), 0, 0, False
    try:
        for base, st in it:
            if st is None:
                skipped += 1
            else:
                done += 1
                tot.update({k: v for k, v in st.items() if k != "split"})
            if deadline and time.time() > deadline:
                stopped = True
                break
    finally:
        if pool:
            pool.terminate()
            pool.join()
    n_all = max(1, tot["n_all"])
    print("=== build_resid（%s，%.0fs；分片 %s） ===" % ("冒烟" if args.smoke else "全量", time.time() - started, PARTS))
    print("文件 %d：新处理 %d，已有跳过 %d%s" % (len(files), done, skipped, "；到 --max-minutes 提前停，重跑续跑" if stopped else ""))
    print("决策 %d：高手选择==生产选择 %.1f%%；不在生产前 %d 名里 %.1f%%（这部分丢弃）；写入 %d；出错 %d" % (
        tot["n_all"], 100.0 * tot["prod_match"] / n_all, args.k, 100.0 * tot["not_in_topk"] / n_all, tot["n"], tot["error"]))


if __name__ == "__main__":
    main()
