"""策略反推：在高手的真实牌局位置上跑我们的策略，看分歧在哪。

方法：全量重放（四家完全信息），对每一次弃牌，用该座位当时的真实手牌调用
mj.strategy.choose_discard，与他实际打出的牌比对。相同 = 我们的策略在那个
位置会做同样的事；不同 = 一个可审计的分歧样本。

这是"看高手怎么打"，不是比统计量——统计量只能告诉我们差多少，分歧样本能
告诉我们差在哪张牌上。

    python3 tools/policy_diff.py --top 8        # 按分/局排名取前 8 名对比
"""
import argparse
import glob
import json
import os
import sys
from collections import Counter, defaultdict
from multiprocessing import Pool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mj.shanten import combined_route, route_shanten, shanten
from mj.strategy import choose_discard
from mj.tiles import TILE_INDEX, to_counts

OUR_UID = "u_fd06550b5fb3"
JOKER = "白"


def merge_rounds(game):
    by = {}
    for block in game["blocks"]:
        rno = block["round_no"]
        item = by.setdefault(rno, dict(round_no=rno, start_hands=None, events=[]))
        hands = block.get("start_hands")
        if hands and all(hands):
            item["start_hands"] = hands
        item["events"].extend(block["events"])
    out = []
    for rno in sorted(by):
        by[rno]["events"].sort(key=lambda e: e["seq"])
        out.append(by[rno])
    return out


def category(tile):
    if tile == JOKER:
        return "财神"
    idx = TILE_INDEX.get(tile, 99)
    if idx >= 27:
        return "字牌"
    rank = idx % 9 + 1
    if rank in (1, 9):
        return "幺九"
    if rank in (2, 8):
        return "二八"
    return "中张"


