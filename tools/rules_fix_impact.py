"""量化 ``mj/rules.py::_melds`` 跨花色顺子修复（2026-10-01）的影响面。

    python3 tools/rules_fix_impact.py --limit 20     # 冒烟（<1 分钟）
    python3 tools/rules_fix_impact.py                 # 全量，交给用户执行

对全语料每一局强制回放，在每次 ``tile_drawn`` 之后（该座位手里 14 张）
比较旧规则（没有 ``(start+2)//9`` 同花色检查）和新规则：
  1. ``win_standard`` 自摸判定不同（旧判能胡/新判不能胡，或反过来）；
  2. 摸牌前 13 张的 ``baotou`` 判定不同。
并统计 ``logs/*.jsonl`` 里 ``decision_attempt``（action=hu）后来 outcome 为
``conflict_409`` 的次数——这些是线上真正被服务端拒绝的胡牌请求；其中有多少
手牌在旧规则下判能胡、新规则下判不能胡，就是这个 bug 实际造成的线上损失。

明细写 ``reports/rules_fix_impact.tsv``，终端只打印 ≤40 行汇总。
旧规则实现在本文件里原样复刻（只去掉那一个检查），不 monkeypatch 生产代码。
"""
import argparse
import glob
import json
import os
import sys
from collections import Counter
from functools import lru_cache
from multiprocessing import Pool

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

os.environ.setdefault("MJ_WEIGHTS_NO_FILE", "1")

from mining_common import discover_files, merge_rounds  # noqa: E402
from mj import rules  # noqa: E402
from mj.sim.engine import IllegalActionError, RoundEngine  # noqa: E402
from mj.tiles import JOKER_IDX, NSUITS, TILE_INDEX, is_suited, rank, to_counts  # noqa: E402

REPORT = os.path.join(ROOT, "reports", "rules_fix_impact.tsv")


def _remove(c, i, n=1):
    v = list(c)
    v[i] -= n
    return tuple(v)


@lru_cache(maxsize=1 << 20)
def _melds_old(c, groups, jokers):
    i = next((i for i, n in enumerate(c) if n), NSUITS)
    if i == NSUITS:
        return jokers >= groups * 3
    if groups == 0:
        return False
    n = c[i]
    if n >= 3 and _melds_old(_remove(c, i, 3), groups - 1, jokers):
        return True
    if n >= 2 and jokers >= 1 and _melds_old(_remove(c, i, 2), groups - 1, jokers - 1):
        return True
    if n >= 1 and jokers >= 2 and _melds_old(_remove(c, i), groups - 1, jokers - 2):
        return True
    if is_suited(i) and rank(i) <= 7:
        need = (c[i + 1] == 0) + (c[i + 2] == 0)
        if need <= jokers:
            v = list(c)
            v[i] -= 1
            if c[i + 1]:
                v[i + 1] -= 1
            if c[i + 2]:
                v[i + 2] -= 1
            if _melds_old(tuple(v), groups - 1, jokers - need):
                return True
    if is_suited(i):
        for start in (i - 2, i - 1):
            if not is_suited(start) or start // 9 != i // 9:   # 旧规则：只查起点
                continue
            span = (start, start + 1, start + 2)
            missing = sum(1 for k in span if c[k] == 0)
            if missing > jokers:
                continue
            v = list(c)
            for k in span:
                if v[k]:
                    v[k] -= 1
            if _melds_old(tuple(v), groups - 1, jokers - missing):
                return True
    return False


def win_standard_old(counts, meld_groups=0):
    need_groups = 4 - meld_groups
    real, jokers = rules.strip_joker(counts)
    if sum(real) + jokers != need_groups * 3 + 2:
        return False
    for i, n in enumerate(real):
        for used, need in ((2, 0), (1, 1), (0, 2)):
            if n < used or need > jokers:
                continue
            if _melds_old(_remove(real, i, used), need_groups, jokers - need):
                return True
    return jokers >= 2 and _melds_old(real, need_groups, jokers - 2)


def _wins_old(counts, mg):
    return win_standard_old(counts, mg) or (mg == 0 and rules.seven_pairs(counts) is not None)


def baotou_old(counts13, mg=0):
    for i in range(NSUITS):
        if counts13[i] >= 4:
            continue
        v = list(counts13)
        v[i] += 1
        if not _wins_old(tuple(v), mg):
            return False
    return True


@lru_cache(maxsize=1 << 20)
def _cmp(counts14, counts13, mg):
    hu_old = win_standard_old(counts14, mg)
    hu_new = rules.win_standard(counts14, mg)
    bt_old = baotou_old(counts13, mg)
    bt_new = rules.baotou(counts13, mg)
    return hu_old, hu_new, bt_old, bt_new


