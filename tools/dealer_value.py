"""T0(b)（docs/IMPL_TABLE_EV.md）：胡者接庄——量化"现在坐庄/接庄"对后续局号
还值多少分，扣掉个人水平（skill）之后的净值。

    python3 tools/dealer_value.py
    python3 tools/dealer_value.py --jobs 6 --no-cache

方法：
  skill(name) = 该名字全语料的场均每局得分。
  对每个对局文件（一场 8 局）、每个局号 r（1..局数-1）、每个座位：
    resid = (第 r+1 局到该文件最后一局的得分和) − skill(name) × (最后局号 − r)
  V(r) = 均值[resid | 第 r+1 局是庄] − 均值[resid | 第 r+1 局是闲]
  最后一局没有 r+1，V(最后一局) 恒为 0（不拟合，直接定义）。

写入 models/dealer_value.json 的是：全体样本、扣 skill 后的 V(r)（"V" 字段，
键是字符串局号）；打印时额外列 我们/高手/全体 三组，以及未扣 skill 的原始差
（对照用，验证"скill 调整"确实起作用——如果原始差和调整后差方向/量级差很多，
说明高手/我们本身局号分布不均匀，不调整会把"水平差"错记成"庄位差"）。
"""
import argparse
import hashlib
import json
import os
import pickle
import sys
from collections import Counter, defaultdict
from multiprocessing import Pool

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from mining_common import discover_files, seat_names  # noqa: E402
from baotou_funnel import MASTERS, OUR  # noqa: E402

CACHE_PATH = os.path.join(ROOT, "tools", ".cache", "dealer_value.pkl")
OUT_PATH = os.path.join(ROOT, "models", "dealer_value.json")


def scan(path):
    """返回 (score_totals, resid_records)。
    score_totals：name -> [sum_score, n_rounds]（用于全语料 skill(name)）。
    resid_records：[(name, r, suffix_sum, dealer_next)]，suffix_sum 是从 r+1
      局到本文件最后一局的得分和；skill 还没扣，main() 里统一二次算。"""
    score_totals = {}   # 普通 dict：结果要跨进程 pickle，defaultdict(lambda) 不能 pickle
    resid_records = []
    try:
        with open(path, encoding="utf-8") as source:
            game = json.load(source)
    except (OSError, ValueError):
        return score_totals, resid_records
    names = seat_names(game)
    if len(names) != 4:
        return score_totals, resid_records
    rounds = {r.get("round_no"): r for r in game.get("rounds") or [] if r.get("round_no") is not None}
    order = sorted(rounds)
    if not order:
        return score_totals, resid_records
    scores = {}
    dealer_of = {}
    for r in order:
        meta = rounds[r]
        s = meta.get("scores")
        if not isinstance(s, list) or len(s) != 4:
            continue
        scores[r] = s
        dealer_of[r] = meta.get("dealer")
    valid = sorted(scores)
    if not valid:
        return score_totals, resid_records
    for r in valid:
        for seat in range(4):
            tot = score_totals.setdefault(names[seat], [0.0, 0])
            tot[0] += scores[r][seat]
            tot[1] += 1
    last_r = valid[-1]
    for r in valid:
        if r >= last_r:
            continue
        nxt = r + 1
        if nxt not in dealer_of:
            continue
        for seat in range(4):
            after = [rr for rr in valid if rr > r]
            suffix = sum(scores[rr][seat] for rr in after)
            resid_records.append((names[seat], r, suffix, dealer_of[nxt] == seat, len(after)))
    return dict(score_totals), resid_records


