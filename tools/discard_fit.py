"""一次拟合三类决策的全部权重：让我们的评分在高手的真实局面上尽量做出和高手一样的选择。

    python3 tools/discard_fit.py extract                                   # 全部四类样本
    python3 tools/discard_fit.py extract --kinds dj cp cc --out data/analysis/fit_more.jsonl
    python3 tools/discard_fit.py fit --inp data/analysis/discard_fit.jsonl data/analysis/fit_more.jsonl

样本类型（每行 k 字段）：
    d0  无财神弃牌   特征 mj/discard_features.FD_FEATURES   权重前缀 fd_   开关 rule_fitted_discard_enabled
    dj  持财神弃牌   特征 mj/discard_features.J_FEATURES    权重前缀 fj_   开关 rule_fitted_discard_joker_enabled
    cp  碰窗口       特征 mj/claim_features.CL_FEATURES     权重前缀 fc_   开关 rule_fitted_claim_enabled
    cc  吃窗口       （与 cp 共用一组 fc_ 权重，一起拟合）
线上评分与这里的特征是同一份代码，拟合出的权重可以直接写进 weights.json。

方法（条件 logit）：每个决策点 = 若干选项，每个选项一组特征；求权重使
    P(高手选这个) = exp(w·x) / Σ exp(w·x')
的对数似然最大。弃牌取最高分、缩放无关，换算成"进张 1 种 = 100"；吃碰以「过」为 0 分，
换算成 ×100。

弃牌只看：不在财飘链上、不是抓打圈、高手的弃牌落在我们的同向听层里（层外跳过并计数）。
吃碰只看：服务端确有该座位的 碰/吃 事件或显式 pass 的窗口（超时代打、被别家抢先而
本人未表态的窗口跳过）；抓打圈窗口跳过；明杠不在选项里。
按对局文件 8:2 切训练/测试；准确率全部在测试集上报告。
「我们」的样本只做校验：现行策略在我们自己真实决策上的一致率应该很高，否则说明
特征提取/重放和线上不一致，拟合结果不可信。
"""
import argparse
import hashlib
import json
import math
import os
import sys
from collections import Counter, defaultdict
from multiprocessing import Pool

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from mining_common import discover_files, merge_rounds  # noqa: E402
from mining_common import seat_names  # noqa: E402
from mj.claim_features import CL_FEATURES, option_features  # noqa: E402
import mj.discard_features  # noqa: E402
from mj.discard_features import FD_FEATURES, J_FEATURES, defaults, features, features_joker  # noqa: E402
from mj.ev import choose_route_discard  # noqa: E402
from mj.fit import load_weights  # noqa: E402
from mj.responses import choose_chi, choose_peng  # noqa: E402
from mj.shanten import route_shanten  # noqa: E402
from mj.strategy import choose_discard  # noqa: E402
from mj.tiles import INDEX_TILE, TILE_INDEX, to_counts  # noqa: E402

mj.discard_features.EXTRACT_ALL = True
OUR = "重生之我是雀神"
JOKER = "白"
MASTERS = ["⭐꧁༺🀆🀆🀆🀆༻꧂⭐", "Deepseek胡", "爆头研究所", "Astra-0", "晴总总，该请桂语山房了",
           "glm-flash", "铳一色14", "歪比巴卜肉蛋葱鸡", "放假了偷偷训练", "康陶应雀"]
OUT = "data/analysis/discard_fit.jsonl"
RESPONSE_TYPES = ("pass", "timeout", "chi", "peng", "gang")
FEATS = {"d0": FD_FEATURES, "dj": J_FEATURES, "cl": CL_FEATURES}
PREFIX = {"d0": "fd_", "dj": "fj_", "cl": "fc_"}
SWITCH = {"d0": "rule_fitted_discard_enabled", "dj": "rule_fitted_discard_joker_enabled",
          "cl": "rule_fitted_claim_enabled"}
TITLE = {"d0": "无财神弃牌", "dj": "持财神弃牌", "cl": "吃碰"}

_STATE = {}


