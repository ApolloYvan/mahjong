# -*- coding: utf-8 -*-
"""高番回放摘要：把大牌局压成几十行，而不是把几 MB 的 JSON 贴进对话。

用法:
    python3 tools/highfan_digest.py '回放目录/*.json'           # 默认 >=8 番
    python3 tools/highfan_digest.py '回放/*.json' --min-fan 16
    python3 tools/highfan_digest.py '回放/*.json' --no-diff     # 只要路线，不跑我们的 bot

对每一局 >=min_fan 的胡牌输出：
  * 赢家起手向听/财神数（分离"牌运"和"技术"）
  * 成牌路线：吃碰杠与每一次弃财神，并标注弃完是否**仍然爆头**
  * DIFF：把赢家的每个决策点喂给我们的 mj.bot.choose_action，**只打印不一致的**

支持两种回放结构：带逐帧 snapshot 的（pipeline collect / 荣耀回放）走全功能；
只有 blocks/events 的走摘要模式（无 DIFF，会明确标注）。
"""
import argparse
import glob
import json
import sys
from collections import Counter

sys.path.insert(0, ".")
from mj.rules import baotou                      # noqa: E402
from mj.tiles import to_counts                   # noqa: E402

JOKER = "白"


def _frames(game):
    """归一化出 (seats, frames)；没有逐帧 snapshot 时返回 frames=None。"""
    seats = [s.get("name") or s.get("user_id") for s in (game.get("seats") or [])]
    fr = game.get("frames")
    if isinstance(fr, list) and fr and isinstance(fr[0], dict) and "snapshot" in fr[0]:
        return seats, fr
    return seats, None


def _seat_snapshot(snap, seat, discarder):
    """把观战视角的一帧投影成我们 bot 期望的单座位快照。"""
    god = snap.get("god") or {}
    def pick(key, default):
        v = god.get(key, default)
        return v[seat] if isinstance(v, list) and len(v) == 4 else v
    out = dict(snap)
    out["seat"] = seat
    out["my_hand"] = list((snap.get("hands") or [[]] * 4)[seat])
    out["god"] = {
        "baotou": bool(pick("baotou", False)),
        "chain_count": pick("chain_count", 0) or 0,
        "catch_play": bool(god.get("catch_play")),
        # 观战投影不带 god_discarder_seat（实盘单座快照是带的）；
        # 用"最近一个打出财神的座位"还原，抓打圈的发起者就是他。
        "god_discarder_seat": discarder,
    }
    out.pop("hands", None)
    return out


def _still_baotou(hand_after, meld_groups):
    try:
        return bool(baotou(to_counts(hand_after), meld_groups))
    except Exception:
        return None


def _load_games(path):
    """一个文件里可能装着多局。

    2026-09-24：data/replay.jsonl 不是 JSON Lines——它是 14 个缩进排版的 JSON
    对象首尾直接相接（`}` 紧跟 `{`），既不是每行一个 JSON、也不是一个数组，
    标准 json.load 会在第二个对象的 `{` 处抛 "Expecting property name"。
    用 raw_decode 逐个吃掉即可，同时兼容普通单局文件和真正的 JSON Lines。
    """
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return []
    decoder = json.JSONDecoder()
    games, i, n = [], 0, len(text)
    while i < n:
        while i < n and text[i] in " \t\r\n":
            i += 1
        if i >= n:
            break
        try:
            obj, i = decoder.raw_decode(text, i)
        except ValueError:
            break
        if isinstance(obj, list):
            games.extend(g for g in obj if isinstance(g, dict))
        elif isinstance(obj, dict):
            games.append(obj)
    return games


def _iter_games(pattern):
    for path in sorted(glob.glob(pattern)):
        games = _load_games(path)
        for index, game in enumerate(games):
            label = "%s#%d" % (path, index + 1) if len(games) > 1 else path
            yield label, game