def _scan_path(path):
    return path, scan(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()
    files = discover_files(limit=args.limit)

    with open(os.path.abspath(__file__), "rb") as own:
        code_ver = hashlib.md5(own.read()).hexdigest()
    cache = {}
    if not args.no_cache:
        try:
            with open(CACHE_PATH, "rb") as source:
                saved = pickle.load(source)
            if saved.get("ver") == code_ver:
                cache = saved.get("files") or {}
        except (OSError, ValueError, EOFError, pickle.UnpicklingError, AttributeError):
            cache = {}

    def file_key(path):
        st = os.stat(path)
        return (st.st_mtime, st.st_size)

    score_totals = defaultdict(lambda: [0.0, 0])
    all_resid = []
    todo, keys = [], {}
    for path in files:
        k = file_key(path)
        keys[path] = k
        hit = cache.get(path)
        if hit and hit[0] == k:
            st, rr = hit[1]
        else:
            todo.append(path)
            continue
        for name, (s, n) in st.items():
            score_totals[name][0] += s
            score_totals[name][1] += n
        all_resid.extend(rr)
    print("  缓存命中 %d 个文件，需要扫描 %d 个" % (len(files) - len(todo), len(todo)), flush=True)

    fresh = {}
    if todo:
        with Pool(args.jobs) as pool:
            for done, (path, (st, rr)) in enumerate(pool.imap_unordered(_scan_path, todo, chunksize=8), 1):
                plain_st = {k: list(v) for k, v in st.items()}
                fresh[path] = (keys[path], (plain_st, rr))
                for name, (s, n) in plain_st.items():
                    score_totals[name][0] += s
                    score_totals[name][1] += n
                all_resid.extend(rr)
                if done % 200 == 0 or done == len(todo):
                    print("  已扫 %d / %d" % (done, len(todo)), flush=True)
    if not args.no_cache:
        merged = {p: cache[p] for p in files if p in cache and p not in fresh}
        merged.update(fresh)
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "wb") as out:
            pickle.dump({"ver": code_ver, "files": merged}, out, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, CACHE_PATH)

    skill = {name: (s / n if n else 0.0) for name, (s, n) in score_totals.items()}

    # 按 r、组（我们/高手/全体）、是否下一局坐庄 聚合 resid 与原始 suffix
    buckets = defaultdict(lambda: Counter())  # (group, r, dealer_next) -> {sum, n}
    for name, r, suffix, dealer_next, last_len in all_resid:
        # last_len = suffix 实际覆盖的局数（缺局的文件也按真实局数扣 skill）
        resid = suffix - skill.get(name, 0.0) * last_len
        for g in (("我们" if name == OUR else ("高手" if name in MASTERS else None)), "全体"):
            if g is None:
                continue
            key = (g, r, dealer_next)
            c = buckets[key]
            c["n"] += 1
            c["resid_sum"] += resid
            c["raw_sum"] += suffix

    max_r = max((rec[1] for rec in all_resid), default=0)
    v_table = {}
    n_table = {}
    print("%-6s %-6s %8s %8s %10s %10s" % ("组", "局号", "V(r)", "样本数", "原始差(未扣skill)", ""))
    for g in ("我们", "高手", "全体"):
        for r in range(1, max_r + 1):
            c_true = buckets.get((g, r, True))
            c_false = buckets.get((g, r, False))
            if not c_true or not c_false or c_true["n"] < 5 or c_false["n"] < 5:
                continue
            v = c_true["resid_sum"] / c_true["n"] - c_false["resid_sum"] / c_false["n"]
            raw = c_true["raw_sum"] / c_true["n"] - c_false["raw_sum"] / c_false["n"]
            n = c_true["n"] + c_false["n"]
            print("%-6s %-6d %8.3f %8d %10.3f" % (g, r, v, n, raw))
            if g == "全体":
                v_table[str(r)] = v
                n_table[str(r)] = n
    v_table[str(max_r + 1 if max_r else 8)] = 0.0   # 最后一局没有 r+1，定义为 0
    print("%-6s %-6d %8.3f （定义值，非拟合）" % ("全体", max_r + 1 if max_r else 8, 0.0))

    out = {"V": v_table, "n": n_table,
          "meta": {"skill_names": len(skill), "resid_samples": len(all_resid), "files": len(files)}}
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("\n已写 %s" % OUT_PATH)


if __name__ == "__main__":
    main()
