"""我们(当前) 比高手差的分数到底丢在哪：按"起手财神数 × 副露数"拆格子，
每格把分差拆成「做庄 / 胡得少 / 胡得小 / 输得多」四项，四项之和 = 分差
（代数恒等式，见下面"分解算法"，不是拟合出来的近似）。

    python3 tools/gap_breakdown.py

======================================================================
防选择偏差（本次新增）
======================================================================
用哪些房间数据"挑出谁是高手"、又用哪些房间数据"拿来做差距分解"，如果是
同一批房间，会有选择偏差（挑出来的高手恰好是那批房里表现好的，分解结果
系统性偏乐观）。按 ``md5(room_id)`` 的奇偶把全部房间分成两半（跟具体
是哪个日期/哪一批采集无关，纯按房间 ID 的哈希值切，可复现）：
  - 一半（"选人半"）只用来判断 ``baotou_funnel.MASTERS`` 里每个候选人在
    这批房间里是否真的跑赢基准（``diff`` 均值 > 0 且 n>=30），滤掉的人
    这次不计入"高手"；
  - 另一半（"分解半"）只用来做上面这套分解统计。
两个切分方向（奇数选人/偶数分解，反过来再来一次）都跑一遍、都打印，不
只挑一个方向报"好看"的结果。

======================================================================
分解算法（严格代数恒等式，不是回归/拟合）
======================================================================
记一个格子里"我们(当前)"和"高手"两组的均值差 diff = mean(score − base)，
base 是这个格子（含庄闲）在全部分组里的共同基准（跟 ``luck_vs_masters``
一致的口径）。

第一步——拆出"做庄"贡献：diff 是"庄"和"闲"两个子集的加权平均（权重=
各自局数占比 dr/`1-dr`），用恒等式
    v_diff − m_diff
      = (v_dr − m_dr) × (m_d_diff − m_nd_diff)                [做庄]
      + v_dr × (v_d_diff − m_d_diff) + (1 − v_dr) × (v_nd_diff − m_nd_diff)   [其余，记作 residual]
这是"加权平均"的精确分解（不是近似）：第一项是"我们做庄频率跟高手不同"
乘以"高手做庄相对不做庄能多赚多少"；第二项是"在各自的庄/闲占比权重下，
我们跟高手比，牌技本身的差距"。

第二步——把 residual 在庄、闲两个子集里各自拆成经典三项（跟本工具旧版
一致，代数上恒等：wr=胜率，wp=胡牌均分，lp=未胡均分，diff=wr·wp+(1−wr)·lp）：
    胡得少 = (v_wr − m_wr) × (m_wp − m_lp)
    胡得小 =  v_wr        × (v_wp − m_wp)
    输得多 = (1 − v_wr)   × (v_lp − m_lp)
    三项之和 == v_diff − m_diff（该子集内的恒等式）
residual = v_dr×(庄子集三项和) + (1−v_dr)×(闲子集三项和)，所以最终
    做庄 + 胡得少 + 胡得小 + 输得多 == v_diff − m_diff
到浮点精度对账，脚本里会打印最大误差并断言 < 0.01。

======================================================================
置信区间
======================================================================
每格每项按房间聚类做 bootstrap（默认 200 次重抽样，抽样单位是房间，不是
单局——同一房间的多个座位/多局不是独立观测）：每次重抽样出跟原始同样多
的房间（有放回），重新算一遍这格子的四项分解，取 2.5/97.5 分位数当 95%CI。
"""
import argparse
import hashlib
import json
import os
import random
import sys
from collections import defaultdict
from multiprocessing import Pool

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from mining_common import discover_files, merge_rounds  # noqa: E402
from mining_common import seat_names  # noqa: E402
from baotou_funnel import MASTERS, OUR, recent_rooms  # noqa: E402
from mj.shanten import shanten  # noqa: E402
from mj.tiles import to_counts  # noqa: E402

JOKER = "白"
N_BOOTSTRAP = 200
MIN_CELL_N = 20