def digest(path, min_fan, do_diff, out, game=None):
    if game is None:
        games = _load_games(path)
        if not games:
            out.append("  [跳过] %s: 无法解析" % path.split("/")[-1])
            return None
        game = games[0]
    seats, frames = _frames(game)
    if frames is None:
        out.append("  [摘要模式] %s 无逐帧 snapshot，跳过 DIFF" % path.split("/")[-1])
        return None

    end = next((f for f in frames
                if (f.get("ev") or {}).get("type") == "round_ended"), None)
    if not end:
        return None
    data = end["ev"].get("data") or {}
    fan, winner = data.get("fan", 0), end["ev"].get("seat")
    if data.get("draw") or fan < min_fan or winner is None:
        return None

    dealer = data.get("dealer")
    first = frames[0].get("snapshot") or {}
    start = list((first.get("hands") or [[]] * 4)[winner])
    tag = "%s r%s" % (game.get("game_id", "?")[:16], data.get("round_no"))
    out.append("\n=== %s  赢家 %s%s  %d番 %s  得分%+d" % (
        tag, seats[winner] if winner < len(seats) else winner,
        "(庄)" if dealer == winner else "", fan,
        "/".join(data.get("detail") or []), (data.get("scores") or [0] * 4)[winner]))
    out.append("  起手: %s  (财神%d张)" % (" ".join(start), start.count(JOKER)))

    route, discarder, mism, total = [], None, [], 0
    for f in frames:
        ev, snap = f.get("ev"), f.get("snapshot") or {}
        if not ev:
            continue
        seat, typ, tile = ev.get("seat"), ev.get("type"), ev.get("tile")
        if typ == "tile_discarded" and tile == JOKER:
            discarder = seat
        if seat == winner:
            if typ in ("peng", "chi", "gang"):
                got = (ev.get("data") or {}).get("tiles") or []
                route.append("%s %s%s" % (typ, tile, "(" + "".join(got) + ")" if got else ""))
            elif typ == "tile_discarded" and tile == JOKER:
                after = snap.get("hands", [[]] * 4)[winner]
                mg = len((snap.get("melds") or [[]] * 4)[winner])
                bt = _still_baotou(list(after), mg)
                route.append("弃财神→链%d%s" % (
                    ((snap.get("god") or {}).get("chain_count") or [0] * 4)[winner]
                    if isinstance((snap.get("god") or {}).get("chain_count"), list) else 0,
                    "[仍爆头]" if bt else "[失爆头!]" if bt is False else ""))
    out.append("  路线: %s" % (" → ".join(route) if route else "(门清自摸)"))

    if do_diff:
        from mj.bot import choose_action
        prev, discarder = None, None
        for f in frames:
            ev, snap = f.get("ev"), f.get("snapshot") or {}
            if prev is not None and (ev or {}).get("seat") == winner:
                typ, tile = ev.get("type"), ev.get("tile")
                if typ in ("peng", "chi", "gang", "pass", "tile_discarded", "hu"):
                    try:
                        got = choose_action(_seat_snapshot(prev, winner, discarder))
                    except Exception as exc:
                        got = {"action": "EXC", "tile": type(exc).__name__}
                    total += 1
                    ga = (got or {}).get("action")
                    want = "discard" if typ == "tile_discarded" else typ
                    if ga != want or (want in ("discard", "peng", "chi") and (got or {}).get("tile") != tile):
                        mism.append("    ✗ seq%-5s 他=%s %s  我们=%s %s" % (
                            ev.get("seq"), want, tile, ga, (got or {}).get("tile")))
            if (ev or {}).get("type") == "tile_discarded" and (ev or {}).get("tile") == JOKER:
                discarder = ev.get("seat")
            prev = snap
        out.append("  DIFF: %d/%d 一致" % (total - len(mism), total))
        out.extend(mism[:12])
        if len(mism) > 12:
            out.append("    ... 另有 %d 处分歧" % (len(mism) - 12))
    return dict(fan=fan, detail=tuple(data.get("detail") or []),
                mism=len(mism), total=total, joker=start.count(JOKER))


