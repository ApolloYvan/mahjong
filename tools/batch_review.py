"""批量复盘：一批房里每房首名是谁、首名和我们差在哪、S1（弃胡转爆头）每次触发的真实结局。

    python3 tools/batch_review.py a_xxx a_yyy ...

只读 models/events（或 tools/models/events）与 logs/*.jsonl，不联网。
先用 `python3 tools/pipeline.py collect <server> <房号...>` 把房采集下来。
"""
import argparse
import glob
import json
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mj.rules import evaluate
from mj.tiles import TILE_INDEX, to_counts

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mining_common import seat_names  # noqa: E402

OUR = "重生之我是雀神"
JOKER = "白"


def load_room(room):
    games, seen = {}, set()
    for path in sorted(glob.glob("models/events/%s_*.json" % room)
                       + glob.glob("tools/models/events/%s_*.json" % room)):
        base = os.path.basename(path)
        if base in seen:
            continue
        seen.add(base)
        try:
            with open(path, encoding="utf-8") as source:
                game = json.JSONDecoder().raw_decode(source.read())[0]
        except (OSError, ValueError):
            continue
        games[game.get("game_id") or base[:-5]] = game
    return games


def new_stat():
    return Counter()


def scan_game(game, room_totals, stat_of):
    names = seat_names(game)
    ended, melds = {}, defaultdict(Counter)
    for block in game.get("blocks", []):
        rn = block.get("round_no")
        for ev in block.get("events", []):
            kind = ev.get("type")
            if kind in ("chi", "peng") and ev.get("seat") is not None:
                melds[rn][ev["seat"]] += 1
            elif kind == "round_ended":
                ended[rn] = ev.get("data") or {}
    for rnd in game.get("rounds", []):
        rn, scores = rnd.get("round_no"), rnd.get("scores") or []
        if len(scores) != len(names):
            continue
        data = ended.get(rn, {})
        fan = data.get("fan") or 0
        detail = data.get("detail") or []
        for seat, name in enumerate(names):
            room_totals[name] += scores[seat]
            st = stat_of(name)
            st["rounds"] += 1
            st["score"] += scores[seat]
            if rnd.get("dealer") == seat:
                st["dr"] += 1
                st["ds"] += scores[seat]
                st["dw"] += rnd.get("winner") == seat and not rnd.get("is_draw")
            if rnd.get("winner") == seat and not rnd.get("is_draw"):
                st["wins"] += 1
                st["fan"] += fan
                st["bt"] += any("爆头" in x for x in detail)
                st["menqing"] += melds[rn][seat] == 0
                st["melds"] += melds[rn][seat]
                st["inc"] += scores[seat]
                if fan >= 2:
                    st["inc_big"] += scores[seat]


def s1_audit(rooms, games, log_glob):
    rows = []
    for path in sorted(glob.glob(log_glob)):
        with open(path, encoding="utf-8", errors="replace") as source:
            for line in source:
                if '"kind": "decision"' not in line or '"phase": "draw"' not in line:
                    continue
                if not any(r in line for r in rooms):
                    continue
                try:
                    p = json.loads(line)["payload"]
                except (ValueError, KeyError):
                    continue
                hand, drawn, seat = p.get("hand") or [], p.get("drawn_tile"), p.get("seat")
                melds = p.get("melds") or []
                groups = len(melds[seat]) if isinstance(melds, list) and len(melds) == 4 else 0
                if not drawn or drawn not in hand or len(hand) + 3 * groups != 14:
                    continue
                if (p.get("decision") or {}).get("action") == "hu":
                    continue
                rest = list(hand)
                rest.remove(drawn)
                counts = tuple(to_counts(rest))
                god = p.get("god") or {}
                now = evaluate(counts, TILE_INDEX[drawn], chain_count=god.get("chain_count") or 0,
                               piao=p.get("piao") or 0, meld_groups=groups)
                if not now or now["baotou"]:
                    continue
                game = games.get(p.get("game_id"))
                if not game:
                    continue
                rnd = next((r for r in game.get("rounds", []) if r.get("round_no") == p.get("round_no")), None)
                if not rnd:
                    continue
                dealer = p.get("dealer") == seat
                rows.append({
                    "game": p["game_id"], "round": p.get("round_no"),
                    "jokers": counts[TILE_INDEX[JOKER]] + (drawn == JOKER), "melds": groups,
                    "wall": p.get("wall_remaining"), "dealer": dealer,
                    "won": rnd.get("winner") == seat and not rnd.get("is_draw"),
                    "got": rnd["scores"][seat],
                    "hu_now": now["fan"] * (24 if dealer else 10),
                })
    uniq = {(r["game"], r["round"]): r for r in rows}   # 同一局多次弃胡只算一次结局
    return list(uniq.values())