def _final_meld_count(events, seat):
    n = 0
    for ev in events or []:
        if ev.get("seat") != seat:
            continue
        t = ev.get("type")
        if t in ("chi", "peng"):
            n += 1
        elif t == "gang" and (ev.get("data") or {}).get("kind") != "bu":
            n += 1   # 暗杠/直接杠开新的一组；补杠是升级已有的碰，不算新组
    return n


def scan(path):
    try:
        with open(path, encoding="utf-8") as source:
            game = json.load(source)
    except (OSError, ValueError):
        return None, []
    names = seat_names(game)
    if len(names) != 4:
        return None, []
    room_id = game.get("room_id") or path
    info = {r.get("round_no"): r for r in game.get("rounds") or []}
    out = []
    for rnd in merge_rounds(game):
        meta = info.get(rnd["round_no"])
        hands = rnd.get("start_hands")
        if not meta or not hands or len(hands) != 4 or rnd.get("truncated") or not all(hands):
            continue
        scores = meta.get("scores") or [0] * 4
        dealer = meta.get("dealer", rnd.get("dealer"))
        winner = None if meta.get("is_draw") else meta.get("winner")
        events = rnd.get("events") or []
        for s in range(4):
            joker_bucket = min(hands[s].count(JOKER), 2)
            meld_bucket = min(_final_meld_count(events, s), 2)
            key = (s == dealer, joker_bucket, meld_bucket)
            out.append((room_id, names[s], key, scores[s], winner == s))
    return room_id, out


def _load_rows(args):
    recent = recent_rooms(args.since)
    files = discover_files()
    rows = []
    with Pool(args.jobs) as pool:
        for done, (room, part) in enumerate(pool.imap_unordered(scan, files, chunksize=8), 1):
            for room_id, name, key, score, won in part:
                g = ("我们(当前)" if room in recent else "我们(以前)") if name == OUR else (
                    "候选高手" if name in MASTERS else "其他")
                rows.append((room_id, g, name, key, score, won))
            if done % 500 == 0 or done == len(files):
                print("  已扫 %d / %d" % (done, len(files)), flush=True)
    return rows


def _room_half(room_id):
    """md5(room_id) 的奇偶（0/1），纯按房间 ID 切，跟采集时间/批次无关。"""
    return int(hashlib.md5(str(room_id).encode("utf-8")).hexdigest(), 16) % 2


def _select_masters(rows_half):
    """在"选人半"里，把 候选高手 名单按"这批房间里 diff 均值 > 0 且
    n>=30" 过滤：只有真的跑赢基准的才在这个方向下算"高手"，不是直接照抄
    ``baotou_funnel.MASTERS`` 静态名单。"""
    tot, cnt = defaultdict(float), defaultdict(int)
    for _room, _g, _name, key, score, _won in rows_half:
        tot[key] += score
        cnt[key] += 1
    base = {k: tot[k] / cnt[k] for k in tot}
    diff_sum, diff_n = defaultdict(float), defaultdict(int)
    for _room, g, name, key, score, _won in rows_half:
        if g != "候选高手":
            continue
        diff_sum[name] += score - base[key]
        diff_n[name] += 1
    selected = {n for n in MASTERS if diff_n[n] >= 30 and diff_sum[n] / diff_n[n] > 0}
    return selected, {n: (diff_sum[n] / diff_n[n] if diff_n[n] else None, diff_n[n]) for n in MASTERS}


def _relabel(rows, masters_selected):
    """把"候选高手"按 masters_selected 收窄成"高手"，其余候选人并入"其他"。"""
    out = []
    for room_id, g, name, key, score, won in rows:
        if g == "候选高手":
            g = "高手" if name in masters_selected else "其他"
        out.append((room_id, g, name, key, score, won))
    return out


def _cell_key(key):
    return (key[1], key[2])   # (joker_bucket, meld_bucket)，做庄拆到 decompose() 里单独处理


def _base_by_key(rows):
    tot, cnt = defaultdict(float), defaultdict(int)
    for _room, _g, _name, key, score, _won in rows:
        tot[key] += score
        cnt[key] += 1
    return {k: tot[k] / cnt[k] for k in tot}


