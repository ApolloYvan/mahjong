"""G3a：三组数据对齐——arena2 自对局 vs 真实日志里的高手 vs 真实日志里的
我方——校准 ``mj/mc/rollout.py`` 里对手快速策略要拟合到哪条曲线。

    python3 tools/sim_calibrate.py --arena-seeds 5      # 冒烟，<1 分钟
    python3 tools/sim_calibrate.py --arena-seeds 400    # 全量，交给用户执行

输出机器可读的 ``reports/sim_calibrate.json``（阶段二拟合脚本
``tools/mc_check.py calibrate`` 直接读这个文件），终端只打印 ≤40 行汇总。

======================================================================
口径
======================================================================
- **累计胡牌率(第 n 摸)** = (本组里"赢家在自己第 <=n 次摸牌时胡牌"的
  座位·局数) / (本组全部座位·局数)。分母是全部座位·局数（含没胡、流局
  的座位），不是"存活到第 n 摸"的条件概率——更贴近"到第 n 摸这个时间点，
  概率上我已经赢了多少"这个校准目标（阶段二验收线直接引用这条曲线在
  第 6/10/14 摸的值）。
- **副露 0/1/2+ 占胡比例** 和 **摸牌序号曲线** 是两个独立的边际统计，
  没有交叉成三维表（真实语料这两个维度交叉后单元格 n 太小，交叉了反而
  置信区间大到没法用；阶段二的校准验收线本身也是分别对这两条曲线各自
  核对，不要求交叉）。
- 真实语料的番型标签（"财飘"/"双财飘"/"连杠×2"/"杠飘链×2"...）用服务端
  真实词表（``tools/fan_breakdown.py`` 已经用全量 detail+fan 反推验证过
  的 ``BRANCH_TAGS``/``MULTIPLIER_TAGS``），不是 ``mj.rules.evaluate()``
  本地词表（两套词表不一样，见 ``fan_breakdown.py`` 模块 docstring）。
  arena2 自对局用的是本地 ``mj.rules`` 词表（自己对自己，天然一致），
  两组的"占胡比例"分类逻辑因此不完全是同一份代码，但分类的**语义**
  （爆头/七对/财飘/杠开/4个白板）一一对应，可以直接比大小。
- CI：胡牌率/占比类用二项分布正态近似（±1.96·sqrt(p(1-p)/n))；均值类
  （吃碰杠次数）用正态近似（±1.96·SE）。n 较小的格子 CI 会很宽，如实
  报告，不做任何"看起来更整齐"的截断。
"""
import argparse
import glob
import json
import math
import os
import sys
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from mining_common import discover_files, merge_rounds, seat_names  # noqa: E402
from baotou_funnel import MASTERS, OUR, recent_rooms  # noqa: E402
from fan_breakdown import BRANCH_TAGS, MULTIPLIER_TAGS  # noqa: E402

os.environ.setdefault("MJ_WEIGHTS_NO_FILE", "1")
from arena2 import Arena2Match  # noqa: E402

DRAW_CHECKPOINTS = (2, 4, 6, 8, 10, 12, 14, 16, 18, 20)


def _wilson_normal_ci(p, n):
    if n == 0:
        return 0.0
    return 1.96 * math.sqrt(max(p * (1 - p), 0.0) / n)


def _final_meld_count(events, seat):
    n = 0
    for ev in events or []:
        if ev.get("seat") != seat:
            continue
        t = ev.get("type")
        if t in ("chi", "peng"):
            n += 1
        elif t == "gang" and (ev.get("data") or {}).get("kind") != "bu":
            n += 1
    return n


def _meld_bucket(n):
    return min(n, 2)


# ---------------------------------------------------------------- 真实日志

def _scan_real_file(path):
    seat_rows, win_rows, round_rows = [], [], []
    try:
        with open(path, encoding="utf-8") as source:
            game = json.load(source)
    except (OSError, ValueError):
        return seat_rows, win_rows, round_rows
    names = seat_names(game)
    if len(names) != 4:
        return seat_rows, win_rows, round_rows
    room_id = game.get("room_id") or path
    info = {r.get("round_no"): r for r in game.get("rounds") or []}
    for rnd in merge_rounds(game):
        meta = info.get(rnd["round_no"])
        hands = rnd.get("start_hands")
        if not meta or not hands or len(hands) != 4 or rnd.get("truncated") or not all(hands):
            continue
        events = rnd.get("events") or []
        dealer = meta.get("dealer", rnd.get("dealer"))
        draws = [0, 0, 0, 0]
        chi_n = peng_n = gang_n = 0
        winner_seat, is_draw, fan, detail = None, True, None, []
        for ev in events:
            t = ev.get("type")
            s = ev.get("seat")
            if t == "tile_drawn" and s is not None:
                draws[s] += 1
            elif t == "chi":
                chi_n += 1
            elif t == "peng":
                peng_n += 1
            elif t == "gang":
                gang_n += 1
            elif t == "round_ended":
                data = ev.get("data") or {}
                is_draw = bool(data.get("draw"))
                if not is_draw:
                    winner_seat = ev.get("seat")
                    fan = data.get("fan")
                    detail = data.get("detail") or []
        round_rows.append({"room_id": room_id, "is_draw": is_draw, "chi": chi_n, "peng": peng_n,
                           "gang": gang_n, "names": list(names)})
        for s in range(4):
            seat_rows.append({"room_id": room_id, "name": names[s], "dealer": s == dealer,
                              "is_winner": s == winner_seat, "total_draws": draws[s]})
        if winner_seat is not None and fan is not None:
            win_rows.append({"room_id": room_id, "name": names[winner_seat], "fan": fan,
                             "detail": detail, "meld_count": _final_meld_count(events, winner_seat),
                             "cats": _real_cats(detail)})
    return seat_rows, win_rows, round_rows


