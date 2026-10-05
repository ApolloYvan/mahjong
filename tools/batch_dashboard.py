"""P0 一键体检：docs/IMPL_UNIFIED_EV_V2.md 里后续所有验收都靠它。

    python3 tools/batch_dashboard.py --since 2026-09-27T16:19
    python3 tools/batch_dashboard.py --since 2026-09-27T16:19 --exclude a_81df00ba3d44

输出「我们(当前) / 我们(以前) / 高手」三组，并把「我们(当前)」再拆「桌上有
强手 / 无强手」，逐项列出：1.结果 2.速度 3.大牌 4.财神/爆头 5.吃碰 6.稳定性。
每项旁边给出 我们(当前)−高手 的差。复用 mining_common/baotou_funnel 的重放
基础设施，不改动它们；只做只读统计，不跑任何耗 CPU 的线上逻辑。
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

from mining_common import discover_files, merge_rounds  # noqa: E402
from mining_common import seat_names  # noqa: E402
from baotou_funnel import MASTERS as _BASE_MASTERS, OUR, recent_rooms  # noqa: E402
from mj.shanten import combined_route, route_shanten  # noqa: E402
from mj.rules import baotou as is_baotou  # noqa: E402
from mj.tiles import INDEX_TILE, TILE_INDEX, to_counts  # noqa: E402

JOKER = "白"
RESPONSE_TYPES = ("pass", "timeout", "chi", "peng", "gang")
CACHE_PATH = os.path.join(ROOT, "tools", ".cache", "batch_dashboard.pkl")


def _accepted_in_window(events, i, seat, action):
    """服务端先开碰窗口（所有人）再开吃窗口（下家），显式 pass 不带窗口信息，
    accept 动作不一定紧跟在 tile_discarded 后面——同 tools/dealer_study.py，
    向后扫描直到第一个不是响应类型的事件为止。"""
    for ev2 in events[i + 1:i + 13]:
        if ev2["type"] not in RESPONSE_TYPES:
            break
        if ev2["type"] == action and ev2.get("seat") == seat:
            return True
    return False
# 强手名单：baotou_funnel.MASTERS 之外，再加任务书里点名的几位。
MASTERS = _BASE_MASTERS | {"我胡汉三又回来了", "三杯猫", "玄武-2346", "腾蛇-0638", "放假了偷偷练"}

_RECENT = set()
_EXCLUDE = set()


def _init(recent, exclude):
    _RECENT.update(recent)
    _EXCLUDE.update(exclude)


def _sh(tiles, melds):
    return route_shanten(tuple(to_counts(tiles)), melds)


def _chi_takes(hand, tile):
    i = TILE_INDEX.get(tile)
    if i is None or i >= 27:
        return []
    out = []
    for offs in ((-2, -1), (-1, 1), (1, 2)):
        idx = [i + o for o in offs]
        if min(idx) < 0 or max(idx) >= 27 or any(k // 9 != i // 9 for k in idx):
            continue
        names = [INDEX_TILE[k] for k in idx]
        if all(hand.count(n) for n in names):
            out.append(names)
    return out


def _after_claim_shanten(hand, take, melds):
    rest = list(hand)
    for t in take:
        rest.remove(t)
    best = 9
    for d in set(rest):
        left = list(rest)
        left.remove(d)
        best = min(best, _sh(left, melds + 1))
    return best


def _tag(x):
    if "飘" in x:
        return "财飘"
    if "七对" in x:
        return "七对"
    for t in ("爆头", "杠开", "4个白板"):
        if t in x:
            return t
    return None


def scan(path):
    stats = defaultdict(Counter)   # key = (group, cell)
    try:
        with open(path, encoding="utf-8") as source:
            game = json.load(source)
    except (OSError, ValueError):
        return stats
    names = seat_names(game)
    if len(names) != 4:
        return stats
    room = game.get("room_id") or ""
    if room in _EXCLUDE:
        return stats
    groups = {}
    for s, n in enumerate(names):
        if n in MASTERS:
            groups[s] = "高手"
        elif n == OUR:
            groups[s] = "我们(当前)" if room in _RECENT else "我们(以前)"
    if not groups:
        return stats
    has_master = any(n in MASTERS for n in names)
    info = {r.get("round_no"): r for r in game.get("rounds") or []}
    for rnd in merge_rounds(game):
        meta = info.get(rnd["round_no"])
        hands = rnd.get("start_hands")
        if not meta or not hands or len(hands) != 4 or rnd.get("truncated") or not all(hands):
            continue
        dealer = meta.get("dealer", rnd.get("dealer"))
        scores = meta.get("scores") or [0] * 4
        winner = None if meta.get("is_draw") else meta.get("winner")
        win_detail = []
        cur_hand = [list(h) for h in hands]
        melds = [0, 0, 0, 0]
        draws = [0] * 4
        first_tenpai = {}
        win_draw = {}
        tenpai_wait = {}
        timeouts_discard = Counter()
        # 吃碰窗口统计：本方/高手在能碰/能吃时是否接受，按"向听是否变好"拆
        claim_offer_n = Counter()
        claim_accept_n = Counter()
        claim_improve_offer = Counter()
        claim_improve_accept = Counter()
        try:
            events = rnd["events"]
            for i, ev in enumerate(events):
                kind, seat, tile = ev["type"], ev.get("seat"), ev.get("tile")
                data = ev.get("data") or {}
                if kind == "tile_drawn":
                    cur_hand[seat].append(tile)
                    if seat in groups:
                        draws[seat] += 1
                elif kind == "timeout" and data.get("kind") == "discard" and seat in groups:
                    timeouts_discard[seat] += 1
                elif kind == "tile_discarded":
                    discarder_hand = cur_hand[seat]
                    for other in range(4):
                        if other == seat or other not in groups:
                            continue
                        g = groups[other]
                        oh = cur_hand[other]
                        if oh.count(tile) >= 2:
                            key = (g, "peng")
                            claim_offer_n[key] += 1
                            before = _sh(oh, melds[other])
                            after = _after_claim_shanten(oh, (tile, tile), melds[other])
                            improves = after < before
                            if improves:
                                claim_improve_offer[key] += 1
                            accepted = _accepted_in_window(events, i, other, "peng")
                            if accepted:
                                claim_accept_n[key] += 1
                                if improves:
                                    claim_improve_accept[key] += 1
                        if other == (seat + 1) % 4:
                            for take in _chi_takes(oh, tile):
                                key = (g, "chi")
                                claim_offer_n[key] += 1
                                before = _sh(oh, melds[other])
                                after = _after_claim_shanten(oh, tuple(take), melds[other])
                                improves = after < before
                                if improves:
                                    claim_improve_offer[key] += 1
                                accepted = _accepted_in_window(events, i, other, "chi")
                                if accepted:
                                    claim_accept_n[key] += 1
                                    if improves:
                                        claim_improve_accept[key] += 1
                                break
                    discarder_hand.remove(tile)
                    if seat in groups and len(discarder_hand) + 3 * melds[seat] == 13:
                        counts = tuple(to_counts(discarder_hand))
                        tenpai = route_shanten(counts, melds[seat]) == 0
                        if tenpai and seat not in first_tenpai:
                            first_tenpai[seat] = draws[seat]
                        if tenpai:
                            _cur, waits = combined_route(counts, melds[seat])
                            if is_baotou(counts, melds[seat]):
                                tenpai_wait[seat] = 34
                            else:
                                tenpai_wait[seat] = len(waits)
                elif kind == "chi":
                    used = list(data.get("tiles") or [])
                    used.remove(tile)
                    for t in used:
                        cur_hand[seat].remove(t)
                    melds[seat] += 1
                elif kind == "peng":
                    for _ in range(2):
                        cur_hand[seat].remove(tile)
                    melds[seat] += 1
                elif kind == "gang":
                    take = {"an": 4, "ming": 3, "bu": 1}[data.get("kind")]
                    for _ in range(take):
                        cur_hand[seat].remove(tile)
                    if data.get("kind") != "bu":
                        melds[seat] += 1
                elif kind == "round_ended":
                    win_detail = data.get("detail") or []
                    if winner is not None and winner in groups:
                        win_draw[winner] = draws[winner]
        except (ValueError, KeyError, IndexError, TypeError):
            continue

        tags = Counter()
        for x in win_detail:
            t = _tag(x)
            if t:
                tags[t] += 1
        win_fan = None
        for ev in rnd["events"]:
            if ev.get("type") == "round_ended":
                d = ev.get("data") or {}
                if not d.get("draw") and d.get("fan"):
                    win_fan = d["fan"]

        for s, g in groups.items():
            jokers = min(hands[s].count(JOKER), 2)
            is_d = s == dealer
            for cell_g in ((g, "全部"), (g, "庄" if is_d else "闲")):
                x = stats[cell_g]
                x["n"] += 1
                x["score"] += scores[s]
                won = winner == s
                x["won"] += won
                x["won_dealer"] += won and is_d
                x["won_idle"] += won and not is_d
                x["n_dealer"] += is_d
                x["n_idle"] += not is_d
                x["score_dealer"] += scores[s] if is_d else 0
                x["score_idle"] += scores[s] if not is_d else 0
                if s in first_tenpai:
                    x["first_tenpai_draw"] += first_tenpai[s]
                    x["first_tenpai_n"] += 1
                if won and s in win_draw:
                    x["win_draw"] += win_draw[s]
                    x["win_draw_n"] += 1
                    if s in first_tenpai:
                        x["tenpai_to_win"] += win_draw[s] - first_tenpai[s]
                        x["tenpai_to_win_n"] += 1
                if s in tenpai_wait:
                    x["tenpai_wait_sum"] += tenpai_wait[s]
                    x["tenpai_wait_n"] += 1
                if won and win_fan is not None:
                    x["win_fan_sum"] += win_fan
                    x["win_fan_n"] += 1
                    x["win_fan4"] += win_fan >= 4
                    for t, k in tags.items():
                        x["tag_" + t] += k
                x["timeouts_discard"] += timeouts_discard.get(s, 0)
            jcell = (g, "joker%d" % jokers)
            jx = stats[jcell]
            jx["n"] += 1
            if won and win_fan is not None:
                jx["won_bt"] += "爆头" in win_detail
                jx["won"] += 1
            if g == "我们(当前)":
                hs_cell = ("我们(当前,有强手)" if has_master else "我们(当前,无强手)", "全部")
                hx = stats[hs_cell]
                hx["n"] += 1
                hx["won"] += winner == s
                hx["score"] += scores[s]

        for g in set(groups.values()):
            for kind_ in ("peng", "chi"):
                key = (g, kind_)
                x = stats[(g, "claim_" + kind_)]
                x["offer"] += claim_offer_n[key]
                x["accept"] += claim_accept_n[key]
                x["offer_improve"] += claim_improve_offer[key]
                x["accept_improve"] += claim_improve_accept[key]
                x["offer_flat"] += claim_offer_n[key] - claim_improve_offer[key]
                x["accept_flat"] += claim_accept_n[key] - claim_improve_accept[key]
    return stats


def _scan_path(path):
    return path, scan(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-09-25T15:30")
    ap.add_argument("--exclude", nargs="*", default=[])
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--no-cache", action="store_true", help="忽略并不写缓存，全量重扫")
    args = ap.parse_args()
    recent = recent_rooms(args.since)
    exclude = set(args.exclude)
    files = discover_files()
    total = defaultdict(Counter)

    # 增量缓存：每个文件的扫描结果只取决于 文件内容 + 它是否属于「当前」/排除 + 本脚本代码，
    # 三者都没变就直接复用，只扫新文件或有变化的文件。
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
        room = "_".join(os.path.basename(path).split("_")[:2])
        return (st.st_mtime, st.st_size, room in recent, room in exclude)

    todo, keys = [], {}
    for path in files:
        k = file_key(path)
        keys[path] = k
        hit = cache.get(path)
        if hit and hit[0] == k:
            for key, c in hit[1].items():
                total[key].update(c)
        else:
            todo.append(path)
    print("  缓存命中 %d 个文件，需要扫描 %d 个" % (len(files) - len(todo), len(todo)), flush=True)

    fresh = {}
    if todo:
        with Pool(args.jobs, initializer=_init, initargs=(recent, exclude)) as pool:
            for done, (path, part) in enumerate(pool.imap_unordered(_scan_path, todo, chunksize=8), 1):
                plain = {key: dict(c) for key, c in part.items()}
                fresh[path] = (keys[path], plain)
                for key, c in plain.items():
                    total[key].update(c)
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

    groups = ("我们(当前)", "我们(以前)", "高手")

    def g(name, cell):
        return total.get((name, cell))

    print("\n=== 1. 结果 ===")
    print("%-12s %8s %8s %7s %7s | %7s %7s %7s %7s" % (
        "组", "局数", "分/局", "胜率", "庄局", "庄胜率", "庄分/局", "闲胜率", "闲分/局"))
    for name in groups:
        x = g(name, "全部")
        if not x or x["n"] < 5:
            continue
        n = x["n"]
        print("%-12s %8d %8.2f %6.1f%% %7d | %6.1f%% %7.2f %6.1f%% %7.2f" % (
            name, n, x["score"] / n, 100 * x["won"] / n, x["n_dealer"],
            100 * x["won_dealer"] / x["n_dealer"] if x["n_dealer"] else 0,
            x["score_dealer"] / x["n_dealer"] if x["n_dealer"] else 0,
            100 * x["won_idle"] / x["n_idle"] if x["n_idle"] else 0,
            x["score_idle"] / x["n_idle"] if x["n_idle"] else 0))
    for extra in ("我们(当前,有强手)", "我们(当前,无强手)"):
        x = g(extra, "全部")
        if x and x["n"] >= 5:
            n = x["n"]
            print("  %-10s %8d %8.2f %6.1f%%" % (extra, n, x["score"] / n, 100 * x["won"] / n))

    print("\n=== 2. 速度 ===")
    print("%-12s %10s %10s %12s %10s" % ("组", "首次听牌手", "胡牌手", "听牌->胡摸数", "听口活牌数"))
    for name in groups:
        x = g(name, "全部")
        if not x:
            continue
        ft = x["first_tenpai_draw"] / x["first_tenpai_n"] if x["first_tenpai_n"] else None
        wd = x["win_draw"] / x["win_draw_n"] if x["win_draw_n"] else None
        gap = x["tenpai_to_win"] / x["tenpai_to_win_n"] if x["tenpai_to_win_n"] else None
        wait = x["tenpai_wait_sum"] / x["tenpai_wait_n"] if x["tenpai_wait_n"] else None
        print("%-12s %10s %10s %12s %10s" % (
            name, "%.2f" % ft if ft else "-", "%.2f" % wd if wd else "-",
            "%.2f" % gap if gap else "-", "%.2f" % wait if wait else "-"))

    print("\n=== 3. 大牌（胡牌口径） ===")
    print("%-12s %8s %8s %7s | " % ("组", "胡次", "平均番", "4番+"), "  ".join(
        "%6s" % t for t in ("爆头", "财飘", "杠开", "七对", "4白")))
    for name in groups:
        x = g(name, "全部")
        if not x or not x["win_fan_n"]:
            continue
        n = x["win_fan_n"]
        print("%-12s %8d %8.2f %6.1f%% | " % (name, n, x["win_fan_sum"] / n, 100 * x["win_fan4"] / n),
             "  ".join("%5.1f%%" % (100 * x.get("tag_" + t, 0) / n)
                       for t in ("爆头", "财飘", "杠开", "七对", "4个白板")))

    print("\n=== 4. 财神/爆头（按起手财神数分格） ===")
    print("%-12s %-8s %8s %10s" % ("组", "财神数", "局数", "爆头占胡"))
    for name in groups:
        for j in (0, 1, 2):
            x = g(name, "joker%d" % j)
            if not x or x["n"] < 10:
                continue
            label = "2+" if j == 2 else str(j)
            won = x["won"] or 1
            print("%-12s %-8s %8d %9.1f%%" % (name, label, x["n"], 100 * x.get("won_bt", 0) / won))

    print("\n=== 5. 吃碰接受率（能碰/能吃时是否接受，按向听是否变好拆） ===")
    print("%-12s %-6s %8s %8s | %8s %8s" % ("组", "类型", "机会数", "接受率", "改善接受率", "不变接受率"))
    for name in groups:
        for kind_ in ("peng", "chi"):
            x = g(name, "claim_" + kind_)
            if not x or x["offer"] < 10:
                continue
            improve_rate = 100 * x["accept_improve"] / x["offer_improve"] if x["offer_improve"] else 0
            flat_rate = 100 * x["accept_flat"] / x["offer_flat"] if x["offer_flat"] else 0
            print("%-12s %-6s %8d %7.1f%% | %7.1f%% %7.1f%%" % (
                name, kind_, x["offer"], 100 * x["accept"] / x["offer"], improve_rate, flat_rate))

    print("\n=== 6. 稳定性 ===")
    for name in groups:
        x = g(name, "全部")
        if x:
            print("%-12s discard 超时次数 %d（%d 局）" % (name, x["timeouts_discard"], x["n"]))

    m = g("高手", "全部")
    o = g("我们(当前)", "全部")
    if m and o and m["n"] and o["n"]:
        print("\n=== 我们(当前) − 高手 ===")
        print("分/局差: %+.2f  胜率差: %+.1fpp" % (
            o["score"] / o["n"] - m["score"] / m["n"], 100 * (o["won"] / o["n"] - m["won"] / m["n"])))


if __name__ == "__main__":
    main()
