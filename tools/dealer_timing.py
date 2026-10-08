"""做庄是不是被"开局第一张"的超时拖累：庄家每局开局就要立刻出牌（14 张、还没摸牌），
若 bot 刚切到新一局反应慢、3 秒内没出，服务端会替我们打最右一张，起手就被打坏。

    python3 tools/dealer_timing.py --since 2026-10-07T18:00

输出：
  1. 对局文件：我们 / 对手 在 庄 / 闲 局里的出牌超时（每局次数），以及"本局第 1 张出牌"超时的比例
  2. 决策日志：我们庄家第一张出牌 vs 其它出牌的 计算耗时 / 发请求耗时（ms）
"""
import argparse
import json
import os
import sys
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tools")]

from baotou_funnel import recent_rooms  # noqa: E402
from mining_common import OUR_NAME, discover_files, merge_rounds, seat_names  # noqa: E402


def q(xs, f):
    xs = sorted(xs)
    return xs[int(f * (len(xs) - 1))] if xs else float("nan")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True)
    args = ap.parse_args()
    rooms = recent_rooms(args.since)
    c = defaultdict(Counter)
    for p in discover_files():
        if os.path.basename(p).split("_r")[0] not in rooms:
            continue
        with open(p, encoding="utf-8") as f:
            g = json.load(f)
        names = seat_names(g)
        if OUR_NAME not in names:
            continue
        me = names.index(OUR_NAME)
        info = {r.get("round_no"): r for r in g.get("rounds") or []}
        for rnd in merge_rounds(g):
            meta = info.get(rnd["round_no"]) or {}
            dealer = meta.get("dealer", rnd.get("dealer"))
            nth = Counter()          # 每个座位本局第几次出牌（含超时代打）
            for ev in rnd["events"]:
                k, s = ev.get("type"), ev.get("seat")
                if k == "round_ended":
                    break
                if k == "timeout" and (ev.get("data") or {}).get("kind") == "discard":
                    who = "我们" if s == me else "对手"
                    role = "庄" if s == dealer else "闲"
                    c[(who, role)]["timeouts"] += 1
                    if nth[s] == 0:
                        c[(who, role)]["first_to"] += 1
                if k == "tile_discarded":
                    nth[s] += 1
            for s in range(4):
                who = "我们" if s == me else "对手"
                role = "庄" if s == dealer else "闲"
                c[(who, role)]["rounds"] += 1
    print("=== 1. 出牌超时（%s 之后 %d 房）===" % (args.since, len(rooms)))
    print("%-8s %6s %8s %10s %16s" % ("", "局数", "超时次数", "每局超时", "本局第1张就超时"))
    for who in ("我们", "对手"):
        for role in ("庄", "闲"):
            x = c[(who, role)]
            n = x["rounds"] or 1
            print("%-8s %6d %8d %10.3f %9d（%.1f%%局）" % (who + role, x["rounds"], x["timeouts"], x["timeouts"] / n,
                                                         x["first_to"], 100.0 * x["first_to"] / n))

    first, other = defaultdict(list), defaultdict(list)
    since = args.since[:19]
    for path in sorted(p for p in os.listdir("logs") if p.endswith(".jsonl") and p[:10] >= args.since[:10]):
        with open(os.path.join("logs", path), "rb") as f:
            for raw in f:
                if b'"kind": "decision"' not in raw[:200] or b"client_prepare_ms" not in raw:
                    continue
                d = json.loads(raw)
                if d["time"][:19] < since:
                    continue
                pl = d["payload"]
                if pl.get("phase") not in ("draw", "discard") or pl.get("dealer") != pl.get("seat"):
                    continue
                is_first = not pl.get("drawn_tile") and (pl.get("wall_remaining") or 0) >= 82 \
                    and not any(pl.get("melds") or [])
                bucket = first if is_first else other
                bucket["compute"].append(pl.get("client_prepare_ms") or 0)
                bucket["request"].append(pl.get("action_request_ms") or 0)
    print("\n=== 2. 我们做庄的出牌耗时（决策日志）===")
    for label, b in (("开局第一张", first), ("其它出牌", other)):
        print("%-10s n=%5d  计算 p50/p90/p99 %4.0f/%4.0f/%4.0f ms   发请求 p50/p90 %4.0f/%4.0f ms" % (
            label, len(b["compute"]), q(b["compute"], .5), q(b["compute"], .9), q(b["compute"], .99),
            q(b["request"], .5), q(b["request"], .9)))
    print("\n读法：若「我们庄」的超时、尤其「第1张就超时」明显高于「我们闲」和「对手庄」，就是开局反应慢在伤庄家。")


if __name__ == "__main__":
    main()