def _group_stats(rows, group, cell, dealer_flag, base):
    """给定组名、格子（不含庄闲）、庄闲，返回 n/wr/wp/lp/diff，n<MIN_CELL_N
    返回 None。"""
    scores, wins = [], []
    diffs = []
    for _room, g, _name, key, score, won in rows:
        if g != group or _cell_key(key) != cell or key[0] != dealer_flag:
            continue
        scores.append(score)
        wins.append(won)
        diffs.append(score - base.get(key, 0.0))
    n = len(scores)
    if n < MIN_CELL_N:
        return None
    w = sum(1 for x in wins if x)
    wp = sum(s for s, won in zip(scores, wins) if won) / w if w else 0.0
    lp = sum(s for s, won in zip(scores, wins) if not won) / (n - w) if n > w else 0.0
    return {"n": n, "wr": w / n, "wp": wp, "lp": lp, "diff": sum(diffs) / n}


def decompose(rows, cell, base, v_group="我们(当前)", m_group="高手"):
    """返回 None（数据不够）或 dict：n_v, n_m, diff_v, diff_m,
    terms={做庄, 胡得少, 胡得小, 输得多}, recon_error。"""
    vt = _group_stats(rows, v_group, cell, True, base)
    vf = _group_stats(rows, v_group, cell, False, base)
    mt = _group_stats(rows, m_group, cell, True, base)
    mf = _group_stats(rows, m_group, cell, False, base)
    if not (vt and vf and mt and mf):
        return None
    v_dr = vt["n"] / (vt["n"] + vf["n"])
    m_dr = mt["n"] / (mt["n"] + mf["n"])
    dealer_term = (v_dr - m_dr) * (mt["diff"] - mf["diff"])

    def three_terms(v, m):
        less = (v["wr"] - m["wr"]) * (m["wp"] - m["lp"])
        small = v["wr"] * (v["wp"] - m["wp"])
        lose = (1 - v["wr"]) * (v["lp"] - m["lp"])
        return less, small, lose

    less_t, small_t, lose_t = three_terms(vt, mt)
    less_f, small_f, lose_f = three_terms(vf, mf)
    less = v_dr * less_t + (1 - v_dr) * less_f
    small = v_dr * small_t + (1 - v_dr) * small_f
    lose = v_dr * lose_t + (1 - v_dr) * lose_f

    diff_v = v_dr * vt["diff"] + (1 - v_dr) * vf["diff"]
    diff_m = m_dr * mt["diff"] + (1 - m_dr) * mf["diff"]
    recon = (dealer_term + less + small + lose) - (diff_v - diff_m)
    return {"n_v": vt["n"] + vf["n"], "n_m": mt["n"] + mf["n"], "diff_v": diff_v, "diff_m": diff_m,
            "terms": {"做庄": dealer_term, "胡得少": less, "胡得小": small, "输得多": lose},
            "recon_error": recon}


def _bootstrap_ci(rows_by_room, room_ids, cell, base):
    """按房间聚类重抽样 N_BOOTSTRAP 次，返回每一项 {"做庄":(lo,hi), ...}
    加总 diff 的 (lo,hi)。跳过重抽样后数据不够（<MIN_CELL_N）的次数。"""
    samples = defaultdict(list)
    n_rooms = len(room_ids)
    if n_rooms == 0:
        return {}
    for _ in range(N_BOOTSTRAP):
        picked = [random.choice(room_ids) for _ in range(n_rooms)]
        boot_rows = []
        for rid in picked:
            boot_rows.extend(rows_by_room.get(rid, ()))
        boot_base = _base_by_key(boot_rows)
        d = decompose(boot_rows, cell, boot_base)
        if d is None:
            continue
        for term, val in d["terms"].items():
            samples[term].append(val)
        samples["总分差"].append(d["diff_v"] - d["diff_m"])
    out = {}
    for term, vals in samples.items():
        if len(vals) < N_BOOTSTRAP // 2:
            continue
        vals = sorted(vals)
        lo = vals[int(0.025 * len(vals))]
        hi = vals[min(len(vals) - 1, int(0.975 * len(vals)))]
        out[term] = (lo, hi)
    return out