_QIDUI_TAGS = {"七对", "豪华七对×1", "豪华七对×2", "豪华七对×3"}
_PIAO_TAGS = {"财飘", "双财飘"}


def _real_cats(detail):
    """服务端真实词表分类（``tools/fan_breakdown.py`` 已用全量 detail+fan
    反推验证过的标签，不是 ``mj.rules.evaluate()`` 本地词表，见模块
    docstring）。"""
    return {
        "baotou": "爆头" in detail,
        "qidui": any(d in _QIDUI_TAGS for d in detail),
        "piao": any(d in _PIAO_TAGS for d in detail),
        "gang_kai": "杠开" in detail,
        "four_white": "4个白板" in detail,
    }


def _group_of(name, room, recent):
    if name == OUR:
        return "我们(当前)" if room in recent else "我们(以前)"
    if name in MASTERS:
        return "高手"
    return None


def load_real(args):
    from multiprocessing import Pool
    recent = recent_rooms(args.since)
    files = discover_files()
    all_seat, all_win, all_round = [], [], []
    with Pool(args.jobs) as pool:
        for done, (seat_rows, win_rows, round_rows) in enumerate(pool.imap_unordered(_scan_real_file, files, chunksize=8), 1):
            all_seat.extend(seat_rows)
            all_win.extend(win_rows)
            all_round.extend(round_rows)
            if done % 1000 == 0 or done == len(files):
                print("  已扫 %d / %d" % (done, len(files)), flush=True)

    by_group = defaultdict(lambda: {"seat": [], "win": [], "round": []})
    for r in all_seat:
        g = _group_of(r["name"], r["room_id"], recent)
        if g:
            by_group[g]["seat"].append(r)
    for r in all_win:
        g = _group_of(r["name"], r["room_id"], recent)
        if g:
            by_group[g]["win"].append(r)
    for r in all_round:
        # 一局四个座位可能分属不同组（比如我们跟高手同桌）；round_rows 的
        # chi/peng/gang 是全桌合计，按"这一局里出现过的每个相关分组各记一次"
        # 处理——calibrate 的目标是"这组人打出来的对局长什么样"，同桌旁观
        # 不该被排除，但也不重复破坏"每局一条"的语义，故每个相关分组各自
        # 拿到一份这一局的合计计数。
        for name in set(r["names"]):
            g = _group_of(name, r["room_id"], recent)
            if g:
                by_group[g]["round"].append(r)
                break   # 每局只记一次，按座位 0 出现的第一个可识别分组归类，避免同局重复计数
    return by_group


# ---------------------------------------------------------------- arena2 自对局

def _scan_arena2(seeds):
    seat_rows, win_rows, round_rows = [], [], []
    for seed in seeds:
        m = Arena2Match(seed, rounds=8).play()
        for rec in m.history:
            round_rows.append({"is_draw": rec["is_draw"],
                               "chi": sum(v for k, v in rec["claims_per_seat"].items() if k.endswith("_chi")),
                               "peng": sum(v for k, v in rec["claims_per_seat"].items() if k.endswith("_peng")),
                               "gang": sum(v for k, v in rec["claims_per_seat"].items() if k.endswith("_gang"))})
            for s in range(4):
                is_winner = rec["winner"] == s
                seat_rows.append({"dealer": s == rec["dealer"], "is_winner": is_winner,
                                  "total_draws": rec["winner_draw_index"] if is_winner else None})
            if not rec["is_draw"] and rec["winner"] is not None:
                c = rec["cats"]
                win_rows.append({"fan": rec["fan"], "meld_count": rec["winner_meld_count"],
                                 "cats": {"baotou": c.get("baotou", False), "qidui": c.get("qidui", False),
                                         "piao": c.get("piao_chain", False), "gang_kai": c.get("gang_kai", False),
                                         "four_white": c.get("four_white", False)}})
    return {"seat": seat_rows, "win": win_rows, "round": round_rows}


# ---------------------------------------------------------------- 统计

def _cumulative_hu_curve(seat_rows, dealer_flag):
    rows = [r for r in seat_rows if r["dealer"] == dealer_flag]
    n = len(rows)
    out = {}
    for cp in DRAW_CHECKPOINTS:
        won_by = sum(1 for r in rows if r["is_winner"] and r["total_draws"] is not None and r["total_draws"] <= cp)
        p = won_by / n if n else 0.0
        out[cp] = (p, _wilson_normal_ci(p, n), n)
    return out


