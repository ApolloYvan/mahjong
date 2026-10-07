"""做爆头的漏斗：拿到财神之后，一步步走到「爆头胡」的每一环转化率，我们（当前配置）vs 高手。

    python3 tools/baotou_funnel.py
    python3 tools/baotou_funnel.py --since 2026-09-25T15:30     # 「我们」只取这个时间之后的房（当前配置）

单步推演只能比较「这一步打哪张」，看不到要连续几步配合的打法；这里直接看结果链条断在哪一环：
  入口  第 5 手打完时手里有财神（按 财神数 × 副露 分组，起点对齐）
  ① 之后某一手打完是听牌，且财神还在手里
  ② 之后某一手打完是爆头听牌（摸任何牌都胡）
  ③ 本局胡牌        ④ 本局以爆头胡牌
每一环都是「条件转化率」，哪一环我们明显低于高手，差距就在那一段打法里。
「我们」的时间取自 logs/*.jsonl 里该房第一条记录（与 luck_adjust 一致）。
"""
import argparse
import os
import sys
import json
from collections import Counter, defaultdict
from multiprocessing import Pool

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from mining_common import discover_files, merge_rounds  # noqa: E402
from mining_common import seat_names  # noqa: E402
from mj.rules import _wins_any, baotou  # noqa: E402
from mj.shanten import route_shanten  # noqa: E402
from mj.tiles import to_counts  # noqa: E402

OUR = "重生之我是雀神"
JOKER = "白"
MASTERS = {"⭐꧁༺🀆🀆🀆🀆༻꧂⭐", "Deepseek胡", "爆头研究所", "Astra-0", "晴总总，该请桂语山房了",
           "glm-flash", "铳一色14", "歪比巴卜肉蛋葱鸡", "放假了偷偷训练", "康陶应雀"}
# 2026-10-04 master_oos 样本外核实过的真高手（MASTERS 里一半样本外只有 +0.2，别再用来当标杆）
TOP_OOS = {"腾蛇-0638", "歪比巴卜肉蛋葱鸡", "⭐꧁༺🀆🀆🀆🀆༻꧂⭐", "玄武-2346", "鲲鹏~6383", "算我求您了"}
_RECENT = set()
_TOP = set()


def _init(recent, top):
    _RECENT.update(recent)
    _TOP.update(top)


def scan(path):
    out = []
    try:
        with open(path, encoding="utf-8") as source:
            game = json.load(source)
    except (OSError, ValueError):
        return out
    names = seat_names(game)
    if len(names) != 4:
        return out
    room = game.get("room_id") or ""
    groups = {}
    for s, n in enumerate(names):
        if n in _TOP:
            groups[s] = "高手"
        elif n == OUR:
            groups[s] = "我们(当前)" if room in _RECENT else "我们(以前)"
    if not groups:
        return out
    info = {r.get("round_no"): r for r in game.get("rounds") or []}
    for rnd in merge_rounds(game):
        meta = info.get(rnd["round_no"])
        if not meta or rnd.get("truncated") or not rnd.get("start_hands"):
            continue
        winner = None if meta.get("is_draw") else meta.get("winner")
        detail = []
        hands = [list(h) for h in rnd["start_hands"]]
        melds = [0] * 4
        disc = [0] * 4
        track = {s: {} for s in groups}
        try:
            for ev in rnd["events"]:
                kind, seat, tile = ev["type"], ev.get("seat"), ev.get("tile")
                data = ev.get("data") or {}
                if kind == "tile_drawn":
                    hands[seat].append(tile)
                    t = track.get(seat)
                    if t and t.get("tn"):
                        t["draws"] = t.get("draws", 0) + 1
                        t["can_win"] = _wins_any(tuple(to_counts(hands[seat])), melds[seat])
                elif kind == "tile_discarded":
                    hands[seat].remove(tile)
                    disc[seat] += 1
                    if seat in track and len(hands[seat]) + 3 * melds[seat] == 13:
                        t = track[seat]
                        h = hands[seat]
                        j = h.count(JOKER)
                        if disc[seat] == 5:
                            t["entry"] = (min(j, 2), min(melds[seat], 2)) if j else None
                        if t.pop("can_win", False):
                            t["declined"] = t.get("declined", 0) + 1
                        was_tn, t["tn"] = t.get("tn"), False
                        if t.get("entry") and disc[seat] >= 5 and j:
                            counts = tuple(to_counts(h))
                            if route_shanten(counts, melds[seat]) == 0:
                                if not t.get("tenpai"):
                                    t["first"] = disc[seat]
                                t["tenpai"] = True
                                if not t.get("bt") and baotou(counts, melds[seat]):
                                    t["bt"] = True
                                    t["conv"] = was_tn
                                t["tn"] = not t.get("bt")
                            elif was_tn:
                                t["broke"] = t.get("broke", 0) + 1
                elif kind == "chi":
                    used = list(data.get("tiles") or [])
                    used.remove(tile)
                    for x in used:
                        hands[seat].remove(x)
                    melds[seat] += 1
                elif kind == "peng":
                    for _ in range(2):
                        hands[seat].remove(tile)
                    melds[seat] += 1
                elif kind == "gang":
                    take = {"an": 4, "ming": 3, "bu": 1}[data.get("kind")]
                    for _ in range(take):
                        hands[seat].remove(tile)
                    if data.get("kind") != "bu":
                        melds[seat] += 1
                elif kind == "round_ended":
                    detail = data.get("detail") or []
        except (ValueError, KeyError, IndexError, TypeError):
            continue
        for s, t in track.items():
            if not t.get("entry"):
                continue
            won = winner == s
            out.append((groups[s], t["entry"], bool(t.get("tenpai")), bool(t.get("bt")), won,
                        won and any("爆头" in x for x in detail),
                        t.get("first", 0), t.get("draws", 0), bool(t.get("conv")),
                        t.get("declined", 0), t.get("broke", 0)))
    return out