def fmt(st):
    r, w = st["rounds"] or 1, st["wins"] or 1
    d = st["dr"] or 1
    return "%5d %6.1f%% %+7.3f %6.2f %7.1f%% %7.1f%% %6.2f %8.1f%% %7.1f%% %+8.2f" % (
        st["rounds"], 100 * st["wins"] / r, st["score"] / r, st["fan"] / w,
        100 * st["bt"] / w, 100 * st["menqing"] / w, st["melds"] / w,
        100 * st["inc_big"] / (st["inc"] or 1), 100 * st["dw"] / d, st["ds"] / d)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("rooms", nargs="+")
    ap.add_argument("--logs", default="logs/*.jsonl")
    args = ap.parse_args()

    all_games, champs = {}, Counter()
    ours, champ_stat, others = new_stat(), new_stat(), new_stat()
    print("%-16s %-22s %9s %6s %9s" % ("房间", "首名", "首名分/局", "我们名次", "我们分/局"))
    print("-" * 70)
    for room in args.rooms:
        games = load_room(room)
        if not games:
            print("%-16s 未采集（先跑 pipeline.py collect）" % room)
            continue
        all_games.update(games)
        totals, per = Counter(), defaultdict(new_stat)
        for game in games.values():
            scan_game(game, totals, lambda n: per[n])
        ranking = sorted(totals, key=totals.get, reverse=True)
        top = ranking[0]
        champs[top] += 1
        for name, st in per.items():
            (ours if name == OUR else champ_stat if name == top else others).update(st)
        our_rank = ranking.index(OUR) + 1 if OUR in ranking else 0
        print("%-16s %-22s %+9.3f %6s %+9.3f" % (
            room, top[:22], per[top]["score"] / (per[top]["rounds"] or 1), our_rank or "-",
            per[OUR]["score"] / (per[OUR]["rounds"] or 1) if OUR in per else 0))

    print("\n=== 首名（在他夺冠的房里） vs 我们 vs 其余玩家 ===")
    print("%-8s %5s %7s %7s %6s %8s %8s %6s %9s" % (
        "", "局数", "胜率", "分/局", "番/胡", "爆头占胡", "门清占胡", "胡时副露", "2番+收入") + "  庄胡率  庄分/局")
    for label, st in (("首名", champ_stat), ("我们", ours), ("其余", others)):
        print("%-8s %s" % (label, fmt(st)))
    print("\n夺冠次数：", "  ".join("%s×%d" % kv for kv in champs.most_common()))

    rows = s1_audit(args.rooms, all_games, args.logs)
    print("\n=== S1 弃胡转爆头：每次触发的真实结局（同一局只计一次） ===")
    if not rows:
        print("没有触发记录（开关没开，或日志不在 %s）" % args.logs)
        return
    won = [r for r in rows if r["won"]]
    net = sum(r["got"] - r["hu_now"] for r in rows)
    print("触发 %d 局，最终自己胡 %d 局（%.0f%%），实际得分合计 %+d，若当场胡合计 %+d，净差 %+d"
          % (len(rows), len(won), 100 * len(won) / len(rows), sum(r["got"] for r in rows),
             sum(r["hu_now"] for r in rows), net))
    cells = defaultdict(list)
    for r in rows:
        cells[(min(r["jokers"], 2), min(r["melds"], 2))].append(r)
    print("%-14s %4s %6s %8s" % ("财神/副露", "次数", "胡率", "净差"))
    for key in sorted(cells):
        rs = cells[key]
        print("%-14s %4d %5.0f%% %+8d" % ("%s白/%s露" % ("2+" if key[0] == 2 else key[0], "2+" if key[1] == 2 else key[1]),
                                         len(rs), 100 * sum(x["won"] for x in rs) / len(rs),
                                         sum(x["got"] - x["hu_now"] for x in rs)))


if __name__ == "__main__":
    main()