def player_report(pattern, who, out):
    """盯人模式：某玩家的每一个决策点 vs 我们的 choose_action，按类别汇总分歧。

    高番模式回答的是"大牌怎么做"；本模式回答的是"高胜率的人每一手跟我们差在哪"。
    因为 Kimi-K4 这类选手的优势不在番数（番/胡 1.185，比我们的 1.250 还低），
    而在做牌速度——那种优势只会体现在成百上千个普通弃牌上，必须按类别聚合看。
    """
    from mj.bot import choose_action
    cat = Counter()
    diffs = Counter()
    examples = {}
    rounds = 0
    for path, game in _iter_games(pattern):
        seats, frames = _frames(game)
        if frames is None:
            continue
        idx = next((i for i, n in enumerate(seats) if who in (n or "")), None)
        if idx is None:
            continue
        rounds += 1
        prev, discarder = None, None
        for f in frames:
            ev, snap = f.get("ev"), f.get("snapshot") or {}
            if prev is not None and (ev or {}).get("seat") == idx:
                typ, tile = ev.get("type"), ev.get("tile")
                if typ in ("peng", "chi", "gang", "pass", "tile_discarded"):
                    want = "discard" if typ == "tile_discarded" else typ
                    # 弃牌按"是不是财神"分开统计，两者的策略含义完全不同
                    key = "discard(财神)" if (want == "discard" and tile == JOKER) else want
                    cat[key] += 1
                    try:
                        got = choose_action(_seat_snapshot(prev, idx, discarder))
                    except Exception as exc:
                        got = {"action": "EXC", "tile": type(exc).__name__}
                    ga, gt = (got or {}).get("action"), (got or {}).get("tile")
                    if ga != want or (want in ("discard", "peng", "chi") and gt != tile):
                        diffs[key] += 1
                        examples.setdefault(key, []).append(
                            "      seq%-5s 他=%s %s → 我们=%s %s  [%s]" % (
                                ev.get("seq"), want, tile, ga, gt, path.split("/")[-1][:22]))
            if (ev or {}).get("type") == "tile_discarded" and (ev or {}).get("tile") == JOKER:
                discarder = ev.get("seat")
            prev = snap
    if not rounds:
        out.append("(没有找到含 '%s' 且带逐帧 snapshot 的回放)" % who)
        return
    out.append("=== 盯人: %s  共 %d 局 ===" % (who, rounds))
    out.append("  %-14s %8s %8s %8s" % ("决策类别", "次数", "分歧", "分歧率"))
    for k in sorted(cat, key=lambda x: -cat[x]):
        out.append("  %-14s %8d %8d %7.1f%%" % (k, cat[k], diffs[k], 100.0 * diffs[k] / cat[k]))
    tot, dif = sum(cat.values()), sum(diffs.values())
    out.append("  %-14s %8d %8d %7.1f%%" % ("合计", tot, dif, 100.0 * dif / max(tot, 1)))
    for k in sorted(diffs, key=lambda x: -diffs[x]):
        out.append("    %s 的分歧样例:" % k)
        out.extend(examples[k][:6])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pattern")
    ap.add_argument("--min-fan", type=int, default=8)
    ap.add_argument("--no-diff", action="store_true")
    ap.add_argument("--player", help="盯住某个玩家名（子串匹配），忽略番数门槛，"
                                     "统计他每一手与我们 bot 的分歧——用来学高手的做牌效率")
    a = ap.parse_args()
    out, rows = [], []
    if a.player:
        player_report(a.pattern, a.player, out)
        print("\n".join(out))
        return
    for label, game in _iter_games(a.pattern):
        r = digest(label, a.min_fan, not a.no_diff, out, game=game)
        if r:
            rows.append(r)
    if rows:
        out.append("\n=== 汇总 %d 局 >=%d番 ===" % (len(rows), a.min_fan))
        det = Counter(d for r in rows for d in r["detail"])
        out.append("  番种出现次数: %s" % "  ".join("%s×%d" % kv for kv in det.most_common()))
        out.append("  起手财神数分布: %s" % dict(Counter(r["joker"] for r in rows)))
        tot, mis = sum(r["total"] for r in rows), sum(r["mism"] for r in rows)
        if tot:
            out.append("  我们与高番赢家的决策一致率: %d/%d = %.1f%%" % (tot - mis, tot, 100.0 * (tot - mis) / tot))
    else:
        out.append("(没有匹配到 >=%d番 的可解析对局)" % a.min_fan)
    print("\n".join(out))


if __name__ == "__main__":
    main()