def room_first_seen():
    """每个房间在 bot 日志里第一次出现的 UTC 时间 {room: time}。

    日志是只追加的 jsonl（合计几个 G），全量读一遍要 30 秒；这里按文件记住
    上次读到的字节位置，只读新增部分。文件变小（被截断/替换）时整份重读。
    缓存：tools/.cache/room_first_seen.json。"""
    import glob
    import re
    rx = re.compile(r'^\{"time": "([^"]+)".*?"game_id": "(a_[0-9a-f]+)_')
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cache_path = os.path.join(root, "tools", ".cache", "room_first_seen.json")
    try:
        with open(cache_path, encoding="utf-8") as source:
            cache = json.load(source)
    except (OSError, ValueError):
        cache = {}
    first = {}
    new_cache = {}
    for path in sorted(glob.glob(os.path.join(root, "logs", "*.jsonl"))):
        size = os.path.getsize(path)
        entry = cache.get(path) or {}
        offset = entry.get("offset", 0)
        rooms = dict(entry.get("first") or {})
        if offset > size:
            offset, rooms = 0, {}
        if offset < size:
            with open(path, "rb") as source:
                source.seek(offset)
                for raw in source:
                    if not raw.endswith(b"\n"):
                        break          # 正在写的半行，下次再读
                    offset += len(raw)
                    m = rx.search(raw.decode("utf-8", errors="replace"))
                    if m and m.group(2) not in rooms:
                        rooms[m.group(2)] = m.group(1)
        new_cache[path] = {"offset": offset, "first": rooms}
        for room, t in rooms.items():
            if room not in first or t < first[room]:
                first[room] = t
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        tmp = cache_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as out:
            json.dump(new_cache, out)
        os.replace(tmp, cache_path)
    except OSError:
        pass
    return first


