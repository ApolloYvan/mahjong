"""爆头漏斗第 ② 环的决策点：手上有财神、这一摸能胡普通（非爆头）胡——胡掉，还是弃胡转爆头？

baotou_funnel（2026-10-07，30 房 v1.3）：差距集中在「2+ 财神」格——听牌后每摸一张转成爆头听
我们 13% vs 高手 21%（0 露）、16% vs 29%（2+ 露），「放弃自摸局%」12.8% vs 23.3%、11.1% vs 35.4%。
先试过「不能胡的那一摸有没有打成爆头」：机会几乎为 0、抓住率人人 100%——因为 13 张是爆头听
就意味着加上任何一张（包括刚摸的）都能胡，所以「转爆头」几乎只发生在「能胡却不胡」这一刻，
也就是 S1 管的决策。这里把高手比我们多弃的那部分拆到 S1 的三个条件上：
  直接可转 = 存在一张非财神牌，打掉后 13 张就是爆头听（S1 现行条件）
  墙尾     = 墙剩 −20 < 8（S1 现在这时不弃）
哪一格高手弃得多、我们弃得少，就是要改的那一条。1 财神格当对照。

    python3 tools/baotou_take_scan.py --since 2026-10-06T17:10 --jobs 6
"""
import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from multiprocessing import Pool

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "tools", "train"))
os.environ.setdefault("MJ_WEIGHTS_NO_FILE", "1")

from baotou_funnel import TOP_OOS, recent_rooms  # noqa: E402
from build_dataset import iter_decisions  # noqa: E402
from mining_common import OUR_NAME, discover_files, seat_names  # noqa: E402
from mj import bot  # noqa: E402
from mj.rules import baotou  # noqa: E402
from mj.tiles import JOKER, to_counts  # noqa: E402

GROUPS = ("高手", "我们(当前)", "我们(以前)")
_RECENT = set()


def _init(recent):
    _RECENT.update(recent)


def _group(name, room):
    if name in TOP_OOS:
        return "高手"
    if name == OUR_NAME:
        return "我们(当前)" if room in _RECENT else "我们(以前)"
    return None