def _fan_bucket(fan):
    if fan is None:
        return None
    for b in (1, 2, 4, 8):
        if fan == b:
            return str(b)
    return "16+" if fan >= 16 else str(fan)


def _fan_distribution(win_rows):
    n = len(win_rows)
    counts = Counter(_fan_bucket(w["fan"]) for w in win_rows)
    out = {}
    for bucket in ("1", "2", "4", "8", "16+"):
        c = counts.get(bucket, 0)
        p = c / n if n else 0.0
        out[bucket] = (p, _wilson_normal_ci(p, n), n)
    return out


def _hu_category_ratio(win_rows):
    """分类在提取阶段就做好了（真实日志用服务端词表 ``_real_cats``，
    arena2 自对局用 ``tools/arena2.py::_win_category`` 的本地词表分类，
    见模块 docstring），这里只按 ``w["cats"]`` 统一算比例。"""
    n = len(win_rows)
    out = {}
    for key in ("baotou", "qidui", "piao", "gang_kai", "four_white"):
        c = sum(1 for w in win_rows if w["cats"][key])
        p = c / n if n else 0.0
        out[key] = (p, _wilson_normal_ci(p, n), n)
    return out


def _claims_per_round(round_rows):
    out = {}
    for key in ("chi", "peng", "gang"):
        vals = [r[key] for r in round_rows]
        n = len(vals)
        mean = sum(vals) / n if n else 0.0
        var = sum((v - mean) ** 2 for v in vals) / (n - 1) if n > 1 else 0.0
        se = math.sqrt(var / n) if n else 0.0
        out[key] = (mean, 1.96 * se, n)
    return out


def _draw_rate(round_rows):
    n = len(round_rows)
    c = sum(1 for r in round_rows if r["is_draw"])
    p = c / n if n else 0.0
    return (p, _wilson_normal_ci(p, n), n)


def summarize(data):
    return {
        "cumulative_hu_curve_dealer": _cumulative_hu_curve(data["seat"], True),
        "cumulative_hu_curve_nondealer": _cumulative_hu_curve(data["seat"], False),
        "fan_distribution": _fan_distribution(data["win"]),
        "hu_category_ratio": _hu_category_ratio(data["win"]),
        "claims_per_round": _claims_per_round(data["round"]),
        "draw_rate": _draw_rate(data["round"]),
        "n_rounds": len(data["round"]), "n_wins": len(data["win"]),
    }


def _print_summary(name, s):
    print("\n=== %s（局数=%d，胡牌数=%d） ===" % (name, s["n_rounds"], s["n_wins"]))
    print("  流局率 %.3f ± %.3f" % (s["draw_rate"][0], s["draw_rate"][1]))
    for key in ("chi", "peng", "gang"):
        m, half, n = s["claims_per_round"][key]
        print("  每局%s次数 %.3f ± %.3f (n=%d)" % (key, m, half, n))
    print("  累计胡牌率(庄) 第6/10/14摸: %s" % ", ".join(
        "%.3f±%.3f" % (s["cumulative_hu_curve_dealer"][cp][0], s["cumulative_hu_curve_dealer"][cp][1])
        for cp in (6, 10, 14)))
    print("  累计胡牌率(闲) 第6/10/14摸: %s" % ", ".join(
        "%.3f±%.3f" % (s["cumulative_hu_curve_nondealer"][cp][0], s["cumulative_hu_curve_nondealer"][cp][1])
        for cp in (6, 10, 14)))
    fan_str = "  ".join("%s:%.2f" % (b, s["fan_distribution"][b][0]) for b in ("1", "2", "4", "8", "16+"))
    print("  番数分布 %s" % fan_str)
    cat_str = "  ".join("%s:%.2f" % (k, s["hu_category_ratio"][k][0]) for k in
                        ("baotou", "qidui", "piao", "gang_kai", "four_white"))
    print("  占胡比例 %s" % cat_str)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-09-25T15:30")
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--arena-seeds", type=int, default=5, help="arena2 自对局跑几个种子（每个 8 局）")
    args = ap.parse_args()

    print("扫描真实日志……")
    by_group = load_real(args)
    print("跑 arena2 自对局 %d 个种子……" % args.arena_seeds)
    arena_data = _scan_arena2(range(args.arena_seeds))

    result = {"arena2自对局": summarize(arena_data)}
    for g in ("高手", "我们(当前)", "我们(以前)"):
        if by_group.get(g) and by_group[g]["round"]:
            result[g] = summarize(by_group[g])

    os.makedirs(os.path.join(ROOT, "reports"), exist_ok=True)
    out_path = os.path.join(ROOT, "reports", "sim_calibrate.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)

    for name, s in result.items():
        _print_summary(name, s)
    print("\n完整机器可读结果见 %s（阶段二 tools/mc_check.py calibrate 的拟合标靶）" % out_path)


if __name__ == "__main__":
    main()