def scan(path):
    stats = Counter()
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            game = json.load(f)
    except (OSError, ValueError):
        return stats, rows
    info = {r.get("round_no"): r for r in game.get("rounds") or []}
    for rnd in merge_rounds(game):
        meta = info.get(rnd["round_no"])
        hands = rnd.get("start_hands")
        if not meta or not hands or len(hands) != 4 or rnd.get("truncated") or not all(hands):
            continue
        engine = RoundEngine.from_known_hands(hands, meta.get("dealer", rnd.get("dealer")),
                                              rnd["round_no"], scores=[0, 0, 0, 0])
        stats["rounds"] += 1
        for ev in rnd["events"]:
            kind, seat, tile = ev.get("type"), ev.get("seat"), ev.get("tile")
            data = ev.get("data") or {}
            try:
                if kind == "tile_drawn":
                    engine.step_draw(forced_tile=tile)
                    h14 = list(engine.hands[seat])
                    pre = list(h14)
                    pre.remove(tile)
                    mg = engine.meld_groups(seat)
                    hu_o, hu_n, bt_o, bt_n = _cmp(to_counts(h14), to_counts(pre), mg)
                    stats["draws_checked"] += 1
                    if hu_o != hu_n:
                        stats["hu_diff"] += 1
                        rows.append((os.path.basename(path), rnd["round_no"], seat, "hu", hu_o, hu_n, " ".join(sorted(h14))))
                    if bt_o != bt_n:
                        stats["baotou_diff"] += 1
                        rows.append((os.path.basename(path), rnd["round_no"], seat, "baotou", bt_o, bt_n, " ".join(sorted(pre))))
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
                elif kind == "round_ended":
                    break
            except (IllegalActionError, ValueError, KeyError, IndexError):
                stats["replay_aborted_rounds"] += 1
                break
    return stats, rows


def count_409(log_paths):
    """线上 hu 请求被 409 拒绝的次数，及对应决策手牌在新旧规则下的判定。"""
    attempts, hu_conflict = {}, []
    for p in log_paths:
        with open(p, encoding="utf-8") as f:
            for line in f:
                if "decision_attempt" not in line and "conflict_409" not in line and '"kind": "decision"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                k, pl = obj.get("kind"), obj.get("payload") or {}
                if k == "decision_attempt" and pl.get("action") == "hu":
                    attempts[pl.get("decision_id")] = pl
                elif k == "decision_attempt_outcome" and pl.get("outcome") == "conflict_409" \
                        and pl.get("decision_id") in attempts:
                    hu_conflict.append(pl["decision_id"])
    return len(attempts), len(hu_conflict)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--jobs", type=int, default=6)
    args = ap.parse_args()
    files = discover_files(limit=args.limit)
    total, rows = Counter(), []
    with Pool(args.jobs) as pool:
        for s, r in pool.imap_unordered(scan, files, chunksize=4):
            total.update(s)
            rows.extend(r)
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("file\tround\tseat\tkind\told\tnew\thand\n")
        for r in rows:
            f.write("\t".join(str(x) for x in r) + "\n")
    n_hu, n_409 = count_409(sorted(glob.glob(os.path.join(ROOT, "logs", "*.jsonl"))))
    print("=== rules_fix_impact（明细 %s） ===" % REPORT)
    print("文件 %d，回放局数 %d（回放中断 %d），检查摸牌 %d 次" % (
        len(files), total["rounds"], total["replay_aborted_rounds"], total["draws_checked"]))
    print("自摸判定新旧不同：%d 次（旧能胡/新不能胡 %d，旧不能胡/新能胡 %d）" % (
        total["hu_diff"], sum(1 for r in rows if r[3] == "hu" and r[4] and not r[5]),
        sum(1 for r in rows if r[3] == "hu" and not r[4] and r[5])))
    print("爆头判定新旧不同：%d 次（旧判爆头/新判不是 %d，反过来 %d）" % (
        total["baotou_diff"], sum(1 for r in rows if r[3] == "baotou" and r[4] and not r[5]),
        sum(1 for r in rows if r[3] == "baotou" and not r[4] and r[5])))
    print("线上日志：hu 请求 %d 次，其中 conflict_409 被拒 %d 次" % (n_hu, n_409))
    print("（409 里有多少是这个 bug 造成的：看明细里 kind=hu、旧=True 新=False 的行，"
          "按 file/round/seat 对应 logs 里同局同座位的 409 记录）")


if __name__ == "__main__":
    main()