def scan(path, focus=None, jokers=None, weights=None):
    """focus 给定时只对这些 uid 的座位调用 choose_discard——盯人模式下其余三家
    的比对结果反正会被丢掉，算它们纯属浪费（这是 4 倍的差别）。"""
    try:
        game = json.load(open(path, encoding="utf-8"))
    except (ValueError, OSError):
        return []
    uids = [s["user_id"] for s in game["seats"]]
    names = [s["name"] for s in game["seats"]]
    out = []
    for rnd in merge_rounds(game):
        if not rnd["start_hands"]:
            continue
        hands = [list(h) for h in rnd["start_hands"]]
        melds = [0] * 4
        try:
            for event in rnd["events"]:
                kind, seat, tile = event["type"], event.get("seat"), event.get("tile")
                data = event.get("data") or {}
                if kind == "tile_drawn":
                    hands[seat].append(tile)
                elif kind == "tile_discarded":
                    hand = list(hands[seat])
                    groups = melds[seat]
                    # 只在"摸牌后 14 张、且不是抓打圈强制"的正常弃牌点比对
                    if (len(hand) + 3 * groups == 14 and not data.get("catch_play")
                            and (focus is None or uids[seat] in focus)
                            and (jokers is None or hand.count(JOKER) == jokers)):
                        mine = choose_discard(hand, groups, 0, 0, {"_weights": weights} if weights else {}, None)
                        # 2026-09-24：分歧时**他打的那张在不在我们的候选层里**。
                        # choose_discard/choose_route_discard 都先用 route_shanten
                        # 把非最小向听的候选**全部砍掉**再打分——如果高手的弃牌
                        # 经常落在层外（gap>=1），那就是结构性封死，任何权重都够不着。
                        gap, his, ours = 0, None, None
                        if mine != tile:
                            # 2026-09-24：层内 95% 的分歧说明双方都在"最快那一层"里挑，
                            # 差别只能是**在同向听下最大化什么**。把他选完/我们选完
                            # 之后的四个量直接摆出来比：进张、对子、手上财神、听口。
                            def _after(pick):
                                rest = list(hand)
                                rest.remove(pick)
                                cnt = to_counts(rest)
                                sh2, waits = combined_route(cnt, groups)
                                return (sh2, len(waits),
                                        sum(1 for x in set(rest) if rest.count(x) >= 2),
                                        rest.count(JOKER))
                            his, ours = _after(tile), _after(mine)
                            currents = {}
                            for cand in set(hand):
                                rest = list(hand)
                                rest.remove(cand)
                                currents[cand] = route_shanten(to_counts(rest), groups)
                            gap = currents.get(tile, 99) - min(currents.values())
                        out.append((uids[seat], names[seat], groups, tile, mine,
                                    hand.count(JOKER), shanten(tuple(to_counts(hand)), groups),
                                    gap, his, ours))
                    hands[seat].remove(tile)
                elif kind == "chi":
                    used = list(data.get("tiles") or [])
                    used.remove(tile)
                    for t in used:
                        hands[seat].remove(t)
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
        except (ValueError, KeyError, TypeError):
            continue
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", nargs="*",
                        default=["models/events/*.json", "tools/models/events/*.json"])
    parser.add_argument("--top", type=int, default=6)
    parser.add_argument("--bottom", type=int, default=6, help="同时对比垫底玩家——若我们更像输家而不像赢家，那是决定性的")
    parser.add_argument("--jobs", type=int, default=6)
    parser.add_argument("--sample", type=int, default=60,
                        help="随机抽 N 个对局文件（每个约 8 局）。全量要跑 200 万次 "
                             "choose_discard，几小时；60 个文件约 3~5 分钟，足够判断同意率高低。"
                             "设 0 表示全量。")
    # 2026-09-24：新增盯人模式。原先只能按"分/局排名"选人，而排名要求 >=150 局
    # ——周榜前列的对手在本地语料里往往只有 3~7 场（双白平胡 5 场、腾蛇 4 场），
    # 全都被门槛挡在外面，恰恰是最该学的人反而看不到。--player 直接按名字锁定，
    # 并且只扫描他出现过的对局文件（否则 60 文件抽样大概率一场都抽不到他）。
    parser.add_argument("--player", nargs="*", default=[],
                        help="按名字盯人，支持子串、可给多个。命中者不受 150 局排名"
                             "门槛与 60 弃牌点门槛限制，且只扫描他出现过的对局文件。")
    parser.add_argument("--weights", default=None,
                        help='临时覆盖权重（JSON），不动 models/weights.json，例如 \'{"rule_std_pair_enabled": 1}\'')
    parser.add_argument("--jokers", type=int, default=None,
                        help="只统计手上恰好有 N 张财神的弃牌点（0 = 无财神手）")
    args = parser.parse_args()

    files, seen = [], set()
    for pattern in args.events:
        for path in sorted(glob.glob(pattern)):
            name = os.path.basename(path)
            if name in seen:
                continue
            seen.add(name)
            files.append(path)

    # 先按分/局排名，取前 N 名 + 我们
    score = defaultdict(lambda: [0, 0.0, ""])
    uid_files = defaultdict(set)
    for path in files:
        try:
            game = json.load(open(path, encoding="utf-8"))
        except (ValueError, OSError):
            continue
        uids = [s["user_id"] for s in game["seats"]]
        for i, uid in enumerate(uids):
            uid_files[uid].add(path)
            score[uid][2] = game["seats"][i]["name"]
        for rnd in game["rounds"]:
            if rnd.get("is_draw"):
                continue
            for i, uid in enumerate(uids):
                item = score[uid]
                item[0] += 1
                item[1] += (rnd.get("scores") or [0] * 4)[i]
                item[2] = game["seats"][i]["name"]
    ranked = sorted([(v[1] / v[0], uid, v[2], v[0]) for uid, v in score.items() if v[0] >= 150],
                    reverse=True)
    picked = ranked[:args.top] + ranked[-args.bottom:]
    focus = {uid: name for _, uid, name, _ in picked}
    named = []
    for uid, item in score.items():
        if item[0] and any(q in item[2] for q in args.player):
            named.append(uid)
            focus[uid] = item[2]
    if args.player and not named:
        print("没有匹配到玩家：%s\n语料里出现过的名字用 --player '' 看不到，"
              "可先跑 tools/joker_playbook.py 看有哪些人。" % " ".join(args.player))
        return
    focus[OUR_UID] = "重生之我是雀神（我们）"
    rank_of = {uid: (i + 1) for i, (_, uid, _, _) in enumerate(ranked)}

    scanned = files
    if named:
        scanned = sorted({path for uid in named for path in uid_files[uid]})
        print("盯人：%s —— 命中 %d 个对局文件（约 %d 局）\n"
              % ("、".join(focus[uid] for uid in named), len(scanned), 8 * len(scanned)))
    elif args.sample and args.sample < len(files):
        import random
        random.seed(20260923)
        scanned = sorted(random.sample(files, args.sample))
        print("抽样 %d / %d 个对局文件（--sample 0 可跑全量）\n" % (len(scanned), len(files)))
    from functools import partial
    worker = partial(scan, focus=(frozenset(named) | {OUR_UID}) if named else None,
                     jokers=args.jokers, weights=json.loads(args.weights) if args.weights else None)
    rows = []
    with Pool(args.jobs) as pool:
        for done, sub in enumerate(pool.imap_unordered(worker, scanned, chunksize=2), 1):
            rows.extend(sub)
            if done % 20 == 0 or done == len(scanned):
                print("  进度 %d / %d 个文件" % (done, len(scanned)), flush=True)
    if args.jokers is not None:
        rows = [r for r in rows if r[5] == args.jokers]
        print("只看手上 %d 张财神的弃牌点：%d 个\n" % (args.jokers, len(rows)))

    agree = defaultdict(lambda: [0, 0])
    keep = defaultdict(Counter)     # 他留着、我们会打掉的牌
    drop = defaultdict(Counter)     # 他打掉、我们会留着的牌
    for uid, name, groups, actual, mine, jokers, sh, gap, his, ours in rows:
        if uid not in focus or mine is None:
            continue
        cell = agree[uid]
        cell[1] += 1
        if actual == mine:
            cell[0] += 1
        else:
            keep[uid][category(mine)] += 1      # 我们想打 mine，他留下了
            drop[uid][category(actual)] += 1    # 他打了 actual，我们想留

    print("%-20s %5s %7s %8s %9s   %s"
          % ("玩家", "名次", "分/局", "弃牌点", "与我们一致", "分歧时：他打了什么 / 他留下了什么"))
    print("-" * 112)
    order = ([(score[uid][1] / score[uid][0], uid, focus[uid], score[uid][0]) for uid in named]
             + ranked[:args.top]
             + [(score[OUR_UID][1] / score[OUR_UID][0], OUR_UID, focus[OUR_UID], 0)]
             + ranked[-args.bottom:])
    shown = set()
    for rate, uid, name, n in order:
        if uid in shown:
            continue
        shown.add(uid)
        if uid not in agree or agree[uid][1] < (1 if uid in named else 60):
            continue
        same, total = agree[uid]
        d = ", ".join("%s%d%%" % (k, round(100 * v / max(1, sum(drop[uid].values()))))
                      for k, v in drop[uid].most_common(3))
        k = ", ".join("%s%d%%" % (kk, round(100 * v / max(1, sum(keep[uid].values()))))
                      for kk, v in keep[uid].most_common(3))
        print("%-20s %5s %+7.2f %8d %8.1f%%   打:%-26s 留:%s"
              % (name[:18], rank_of.get(uid, "-"), rate, total, 100 * same / total, d, k))

    # 2026-09-24：分歧率的**平均值**没法指导改代码——需要知道分歧集中在哪种局面。
    # 实测我们的 1露 收入占比 28%（全场最低）、2露 57%（全场最高），怀疑"第一口
    # 吃完手牌没变好、只好再吃第二口"，所以按 副露组数 × 向听 拆开看。
    cell = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    cell_drop = defaultdict(lambda: defaultdict(Counter))
    for uid, name, groups, actual, mine, jokers, sh, gap, his, ours in rows:
        if uid not in focus or mine is None:
            continue
        key = ("%d露" % min(groups, 2), "听牌" if sh <= 0 else ("%d向" % min(sh, 3)))
        box = cell[uid][key]
        box[1] += 1
        if actual == mine:
            box[0] += 1
        else:
            cell_drop[uid][key][category(actual)] += 1

    tier = defaultdict(Counter)
    for uid, name, groups, actual, mine, jokers, sh, gap, his, ours in rows:
        if uid not in focus or mine is None or actual == mine:
            continue
        tier[uid]["总分歧"] += 1
        tier[uid]["层内(gap=0)" if gap <= 0 else ("层外 gap=%d" % min(gap, 3))] += 1
    print("\n=== 分歧时，他打的那张在不在我们的候选层里 ===")
    print("（我们先用 route_shanten 砍掉所有非最小向听的候选，再打分。"
          "层外 = 他主动退向听换牌型，我们结构上做不到）")
    for uid in [u for u in named if u in tier] + ([OUR_UID] if OUR_UID in tier else []):
        total = tier[uid]["总分歧"]
        if not total:
            continue
        parts = "  ".join("%s %.0f%%" % (k, 100.0 * v / total)
                          for k, v in sorted(tier[uid].items()) if k != "总分歧")
        print("%-22s n=%-6d %s" % (focus.get(uid, uid)[:20], total, parts))

    opt = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0, 0]))   # uid -> 段 -> [他, 我们, n]
    LABELS = ("进张", "对子", "手上财神")
    for uid, name, groups, actual, mine, jokers, sh, gap, his, ours in rows:
        if uid not in focus or mine is None or actual == mine or not his:
            continue
        bucket = "听牌" if sh <= 0 else ("%d向" % min(sh, 3))
        for i, label in enumerate(LABELS):
            box = opt[uid][(bucket, label)]
            box[0] += his[i + 1]
            box[1] += ours[i + 1]
            box[2] += 1
    print("\n=== 同向听下他在最大化什么（分歧点上，选完之后的手牌）===")
    for uid in [u for u in named if u in opt] + ([OUR_UID] if OUR_UID in opt else []):
        print("\n%s" % focus.get(uid, uid))
        print("       %s" % "  ".join("%-22s" % ("他的选择 / 我们的选择（%s）" % l) for l in LABELS))
        for bucket in ("3向", "2向", "1向", "听牌"):
            cells = []
            for label in LABELS:
                h, o, n = opt[uid][(bucket, label)]
                cells.append("%-22s" % ("-" if n < 20
                                        else "%.2f / %.2f  (%+.2f)" % (h / n, o / n, (h - o) / n)))
            print("%-6s %s" % (bucket, "  ".join(cells)))
    print("\n括号里是「他 − 我们」。哪一列稳定为正，他就是在最大化那个量。")

    print("\n=== 分歧集中在哪种局面（行=副露组数，列=向听）===")
    cols = ["3向", "2向", "1向", "听牌"]
    for uid in [u for u in named if u in cell] + [OUR_UID]:
        if uid not in cell:
            continue
        print("\n%s" % focus.get(uid, uid))
        print("      %s" % "  ".join("%-14s" % c for c in cols))
        for row in ("0露", "1露", "2露"):
            line = []
            for col in cols:
                same, n = cell[uid][(row, col)]
                if n < 8:
                    line.append("%-14s" % "-")
                else:
                    top = cell_drop[uid][(row, col)].most_common(1)
                    tag = top[0][0] if top else ""
                    line.append("%-14s" % ("%.0f%%分歧 %s" % (100.0 * (n - same) / n, tag)))
            print("%-5s %s" % (row, "  ".join(line)))
    print("\n每格 <8 样本显示 '-'。格子里的词 = 分歧时**他**打出的牌属于哪一类"
          "（我们会留着不打）。")


if __name__ == "__main__":
    main()