def scan(path):
    out = []
    try:
        with open(path, encoding="utf-8") as source:
            game = json.load(source)
    except (OSError, ValueError):
        return out
    names = seat_names(game)
    room = game.get("room_id") or ""
    wanted = {n for n in names if _group(n, room)}
    if not wanted:
        return out
    for rec in iter_decisions(path, keep=lambda nm, ph, ac: 1.0 if nm in wanted and ph == "draw" else 0.0, seed=0):
        snap, act = rec["snapshot"], rec["action"]
        hand = list(snap.get("my_hand") or [])
        jokers = hand.count(JOKER)
        a = act.get("action")
        if not jokers or a not in ("hu", "discard"):
            continue
        me = snap["seat"]
        god = snap.get("god") or {}
        if god.get("catch_play") and god.get("god_discarder_seat") != me:
            continue          # 抓打圈受限方只能胡或打摸到的牌，不算自由选择
        hr = bot.hu_result(snap)
        if not hr or hr.get("baotou"):
            continue          # 不能胡、或本来就是爆头胡（胡/飘归 _piao_candidate 管）
        groups = len(snap["melds"][me])
        conv = []
        for t in sorted(set(hand) - {JOKER}):
            rest = list(hand)
            rest.remove(t)
            if baotou(tuple(to_counts(rest)), groups):
                conv.append(t)
        declined = a == "discard"
        became = False
        if declined and act.get("tile") in hand:
            rest = list(hand)
            rest.remove(act["tile"])
            became = baotou(tuple(to_counts(rest)), groups)
        wall = snap.get("wall_remaining") or 0
        out.append({
            "g": _group(rec["name"], room), "j": min(jokers, 2), "m": min(groups, 2),
            "conv": bool(conv), "late": wall - 20 < 8, "dec": declined, "became": became,
            "dealer": snap.get("dealer") == me, "delta": rec["ret"]["delta"], "win": rec["ret"]["win"],
            "ex": "%s 第%s局 手[%s] 摸%s 墙%s 露%d → %s（可转：%s）" % (
                os.path.basename(path)[:16], rec["round_no"], " ".join(sorted(hand)), snap.get("drawn_tile") or "-",
                wall, groups, "弃胡打%s" % act.get("tile") if declined else "胡", " ".join(conv) or "无"),
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True, help="「我们(当前)」只取这个 UTC 时间之后开打的房")
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--examples", type=int, default=15)
    ap.add_argument("--limit", type=int, default=None, help="只扫前 N 个文件（试跑用）")
    args = ap.parse_args()
    recent = recent_rooms(args.since)
    files = discover_files(limit=args.limit)
    rows = []
    with Pool(args.jobs, initializer=_init, initargs=(recent,)) as pool:
        for done, part in enumerate(pool.imap_unordered(scan, files, chunksize=4), 1):
            rows += part
            if done % 500 == 0 or done == len(files):
                print("  已扫 %d / %d" % (done, len(files)), file=sys.stderr, flush=True)

    # 三个子格：可转&墙够（S1 现行触发区）/ 可转&墙尾（E1 区）/ 不可直接转（E2 区）
    subs = (("可转墙够", lambda r: r["conv"] and not r["late"]),
            ("可转墙尾", lambda r: r["conv"] and r["late"]),
            ("不可直接转", lambda r: not r["conv"]))

    def pct(a, b):
        return "%5.1f%%" % (100.0 * a / b) if b >= 10 else "   -  "

    print("=== 持财神、这一摸能普通胡：弃胡率（%s 之后为当前）===" % args.since)
    print("每格：决策点数 / 弃胡率 / 弃胡后最终胡率 / 弃胡局分 vs 当场胡局分（最终结果，混牌运，只作参考）")
    print("%-9s %-10s | %-34s | %-34s | %-34s" % ("财神/副露", "组", *[s for s, _ in subs]))
    for j in (1, 2):
        for m in (0, 1, 2):
            label = "%s白/%s露" % ("2+" if j == 2 else j, "2+" if m == 2 else m)
            for g in GROUPS:
                base = [r for r in rows if r["g"] == g and r["j"] == j and r["m"] == m]
                if not base:
                    continue
                cells = []
                for _, f in subs:
                    sel = [r for r in base if f(r)]
                    dec = [r for r in sel if r["dec"]]
                    hu = [r for r in sel if not r["dec"]]
                    dv = [r["delta"] for r in dec if r["delta"] is not None]
                    hv = [r["delta"] for r in hu if r["delta"] is not None]
                    cells.append("%4d %s 胡%s %s/%s" % (
                        len(sel), pct(len(dec), len(sel)), pct(sum(r["win"] for r in dec), len(dec)),
                        "%+5.1f" % (sum(dv) / len(dv)) if len(dv) >= 10 else "  -  ",
                        "%+5.1f" % (sum(hv) / len(hv)) if len(hv) >= 10 else "  -  "))
                print("%-9s %-10s | %-34s | %-34s | %-34s" % (label, g, *cells))
            print()
    for title, f in (("可转墙够却当场胡（S1 应触发而没触发）", lambda r: r["conv"] and not r["late"] and not r["dec"]),
                     ("可转墙尾当场胡", lambda r: r["conv"] and r["late"] and not r["dec"])):
        ex = [r for r in rows if r["g"] == "我们(当前)" and r["j"] == 2 and f(r)]
        print("=== 我们(当前) 2+ 财神 %s：%d 个，列前 %d ===" % (title, len(ex), args.examples))
        for r in ex[:args.examples]:
            print("  " + r["ex"])
    ex = [r for r in rows if r["g"] == "高手" and r["j"] == 2 and r["dec"] and (r["late"] or not r["conv"])]
    print("=== 高手 2+ 财神 在 S1 不触发的地方弃胡：%d 个，列前 %d ===" % (len(ex), args.examples))
    for r in ex[:args.examples]:
        print("  " + r["ex"])


if __name__ == "__main__":
    main()