def recent_rooms(since):
    return {room for room, t in room_first_seen().items() if t >= since}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-09-25T15:30", help="「我们(当前)」只取这个 UTC 时间之后开打的房")
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--top", choices=("oos", "old"), default="oos", help="高手组：oos=样本外核实的 6 人（默认），old=旧 MASTERS")
    args = ap.parse_args()
    top = TOP_OOS if args.top == "oos" else MASTERS
    recent = recent_rooms(args.since)
    print("「我们(当前)」的房：%d 个（%s 之后）" % (len(recent), args.since))
    files = discover_files()
    rows = []
    with Pool(args.jobs, initializer=_init, initargs=(recent, top)) as pool:
        for done, part in enumerate(pool.imap_unordered(scan, files, chunksize=8), 1):
            rows += part
            if done % 500 == 0 or done == len(files):
                print("  已扫 %d / %d" % (done, len(files)), flush=True)

    cell = defaultdict(Counter)
    for g, entry, tenpai, bt, won, won_bt, first, draws, conv, declined, broke in rows:
        for key in ((g, entry), (g, "合计")):
            c = cell[key]
            c["n"] += 1
            c["tenpai"] += tenpai
            c["bt"] += bt
            c["won"] += won
            c["won_bt"] += won_bt
            c["bt_won"] += bt and won
            c["tenpai_nobt_won"] += tenpai and not bt and won
            c["tenpai_nobt"] += tenpai and not bt
            c["first"] += first
            c["draws"] += draws
            c["conv"] += conv
            c["declined_r"] += declined > 0
            c["broke"] += broke

    def pct(a, b):
        return "%5.1f%%" % (100.0 * a / b) if b >= 10 else "   -  "

    print("\n=== 做爆头的漏斗（第 5 手打完手里有财神的局）===")
    print("%-8s %-10s %6s | %-9s %-11s %-11s | %-9s %-11s | %s" % (
        "财神/副露", "组", "局数", "①听牌留白", "②成爆头听|①", "爆头听→胡", "③胡牌", "④爆头胡", "普通听牌→胡"))
    entries = sorted({e for (_g, e) in cell if e != "合计"}) + ["合计"]
    for e in entries:
        label = e if e == "合计" else "%s白/%s露" % ("2+" if e[0] == 2 else e[0], "2+" if e[1] == 2 else e[1])
        for g in ("高手", "我们(当前)", "我们(以前)"):
            c = cell.get((g, e))
            if not c:
                continue
            print("%-8s %-10s %6d | %-9s %-11s %-11s | %-9s %-11s | %s" % (
                label, g, c["n"], pct(c["tenpai"], c["n"]), pct(c["bt"], c["tenpai"]), pct(c["bt_won"], c["bt"]),
                pct(c["won"], c["n"]), pct(c["won_bt"], c["n"]), pct(c["tenpai_nobt_won"], c["tenpai_nobt"])))
        print()
    print("=== ②这一环拆开看（只算听牌的局）===")
    print("%-8s %-10s %6s | %-10s %-12s %-14s | %-12s %s" % (
        "财神/副露", "组", "听牌局", "首次听牌手", "听后摸牌/局", "每摸一张成爆头", "放弃自摸局%", "拆听次数/局"))
    for e in entries:
        label = e if e == "合计" else "%s白/%s露" % ("2+" if e[0] == 2 else e[0], "2+" if e[1] == 2 else e[1])
        for g in ("高手", "我们(当前)", "我们(以前)"):
            c = cell.get((g, e))
            if not c or c["tenpai"] < 10:
                continue
            n = c["tenpai"]
            print("%-8s %-10s %6d | %8.1f   %9.2f    %11s    | %9s    %.2f" % (
                label, g, n, c["first"] / n, c["draws"] / n,
                "%5.1f%%" % (100.0 * c["conv"] / c["draws"]) if c["draws"] >= 10 else "  -  ",
                pct(c["declined_r"], n), c["broke"] / n))
        print()
    print("读法：首次听牌手 大 = 听得慢，留给转爆头的摸牌少；每摸一张成爆头 低 = 听牌后的牌形/打法不利于转爆头；")
    print("  放弃自摸局% = 摸到能胡的普通牌却不胡（为了等爆头）的局占比；拆听 = 从听牌打回不听。")
    print()
    print("读法：从左到右看，哪一列「我们(当前)」比「高手」低得最多，断点就在那一环：")
    print("  ①低 = 拿着财神却迟迟听不了牌（或途中把财神用掉/打掉）；②低 = 听牌了但收不成爆头形；")
    print("  爆头听→胡 低 = 到了爆头却没胡下来（被别人先胡）；样本 <10 显示 '-'。")


if __name__ == "__main__":
    main()