def _init(masters, ours_sample, kinds):
    _STATE["masters"] = set(masters)
    _STATE["ours_sample"] = ours_sample
    _STATE["kinds"] = set(kinds)
    _STATE["weights"] = load_weights()


def _keep_ours(key, rate):
    return int(hashlib.md5(key.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF < rate


def _chi_options(hand, tile):
    """与 mj.responses.choose_chi 的候选枚举一致。"""
    index = TILE_INDEX.get(tile)
    if index is None or index >= 27:
        return []
    out = []
    for offsets in ((-2, -1), (-1, 1), (1, 2)):
        idxs = [index + o for o in offsets]
        if min(idxs) < 0 or max(idxs) >= 27 or any(i // 9 != index // 9 for i in idxs):
            continue
        names = [INDEX_TILE[i] for i in idxs]
        if all(hand.count(n) for n in names):
            out.append(names)
    return out


def extract_file(path):
    masters, rate, kinds, weights = (_STATE["masters"], _STATE["ours_sample"], _STATE["kinds"],
                                     _STATE["weights"])
    stats = Counter()
    try:
        with open(path, encoding="utf-8") as source:
            game = json.load(source)
    except (OSError, ValueError):
        return [], stats
    names = seat_names(game)
    if len(names) != 4 or not any(n == OUR or n in masters for n in names):
        return [], stats
    base = os.path.basename(path)
    info = {r.get("round_no"): r for r in game.get("rounds") or []}

    def group_of(seat, key):
        name = names[seat]
        if name in masters:
            return "master"
        if name == OUR and _keep_ours(key, rate):
            return "ours"
        return None

    rows = []
    for rnd in merge_rounds(game):
        meta = info.get(rnd["round_no"])
        if not meta or rnd.get("truncated") or not rnd.get("start_hands"):
            continue
        dealer = meta.get("dealer", rnd.get("dealer"))
        hands = [list(h) for h in rnd["start_hands"]]
        total_start = sum(len(h) for h in hands)
        drawn = 0
        melds = [0] * 4
        chis = [0] * 4
        seen = [0] * 34
        piaoed = [False] * 4
        events = rnd["events"]
        try:
            for idx, ev in enumerate(events):
                kind, seat, tile = ev["type"], ev.get("seat"), ev.get("tile")
                data = ev.get("data") or {}
                if kind == "tile_drawn":
                    hands[seat].append(tile)
                    drawn += 1
                elif kind == "tile_discarded":
                    hand = hands[seat]
                    catch = bool(data.get("catch_play"))
                    has_joker = JOKER in hand
                    k = "dj" if has_joker else "d0"
                    grp = group_of(seat, "%s|%s" % (base, ev.get("seq")))
                    if (grp and k in kinds and not piaoed[seat] and not catch
                            and len(hand) + 3 * melds[seat] == 14):
                        stats["%s_%s_points" % (grp, k)] += 1
                        row = _discard_point(k, hand, tile, melds[seat], seen, weights, seat == dealer)
                        if row is None:
                            stats["%s_%s_outtier" % (grp, k)] += 1
                        elif "single" in row:
                            stats["single_candidate"] += 1
                        else:
                            row.update(k=k, g=grp, p=names[seat], f=base, d=int(seat == dealer))
                            rows.append(row)
                    hand.remove(tile)
                    seen[TILE_INDEX[tile]] += 1
                    if tile == JOKER:
                        piaoed[seat] = True
                    if tile != JOKER and not catch and kinds & {"cp", "cc"}:
                        wall = 136 - total_start - drawn
                        rows += _claim_windows(events, idx, seat, tile, hands, melds, chis, dealer, wall,
                                               names, base, group_of, kinds, stats)
                elif kind == "chi":
                    used = list(data.get("tiles") or [])
                    used.remove(tile)
                    for t in used:
                        hands[seat].remove(t)
                        seen[TILE_INDEX[t]] += 1
                    melds[seat] += 1
                    chis[seat] += 1
                elif kind == "peng":
                    for _ in range(2):
                        hands[seat].remove(tile)
                    seen[TILE_INDEX[tile]] += 2
                    melds[seat] += 1
                elif kind == "gang":
                    take = {"an": 4, "ming": 3, "bu": 1}[data.get("kind")]
                    for _ in range(take):
                        hands[seat].remove(tile)
                    seen[TILE_INDEX[tile]] += take
                    if data.get("kind") != "bu":
                        melds[seat] += 1
        except (ValueError, KeyError, IndexError, TypeError):
            stats["bad_round"] += 1
            continue
    return rows, stats


def _discard_point(k, hand, actual, groups, seen, weights, is_dealer):
    unique = sorted(set(hand))
    currents = {t: route_shanten(_counts_without(hand, t), groups) for t in unique}
    best = min(currents.values())
    tier = [t for t in unique if currents[t] == best]
    if actual not in tier:
        return None
    if len(tier) == 1:
        return {"single": 1}
    own = to_counts(hand)
    visible = [min(4, seen[i] + own[i]) for i in range(34)]
    if k == "d0":
        X = [[features(hand, t, groups, visible, weights)[n] for n in FD_FEATURES] for t in tier]
        return {"c": tier, "X": X, "y": tier.index(actual)}
    X = [[features_joker(hand, t, groups, visible, weights)[n] for n in J_FEATURES] for t in tier]
    pick = (choose_route_discard(hand, groups, 0, 0, {}, visible) if is_dealer
            else choose_discard(hand, groups, 0, 0, {}, visible))
    return {"c": tier, "X": X, "y": tier.index(actual), "cur": tier.index(pick) if pick in tier else -1}


def _counts_without(hand, tile):
    rest = list(hand)
    rest.remove(tile)
    return to_counts(rest)


def _claim_windows(events, idx, disc_seat, tile, hands, melds, chis, dealer, wall, names, base,
                   group_of, kinds, stats):
    window = []
    for ev2 in events[idx + 1:idx + 13]:
        if ev2["type"] not in RESPONSE_TYPES:
            break
        window.append(ev2)
    accepted = next(((e.get("seat"), e["type"], e.get("data") or {}) for e in window
                     if e["type"] in ("chi", "peng", "gang")), None)
    passed = {e.get("seat") for e in window if e["type"] == "pass"}
    out = []

    def decided(r, mine):
        """返回 (是否可用, 是否接受)。"""
        if accepted and accepted[0] == r:
            return (accepted[1] in mine), accepted[1] in mine
        if r in passed:
            return True, False
        return False, False

    if "cp" in kinds:
        for r in range(4):
            if r == disc_seat or hands[r].count(tile) < 2:
                continue
            grp = group_of(r, "%s|%s|p%d" % (base, events[idx].get("seq"), r))
            if not grp:
                continue
            ok, took = decided(r, ("peng",))
            if not ok:
                stats["cp_unconfirmed"] += 1
                continue
            f = option_features(hands[r], (tile, tile), melds[r], chis[r], wall, r == dealer)
            if f is None:
                continue
            snap = {"my_hand": list(hands[r]), "melds": [[{"kind": "x"}] * melds[r] if s == r else []
                                                         for s in range(4)],
                    "seat": r, "window_tile": tile, "wall_remaining": wall, "dealer": dealer,
                    "phase": "response_peng"}
            cur = 1 if choose_peng(snap) else 0
            out.append({"k": "cp", "g": grp, "p": names[r], "f": base, "d": int(r == dealer),
                        "X": [[0.0] * len(CL_FEATURES), [f[n] for n in CL_FEATURES]],
                        "y": int(took), "cur": cur})
    if "cc" in kinds:
        r = (disc_seat + 1) % 4
        opts = _chi_options(hands[r], tile) if chis[r] < 2 else []
        grp = group_of(r, "%s|%s|c" % (base, events[idx].get("seq"))) if opts else None
        if grp:
            ok, took = decided(r, ("chi",))
            if accepted and accepted[0] == r and accepted[1] != "chi":
                ok = False
            if not ok:
                stats["cc_unconfirmed"] += 1
                return out
            X, labels = [[0.0] * len(CL_FEATURES)], [None]
            for names_ in opts:
                f = option_features(hands[r], tuple(names_), melds[r], chis[r], wall, r == dealer)
                if f is not None:
                    X.append([f[n] for n in CL_FEATURES])
                    labels.append(sorted(names_))
            if len(X) == 1:
                return out
            y = 0
            if took:
                used = list(accepted[2].get("tiles") or [])
                if tile in used:
                    used.remove(tile)
                if sorted(used) not in labels:
                    stats["cc_combo_unmatched"] += 1
                    return out
                y = labels.index(sorted(used))
            snap = {"my_hand": list(hands[r]),
                    "melds": [[{"kind": "chi"}] * chis[r] + [{"kind": "x"}] * (melds[r] - chis[r])
                              if s == r else [] for s in range(4)],
                    "seat": r, "window_tile": tile, "wall_remaining": wall, "dealer": dealer,
                    "phase": "response_chi"}
            pick = choose_chi(snap)
            cur = labels.index(sorted(pick["tiles"])) if pick and sorted(pick["tiles"]) in labels else 0
            out.append({"k": "cc", "g": grp, "p": names[r], "f": base, "d": int(r == dealer),
                        "X": X, "y": y, "cur": cur})
    return out


def cmd_extract(args):
    files = discover_files()
    total = Counter()
    per_kind = Counter()
    n = 0
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as sink, \
            Pool(args.jobs, initializer=_init, initargs=(args.players, args.ours_sample, args.kinds)) as pool:
        for done, (rows, stats) in enumerate(pool.imap_unordered(extract_file, files, chunksize=4), 1):
            total.update(stats)
            for row in rows:
                sink.write(json.dumps(row, ensure_ascii=False) + "\n")
                per_kind["%s/%s" % (row["k"], row["g"])] += 1
                n += 1
            if done % 100 == 0 or done == len(files):
                print("  进度 %d / %d 个文件，已写 %d 个决策点" % (done, len(files), n), flush=True)
    print("\n已写出 %s（%d 行）" % (args.out, n))
    for key in sorted(per_kind):
        print("  %-12s %d" % (key, per_kind[key]))
    for k in ("d0", "dj"):
        pts, out_ = total["master_%s_points" % k], total["master_%s_outtier" % k]
        if pts:
            print("高手 %s 弃牌点 %d，层外跳过 %d（%.1f%%）" % (TITLE[k], pts, out_, 100 * out_ / pts))
    print("只有一个候选 %d；吃碰窗口无法确认本人决策而跳过：碰 %d / 吃 %d；吃法对不上 %d；坏局 %d"
          % (total["single_candidate"], total["cp_unconfirmed"], total["cc_unconfirmed"],
             total["cc_combo_unmatched"], total["bad_round"]))


# ---------------------------------------------------------------- 拟合（纯 Python 条件 logit / 牛顿法）

def _solve(A, b):
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(M[r][c]))
        M[c], M[piv] = M[piv], M[c]
        d = M[c][c]
        if abs(d) < 1e-12:
            continue
        for j in range(c, n + 1):
            M[c][j] /= d
        for r in range(n):
            if r != c and M[r][c]:
                k = M[r][c]
                for j in range(c, n + 1):
                    M[r][j] -= k * M[c][j]
    return [M[i][n] for i in range(n)]


def _inverse_diag(A):
    n = len(A)
    return [_solve(A, [1.0 if j == i else 0.0 for j in range(n)])[i] for i in range(n)]


def fit_logit(data, dim, ridge=1e-2, iters=30):
    w = [0.0] * dim
    H = None
    for it in range(iters):
        g = [0.0] * dim
        H = [[0.0] * dim for _ in range(dim)]
        ll = 0.0
        for X, y in data:
            s = [sum(wi * xi for wi, xi in zip(w, x)) for x in X]
            m = max(s)
            e = [math.exp(v - m) for v in s]
            z = sum(e)
            p = [v / z for v in e]
            ll += math.log(max(p[y], 1e-300))
            mu = [sum(p[k] * X[k][j] for k in range(len(X))) for j in range(dim)]
            xy = X[y]
            for j in range(dim):
                g[j] += xy[j] - mu[j]
            for k, x in enumerate(X):
                pk = p[k]
                if pk < 1e-12:
                    continue
                for a in range(dim):
                    xa = pk * x[a]
                    if xa:
                        row = H[a]
                        for b in range(dim):
                            row[b] += xa * x[b]
            for a in range(dim):
                ma = mu[a]
                if ma:
                    row = H[a]
                    for b in range(dim):
                        row[b] -= ma * mu[b]
        for j in range(dim):
            g[j] -= ridge * w[j]
            H[j][j] += ridge
        step = _solve(H, g)
        w = [wi + si for wi, si in zip(w, step)]
        print("  迭代 %2d  对数似然 %.1f  最大步长 %.2e" % (it + 1, ll, max(abs(s) for s in step)), flush=True)
        if max(abs(s) for s in step) < 1e-6:
            break
    return w, _inverse_diag(H)


def _pick(X, w):
    s = [sum(wi * xi for wi, xi in zip(w, x)) for x in X]
    return s.index(max(s))          # 平分取第一个，与线上 max() 一致；吃碰里第一个是「过」


def _acc(rows, w=None):
    """w=None 时用行里记录的现行策略选择 cur。"""
    if not rows:
        return float("nan")
    hit = sum((r["cur"] if w is None else _pick(r["X"], w)) == r["y"] for r in rows)
    return hit / len(rows)


MEANING = {
    "uke": "弃后≤2向听 进张/听口活度（每种）", "uke3": "弃后3向听 进张活度（每种）",
    "pair_far": "弃后≥2向听 每个对子", "pair_near": "弃后≤1向听 每个对子",
    "hon_iso": "打孤字（相对打中张）", "hon_pair": "拆字对（相对打中张）", "hon_trip": "拆字刻（相对打中张）",
    "term": "打幺九（相对打中张）", "e28": "打二八（相对打中张）",
    "isolated": "弃后每张孤张", "seen": "打出牌场上已见张数（每张）", "pair_route": "七对路线分（×现行值/100）",
    "bt_after": "弃后即爆头听牌", "bt_dist": "弃后离爆头还差几步（每步）", "plan_gain": "留白方案溢价（/100）",
    "mq_bt": "S7 门清爆头加分（/100）", "disc_joker": "打出财神（财飘）",
    "chi": "吃（相对过）", "peng": "碰（相对过）", "d_shanten": "声明后向听变化（每步，负=前进）",
    "d_uke": "声明后进张种数变化（每种）", "after_bt": "声明后成爆头听牌", "after_tenpai": "声明后听牌",
    "tenpai": "声明前已听牌", "first": "首副露（破门清）", "jokers": "手上财神数（每张）",
    "late": "墙 <30 张", "pair_route": "七对路线分（×现行值/100）", "chi2": "第二口吃（吃满）",
    "dealer": "坐庄",
}
MEANING["pair_route"] = "七对路线（弃牌：分/100；吃碰：手上有七对路线）"


def fit_kind(k, rows, ridge):
    names = FEATS[k]
    test_of = lambda r: int(hashlib.md5(r["f"].encode()).hexdigest()[:8], 16) % 5 == 0
    masters = [r for r in rows if r["g"] == "master"]
    ours = [r for r in rows if r["g"] == "ours"]
    train = [r for r in masters if not test_of(r)]
    test = [r for r in masters if test_of(r)]
    print("\n" + "=" * 90)
    print("【%s】高手样本：训练 %d / 测试 %d；我们（校验用）%d" % (TITLE[k], len(train), len(test), len(ours)))
    if len(train) < 200:
        print("样本太少，跳过")
        return None
    live = load_weights()
    if k == "d0":
        base = defaults(live)
        w_cur = [live.get("fd_" + n, base[n]) for n in names]
        cur_acc = lambda rs: _acc(rs, w_cur)
    else:
        w_cur = None
        cur_acc = lambda rs: _acc(rs)
    print("【校验】现行策略在我们自己真实决策上的一致率：%.1f%%（应明显高于在高手上的一致率）" % (100 * cur_acc(ours)))
    print("        现行策略在高手测试集上的一致率：%.1f%%" % (100 * cur_acc(test)))
    if k == "cl":
        for kk, label in (("cp", "碰"), ("cc", "吃")):
            tr = [r for r in masters if r["k"] == kk]
            ou = [r for r in ours if r["k"] == kk]
            if tr and ou:
                print("        %s窗口接受率：高手 %.1f%%（n=%d）  我们 %.1f%%（n=%d）" % (
                    label, 100 * sum(r["y"] > 0 for r in tr) / len(tr), len(tr),
                    100 * sum(r["y"] > 0 for r in ou) / len(ou), len(ou)))

    print("拟合中……")
    w, var = fit_logit([(r["X"], r["y"]) for r in train], len(names), ridge=ridge)
    if k == "cl":
        scale = 100.0
    else:
        iu = names.index("uke")
        if w[iu] <= 0:
            print("！进张权重拟合为非正，无法换算，本类结果不可上线。")
            return None
        scale = 100.0 / w[iu]
    fitted = [int(round(x * scale)) for x in w]
    print("【结果】拟合后在高手测试集上的一致率：%.1f%%（现行 %.1f%%）" % (100 * _acc(test, fitted), 100 * cur_acc(test)))
    print("\n%-12s %10s %10s %8s   %s" % ("特征", "现行权重", "拟合权重", "z值", "含义"))
    for j, n in enumerate(names):
        se = math.sqrt(max(var[j], 1e-18))
        cur_txt = "%10.0f" % w_cur[j] if w_cur else "%10s" % "-"
        print("%-12s %s %10d %8.1f   %s" % (n, cur_txt, fitted[j], w[j] / se, MEANING.get(n, "")))
    per = defaultdict(list)
    for r in test:
        per[r["p"]].append(r)
    print("\n每位高手（测试集）：现行一致率 → 拟合一致率")
    for name, rs in sorted(per.items(), key=lambda kv: -len(kv[1])):
        print("  %-24s n=%-5d %.1f%% → %.1f%%" % (name[:24], len(rs), 100 * cur_acc(rs), 100 * _acc(rs, fitted)))
    gain = _acc(test, fitted) - cur_acc(test)
    if gain < 0.01:
        print("\n→ 拟合只比现行高 %.1f 个百分点（门槛 1.0），本类不写入输出，保持现行策略。" % (100 * gain))
        return None
    out = {SWITCH[k]: 1}
    out.update({PREFIX[k] + n: v for n, v in zip(names, fitted)})
    return out


def cmd_fit(args):
    rows = defaultdict(list)
    for path in args.inp:
        with open(path, encoding="utf-8") as source:
            for line in source:
                r = json.loads(line)
                k = r.get("k", "d0")
                rows["cl" if k in ("cp", "cc") else k].append(r)
    merged = {}
    for k in ("d0", "dj", "cl"):
        if k in args.kinds and rows.get(k):
            out = fit_kind(k, rows[k], args.ridge)
            if out:
                merged.update(out)
    if not merged:
        print("没有可用结果")
        return
    print("\n" + "=" * 90)
    print("写进 models/weights.json 的 \"weights\" 里（与现有开关并列）：")
    print(json.dumps(merged, ensure_ascii=False))
    with open(args.save, "w", encoding="utf-8") as sink:
        json.dump(merged, sink, ensure_ascii=False, indent=1)
    print("（同时保存到 %s）" % args.save)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    ex = sub.add_parser("extract")
    ex.add_argument("--players", nargs="*", default=MASTERS)
    ex.add_argument("--ours-sample", type=float, default=0.1, help="我们的样本抽样比例（只做校验）")
    ex.add_argument("--kinds", nargs="*", default=["d0", "dj", "cp", "cc"])
    ex.add_argument("--jobs", type=int, default=6)
    ex.add_argument("--out", default=OUT)
    ft = sub.add_parser("fit")
    ft.add_argument("--inp", nargs="+", default=[OUT])
    ft.add_argument("--kinds", nargs="*", default=["d0", "dj", "cl"])
    ft.add_argument("--ridge", type=float, default=1e-2)
    ft.add_argument("--save", default="models/fit_weights.json")
    args = ap.parse_args()
    (cmd_extract if args.cmd == "extract" else cmd_fit)(args)


if __name__ == "__main__":
    main()