def _run_direction(rows, select_half, decomp_half_label, detail_out):
    select_rows = [r for r in rows if _room_half(r[0]) == select_half]
    decomp_rows = [r for r in rows if _room_half(r[0]) != select_half]
    masters_selected, master_diag = _select_masters(select_rows)
    decomp_rows = _relabel(decomp_rows, masters_selected)

    direction_label = "%s选高手/%s分解" % ("偶数" if select_half == 0 else "奇数", decomp_half_label)
    lines = []
    lines.append("\n--- 方向：用房间哈希%s（高手=%d/%d 名候选人通过筛选）---" % (
        direction_label, len(masters_selected), len(MASTERS)))
    dropped = sorted(n for n in MASTERS if n not in masters_selected)
    if dropped:
        lines.append("  本方向滤掉的候选高手（选人半里 diff<=0 或 n<30）：%s" % ", ".join(
            "%s(diff=%s,n=%d)" % (n, "%.2f" % master_diag[n][0] if master_diag[n][0] is not None else "NA",
                                   master_diag[n][1]) for n in dropped))

    base = _base_by_key(decomp_rows)
    rows_by_room = defaultdict(list)
    for r in decomp_rows:
        rows_by_room[r[0]].append(r)
    room_ids = list(rows_by_room)

    cells = sorted({_cell_key(r[3]) for r in decomp_rows})
    max_recon_error = 0.0
    lines.append("%-14s %8s %8s %10s | %10s %10s %10s %10s" % (
        "格子(财神/副露)", "局数(我)", "局数(高手)", "分差", "做庄", "胡得少", "胡得小", "输得多"))
    for cell in cells:
        d = decompose(decomp_rows, cell, base)
        if d is None:
            continue
        max_recon_error = max(max_recon_error, abs(d["recon_error"]))
        ci = _bootstrap_ci(rows_by_room, room_ids, cell, base)
        label = "%d白/%s组" % (cell[0], "2+" if cell[1] == 2 else cell[1])
        total_diff = d["diff_v"] - d["diff_m"]
        lines.append("%-14s %8d %8d %+10.2f | %+10.2f %+10.2f %+10.2f %+10.2f" % (
            label, d["n_v"], d["n_m"], total_diff,
            d["terms"]["做庄"], d["terms"]["胡得少"], d["terms"]["胡得小"], d["terms"]["输得多"]))
        if ci:
            def fmt(term):
                lo, hi = ci.get(term, (float("nan"), float("nan")))
                return "[%+.2f,%+.2f]" % (lo, hi)
            lines.append("               95%%CI（按房间聚类 bootstrap）        %s %s %s %s %s" % (
                fmt("总分差"), fmt("做庄"), fmt("胡得少"), fmt("胡得小"), fmt("输得多")))
    lines.append("对账：本方向所有格子里 |四项之和 − 实际分差| 的最大值 = %.2e（验收线 < 0.01）" % max_recon_error)
    assert max_recon_error < 0.01, "分解代数恒等式对不上账，说明实现有 bug，不是数据问题"
    detail_out.extend(lines)
    return lines


def main():
    global N_BOOTSTRAP
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-09-25T15:30")
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--bootstrap", type=int, default=N_BOOTSTRAP)
    args = ap.parse_args()
    N_BOOTSTRAP = args.bootstrap
    random.seed(0)

    rows = _load_rows(args)
    print("共 %d 条 (房间,座位,局) 记录" % len(rows))
    detail = ["共 %d 条 (房间,座位,局) 记录" % len(rows)]
    lines0 = _run_direction(rows, select_half=0, decomp_half_label="奇数半", detail_out=detail)
    lines1 = _run_direction(rows, select_half=1, decomp_half_label="偶数半", detail_out=detail)

    os.makedirs(os.path.join(ROOT, "reports"), exist_ok=True)
    out_path = os.path.join(ROOT, "reports", "gap_breakdown_detail.txt")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(detail) + "\n")

    for line in lines0:
        print(line)
    for line in lines1:
        print(line)
    print("\n完整明细见 %s" % out_path)
    print("读法：两个方向的数字应该大致一致（差异在 bootstrap CI 范围内）——"
          "如果明显不一致，说明存在选择偏差，结论要打折扣。")


if __name__ == "__main__":
    main()
