"""离线推演：持财神手的弃牌，逐个候选模拟打完这一局，看哪张牌的期望得分最高。

    python3 tools/rollout.py                       # 默认 600 个局面 × 每个候选 40 次推演
    python3 tools/rollout.py --states 300 --rollouts 24     # 先快速看一眼

做什么：
  1. 从全部对局里抽「手上有财神、不在财飘链上、弃后 ≤2 向听、同向听层 ≥2 张候选」的真实弃牌局面。
  2. 对每张候选牌，用同一组随机牌序（公共随机数，候选之间只差这一张牌）模拟后面的摸打：
     我们之后的每一步都调用线上完整决策（mj.bot.choose_action：胡/弃胡转爆头/财飘/杠/弃牌），
     别家不建模手牌，只按真实数据标定的「每一巡有人自摸」的危险率结束本局。
  3. 得分：自己胡 = 番 × (庄 24 / 闲 10)；别人先胡 = −(真实数据里被自摸时的平均支付)；流局 = 0。
输出：
  - 线上策略 / 高手实际选择 与「推演最优」的期望分差（遗憾值），按 财神数 × 副露 分格；
  - data/analysis/rollout_labels.jsonl：以推演最优为标签的样本，格式与 discard_fit 的 dj 行一致，
    可直接 `python3 tools/discard_fit.py fit --inp data/analysis/rollout_labels.jsonl --kinds dj`
    拟合出「按推演最优」的 fj_ 权重（报告里写的「高手」在这里指「推演最优」）。

局限（如实）：别家不吃碰、不喂牌，我们也不吃碰（只模拟自摸路线）；抓打圈不建模。
所以它比较的是「同一手牌打哪张更容易自摸、胡得更大」，正是持财神弃牌要回答的问题。
耗时：每个局面约 30~60 秒单核；默认参数 6 进程约 1 小时。**跑的时候不要开房。**
"""
import argparse
import hashlib
import json
import os
import random
import sys
from collections import Counter, defaultdict
from multiprocessing import Pool

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import mj.fit as _fit  # noqa: E402
from mining_common import discover_files, merge_rounds  # noqa: E402
from mining_common import seat_names  # noqa: E402
from mj.discard_features import J_FEATURES, features_joker  # noqa: E402
from mj.shanten import route_shanten  # noqa: E402
from mj.tiles import ALL_TILES, TILE_INDEX, to_counts  # noqa: E402

OUR = "重生之我是雀神"
JOKER = "白"
MASTERS = {"⭐꧁༺🀆🀆🀆🀆༻꧂⭐", "Deepseek胡", "爆头研究所", "Astra-0", "晴总总，该请桂语山房了",
           "glm-flash", "铳一色14", "歪比巴卜肉蛋葱鸡", "放假了偷偷训练", "康陶应雀"}
OUT = "data/analysis/rollout_labels.jsonl"


def _freeze_weights():
    """推演里每一步都调线上决策；load_weights 每次读文件太慢，这里读一次后固定。"""
    frozen = _fit.load_weights()
    for name, mod in list(sys.modules.items()):
        if name.startswith("mj") and hasattr(mod, "load_weights"):
            mod.load_weights = lambda path="models/weights.json", _w=frozen: _w
    return frozen


# ---------------------------------------------------------------- 1. 抽局面 + 标定危险率/支付

def collect_file(args):
    path, sample = args[:2]
    any_hand = len(args) > 2 and args[2]          # 逐房复盘：不限持财神，只看我们
    only = OUR if any_hand else None
    out, stats = [], Counter()
    try:
        with open(path, encoding="utf-8") as source:
            game = json.load(source)
    except (OSError, ValueError):
        return out, stats
    names = seat_names(game)
    if len(names) != 4:
        return out, stats
    info = {r.get("round_no"): r for r in game.get("rounds") or []}
    base = os.path.basename(path)
    for rnd in merge_rounds(game):
        meta = info.get(rnd["round_no"])
        if not meta or rnd.get("truncated") or not rnd.get("start_hands"):
            continue
        dealer = meta.get("dealer", rnd.get("dealer"))
        winner = None if meta.get("is_draw") else meta.get("winner")
        scores = meta.get("scores") or [0] * 4
        hands = [list(h) for h in rnd["start_hands"]]
        total_start = sum(len(h) for h in hands)
        drawn = 0
        melds = [[] for _ in range(4)]
        seen = [0] * 34
        piaoed = [False] * 4
        draws = [0] * 4
        try:
            for ev in rnd["events"]:
                kind, seat, tile = ev["type"], ev.get("seat"), ev.get("tile")
                data = ev.get("data") or {}
                if kind == "tile_drawn":
                    hands[seat].append(tile)
                    drawn += 1
                    draws[seat] += 1
                elif kind == "tile_discarded":
                    hand = hands[seat]
                    if ((any_hand or JOKER in hand) and (only is None or names[seat] == only)
                            and not piaoed[seat] and not data.get("catch_play")
                            and len(hand) + 3 * len(melds[seat]) == 14
                            and _keep("%s|%s" % (base, ev.get("seq")), sample)):
                        tier = _tier(hand, len(melds[seat]))
                        if tier and len(tier[1]) >= 2 and tier[0] <= 2 and tile in tier[1]:
                            name = names[seat]
                            out.append({
                                "f": base, "p": name,
                                "g": "ours" if name == OUR else ("master" if name in MASTERS else "other"),
                                "hand": list(hand), "melds": [dict(m) for m in melds[seat]],
                                "seen": list(seen), "wall": 136 - total_start - drawn,
                                "dealer": int(seat == dealer), "turn": draws[seat], "actual": tile,
                                "tier": tier[1], "shanten": tier[0], "round_no": rnd["round_no"],
                            })
                    hand.remove(tile)
                    seen[TILE_INDEX[tile]] += 1
                    if tile == JOKER:
                        piaoed[seat] = True
                elif kind == "chi":
                    used = list(data.get("tiles") or [])
                    used.remove(tile)
                    for t in used:
                        hands[seat].remove(t)
                        seen[TILE_INDEX[t]] += 1
                    melds[seat].append({"kind": "chi", "tiles": list(data.get("tiles") or [])})
                elif kind == "peng":
                    for _ in range(2):
                        hands[seat].remove(tile)
                    seen[TILE_INDEX[tile]] += 2
                    melds[seat].append({"kind": "peng", "tiles": [tile] * 3})
                elif kind == "gang":
                    gk = data.get("kind")
                    take = {"an": 4, "ming": 3, "bu": 1}[gk]
                    for _ in range(take):
                        hands[seat].remove(tile)
                    seen[TILE_INDEX[tile]] += take
                    if gk == "bu":
                        for m in melds[seat]:
                            if m["kind"] == "peng" and m["tiles"][0] == tile:
                                m["kind"], m["tiles"] = "gang", [tile] * 4
                                break
                    else:
                        melds[seat].append({"kind": "gang", "tiles": [tile] * 4})
        except (ValueError, KeyError, IndexError, TypeError):
            continue
        _round_stats(stats, draws, winner, dealer, scores)
    return out, stats


def _round_stats(stats, draws, winner, dealer, scores):
    """危险率：每位玩家第 k 次摸牌时自摸的条件概率；支付：别人自摸时本座平均得分（按是否坐庄）。"""
    for s in range(4):
        for k in range(1, draws[s] + 1):
            stats["alive_%d" % min(k, 25)] += 1
        if winner == s and draws[s]:
            stats["win_%d" % min(draws[s], 25)] += 1
        if winner is not None and winner != s:
            key = "d" if s == dealer else "n"
            stats["pay_sum_" + key] += scores[s]
            stats["pay_n_" + key] += 1


def _keep(key, rate):
    return int(hashlib.md5(key.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF < rate


def _tier(hand, groups):
    cur = {}
    for t in set(hand):
        rest = list(hand)
        rest.remove(t)
        cur[t] = route_shanten(to_counts(rest), groups)
    best = min(cur.values())
    return best, sorted(t for t, v in cur.items() if v == best)


# ---------------------------------------------------------------- 2. 推演

_CAL = {}


def _init_eval(cal):
    _CAL.update(cal)
    import mj.bot  # noqa: F401  先把决策链上的模块都导入，再统一冻结权重
    _freeze_weights()


def _snapshot(hand, drawn, melds, seen, wall, dealer, chain, river):
    return {
        "phase": "draw", "turn": 0, "seat": 0, "dealer": 0 if dealer else 1,
        "my_hand": list(hand), "drawn_tile": drawn, "wall_remaining": wall,
        "melds": [melds, [], [], []], "discards": [river, [], [], []],
        "god": {"chain_count": chain, "baotou": False, "catch_play": False, "god_discarder_seat": -1},
        "scores": [0, 0, 0, 0], "round_no": 1,
    }


def _resp_snapshot(phase, hand, tile, melds, river, wall, dealer, chain):
    snap = _snapshot(hand, None, melds, None, wall, dealer, chain, river)
    snap.update(phase=phase, turn=3, responding_seats=[0], window_tile=tile, last_discard=tile)
    return snap


class _Sim:
    """一局剩余部分的推演状态（只有我们一家有手牌）。"""

    def __init__(self, hand13, melds, seen, wall, dealer, pool):
        self.hand = list(hand13)
        self.melds = [dict(m, tiles=list(m["tiles"])) for m in melds]
        self.pool = list(pool)
        self.river = [ALL_TILES[i] for i in range(34) for _ in range(seen[i])]
        self.chain = 0
        self.wall = wall
        self.dealer = dealer
        self.rate = 24 if dealer else 10

    def draw(self, front=False):
        if self.wall <= 0 or not self.pool:
            return None
        self.wall -= 1
        return self.pool.pop(0) if front else self.pool.pop()

    def discard(self, tile):
        self.hand.remove(tile)
        self.river.append(tile)
        if tile == JOKER:
            self.chain = min(self.chain + 1, 3)

    def discard_after_claim(self):
        from mj.bot import choose_discard as bot_discard
        snap = _snapshot(self.hand, None, self.melds, None, self.wall, self.dealer, self.chain, self.river)
        tile = (bot_discard(snap) or {}).get("tile")
        self.discard(tile if tile in self.hand else self.hand[-1])

    def our_turn(self, drawn, gang_open=False):
        """摸到 drawn 之后的完整决策；返回得分（胡了）或 None（打出一张，继续）。"""
        from mj.bot import choose_action, hu_result
        while True:
            self.hand.append(drawn)
            snap = _snapshot(self.hand, drawn, self.melds, None, self.wall, self.dealer, self.chain, self.river)
            act = choose_action(snap, gang_open=gang_open) or {"action": "discard", "tile": drawn}
            if act["action"] == "hu":
                res = hu_result(snap, gang_open=gang_open) or {"fan": 1}
                return float(res.get("fan", 1) * self.rate)
            gang_open = False
            if act["action"] == "gang":
                t = act["tile"]
                bu = next((m for m in self.melds if m["kind"] == "peng" and m["tiles"][0] == t), None)
                if bu is not None and self.hand.count(t) >= 1:
                    self.hand.remove(t)
                    bu["kind"], bu["tiles"] = "gang", [t] * 4
                elif self.hand.count(t) >= 4:
                    for _ in range(4):
                        self.hand.remove(t)
                    self.melds.append({"kind": "gang", "tiles": [t] * 4})
                else:
                    act = {"action": "discard", "tile": drawn}
                if act["action"] == "gang":
                    drawn = self.draw(front=True)          # 杠后从牌尾补
                    if drawn is None:
                        return 0.0
                    gang_open = True
                    continue
            tile = act.get("tile") or drawn
            self.discard(tile if tile in self.hand else drawn)
            return None

    def apply_claim(self, kind, tile, tiles=None):
        """执行一次 碰/吃（明杠另行处理）并完成声明后的强制弃牌。"""
        if kind == "peng":
            for _ in range(2):
                self.hand.remove(tile)
            self.melds.append({"kind": "peng", "tiles": [tile] * 3})
        else:
            for t in tiles:
                self.hand.remove(t)
            self.melds.append({"kind": "chi", "tiles": sorted(list(tiles) + [tile])})
        self.discard_after_claim()

    def respond(self, tile, left):
        """别家打出 tile 时按线上逻辑决定 明杠/碰/吃/过；返回 得分 / "claimed" / None。"""
        from mj.bot import choose_action
        snap = _resp_snapshot("response_peng", self.hand, tile, self.melds, self.river, self.wall,
                              self.dealer, self.chain)
        act = choose_action(snap) or {"action": "pass"}
        if act["action"] == "gang" and self.hand.count(tile) >= 3:
            for _ in range(3):
                self.hand.remove(tile)
            self.melds.append({"kind": "gang", "tiles": [tile] * 4})
            drawn = self.draw(front=True)
            if drawn is None:
                return 0.0
            res = self.our_turn(drawn, gang_open=True)
            return res if res is not None else "claimed"
        if act["action"] == "peng" and self.hand.count(tile) >= 2:
            self.apply_claim("peng", tile)
            return "claimed"
        if left:
            snap = _resp_snapshot("response_chi", self.hand, tile, self.melds, self.river, self.wall,
                                  self.dealer, self.chain)
            snap["responding_seats"] = [0]
            act = choose_action(snap) or {"action": "pass"}
            if act["action"] == "chi" and all(t in self.hand for t in act.get("tiles") or [None]):
                self.apply_claim("chi", tile, act["tiles"])
                return "claimed"
        return None


def run_sim(sim, turn0, hazard_draws, pre_opp=3, claims=True, max_draws=18):
    """从「pre_opp 家别家先行动」开始推演到本局结束。别家摸什么打什么（牌序随机），
    每家每次摸牌按真实危险率自摸结束本局；打出的牌我们按线上逻辑吃碰杠（claims=True）。"""
    haz = iter(hazard_draws)
    n_opp = pre_opp
    for step in range(max_draws):
        h = _CAL["hazard"].get(str(min(turn0 + step + 1, 25)), 0.03)
        claimed = False
        for j in range(n_opp):
            t = sim.draw()
            if t is None:
                return 0.0
            if next(haz) < h:
                return _CAL["pay_d" if sim.dealer else "pay_n"]
            sim.river.append(t)
            if claims and t != JOKER:
                res = sim.respond(t, left=(j == n_opp - 1))
                if res == "claimed":
                    claimed = True
                    break
                if res is not None:
                    return res
        n_opp = 3
        if claimed:
            continue
        drawn = sim.draw()
        if drawn is None:
            return 0.0
        res = sim.our_turn(drawn)
        if res is not None:
            return res
    return 0.0


def simulate(hand13, melds, seen, wall, dealer, turn0, pool, hazard_draws, pre_opp=3, claims=True):
    return run_sim(_Sim(hand13, melds, seen, wall, dealer, pool), turn0, hazard_draws, pre_opp, claims)


def evaluate_state(args):
    idx, st, rollouts = args
    unseen = []
    own = Counter(st["hand"])
    for i, t in enumerate(ALL_TILES):
        unseen += [t] * max(0, 4 - st["seen"][i] - own[t])
    ev = {c: [] for c in st["tier"]}
    for r in range(rollouts):
        rng = random.Random("%s|%d|%d" % (st["f"], idx, r))
        pool = list(unseen)
        rng.shuffle(pool)
        haz = [rng.random() for _ in range(200)]
        for c in st["tier"]:
            hand13 = list(st["hand"])
            hand13.remove(c)
            seen = list(st["seen"])
            seen[TILE_INDEX[c]] += 1
            ev[c].append(simulate(hand13, st["melds"], seen, st["wall"], st["dealer"], st["turn"],
                                  pool, haz))
    from mj.bot import choose_discard as bot_discard
    snap = _snapshot(st["hand"], None, st["melds"], st["seen"], st["wall"], st["dealer"], 0,
                     [ALL_TILES[i] for i in range(34) for _ in range(st["seen"][i])])
    prod = (bot_discard(snap) or {}).get("tile")
    means = {c: sum(v) / len(v) for c, v in ev.items()}
    return idx, means, prod


# ---------------------------------------------------------------- 2b. 吃碰决策点：接 vs 过

RESPONSE_TYPES = ("pass", "timeout", "chi", "peng", "gang")


def _chi_options(hand, tile):
    index = TILE_INDEX.get(tile)
    if index is None or index >= 27:
        return []
    out = []
    for offsets in ((-2, -1), (-1, 1), (1, 2)):
        idxs = [index + o for o in offsets]
        if min(idxs) < 0 or max(idxs) >= 27 or any(i // 9 != index // 9 for i in idxs):
            continue
        names = [ALL_TILES[i] for i in idxs]
        if all(hand.count(n) for n in names):
            out.append(names)
    return out


def collect_claims_file(args):
    path, sample = args
    out, stats = [], Counter()
    try:
        with open(path, encoding="utf-8") as source:
            game = json.load(source)
    except (OSError, ValueError):
        return out, stats
    names = seat_names(game)
    if len(names) != 4:
        return out, stats
    info = {r.get("round_no"): r for r in game.get("rounds") or []}
    base = os.path.basename(path)
    for rnd in merge_rounds(game):
        meta = info.get(rnd["round_no"])
        if not meta or rnd.get("truncated") or not rnd.get("start_hands"):
            continue
        dealer = meta.get("dealer", rnd.get("dealer"))
        winner = None if meta.get("is_draw") else meta.get("winner")
        hands = [list(h) for h in rnd["start_hands"]]
        total_start = sum(len(h) for h in hands)
        drawn = 0
        melds = [[] for _ in range(4)]
        seen = [0] * 34
        draws = [0] * 4
        events = rnd["events"]
        try:
            for idx, ev in enumerate(events):
                kind, seat, tile = ev["type"], ev.get("seat"), ev.get("tile")
                data = ev.get("data") or {}
                if kind == "tile_drawn":
                    hands[seat].append(tile)
                    drawn += 1
                    draws[seat] += 1
                elif kind == "tile_discarded":
                    hands[seat].remove(tile)
                    seen[TILE_INDEX[tile]] += 1
                    if tile != JOKER and not data.get("catch_play"):
                        new = _claim_points(events, idx, seat, tile, hands, melds, seen, draws, dealer,
                                            136 - total_start - drawn, names, base, sample)
                        for row in new:
                            row["round_no"] = rnd["round_no"]
                        out += new
                elif kind == "chi":
                    used = list(data.get("tiles") or [])
                    used.remove(tile)
                    for t in used:
                        hands[seat].remove(t)
                        seen[TILE_INDEX[t]] += 1
                    melds[seat].append({"kind": "chi", "tiles": list(data.get("tiles") or [])})
                elif kind == "peng":
                    for _ in range(2):
                        hands[seat].remove(tile)
                    seen[TILE_INDEX[tile]] += 2
                    melds[seat].append({"kind": "peng", "tiles": [tile] * 3})
                elif kind == "gang":
                    gk = data.get("kind")
                    take = {"an": 4, "ming": 3, "bu": 1}[gk]
                    for _ in range(take):
                        hands[seat].remove(tile)
                    seen[TILE_INDEX[tile]] += take
                    if gk == "bu":
                        for m in melds[seat]:
                            if m["kind"] == "peng" and m["tiles"][0] == tile:
                                m["kind"], m["tiles"] = "gang", [tile] * 4
                                break
                    else:
                        melds[seat].append({"kind": "gang", "tiles": [tile] * 4})
        except (ValueError, KeyError, IndexError, TypeError):
            continue
        _round_stats(stats, draws, winner, dealer, meta.get("scores") or [0] * 4)
    return out, stats


def _claim_points(events, idx, disc, tile, hands, melds, seen, draws, dealer, wall, names, base, sample):
    window = []
    for ev2 in events[idx + 1:idx + 13]:
        if ev2["type"] not in RESPONSE_TYPES:
            break
        window.append(ev2)
    accepted = next(((e.get("seat"), e["type"], e.get("data") or {}) for e in window
                     if e["type"] in ("chi", "peng", "gang")), None)
    passed = {e.get("seat") for e in window if e["type"] == "pass"}
    out = []
    for r in range(4):
        if r == disc:
            continue
        name = names[r]
        g = "ours" if name == OUR else ("master" if name in MASTERS else None)
        if not g or not _keep("%s|%s|%d" % (base, events[idx].get("seq"), r), sample):
            continue
        opts = [["pass"]]
        if hands[r].count(tile) >= 2:
            opts.append(["peng"])
        left = r == (disc + 1) % 4
        if left and sum(m["kind"] == "chi" for m in melds[r]) < 2:
            opts += [["chi", names_] for names_ in _chi_options(hands[r], tile)]
        if len(opts) == 1:
            continue
        if accepted and accepted[0] == r:
            if accepted[1] == "peng":
                actual = 1 if opts[1] == ["peng"] else None
            elif accepted[1] == "chi":
                used = list(accepted[2].get("tiles") or [])
                if tile in used:
                    used.remove(tile)
                actual = next((i for i, o in enumerate(opts)
                               if o[0] == "chi" and sorted(o[1]) == sorted(used)), None)
            else:
                actual = None                      # 明杠不在比较范围
        elif r in passed:
            actual = 0
        else:
            actual = None                          # 超时代打 / 被别家抢先而本人未表态
        if actual is None:
            continue
        out.append({"f": base, "p": name, "g": g, "hand": list(hands[r]),
                    "melds": [dict(m, tiles=list(m["tiles"])) for m in melds[r]], "seen": list(seen),
                    "wall": wall, "dealer": int(r == dealer), "turn": draws[r], "tile": tile,
                    "pre_opp": (r - disc - 1) % 4, "left": int(left), "opts": opts, "actual": actual})
    return out


def evaluate_claim(args):
    idx, st, rollouts = args
    from mj.bot import choose_action
    unseen = []
    own = Counter(st["hand"])
    for i, t in enumerate(ALL_TILES):
        unseen += [t] * max(0, 4 - st["seen"][i] - own[t])
    vals = [[] for _ in st["opts"]]
    for r in range(rollouts):
        rng = random.Random("%s|c%d|%d" % (st["f"], idx, r))
        pool = list(unseen)
        rng.shuffle(pool)
        haz = [rng.random() for _ in range(300)]
        for k, opt in enumerate(st["opts"]):
            sim = _Sim(st["hand"], st["melds"], st["seen"], st["wall"], st["dealer"], pool)
            if opt[0] == "pass":
                vals[k].append(run_sim(sim, st["turn"], haz, pre_opp=st["pre_opp"]))
            else:
                sim.apply_claim(opt[0], st["tile"], opt[1] if len(opt) > 1 else None)
                vals[k].append(run_sim(sim, st["turn"], haz, pre_opp=3))
    river = [ALL_TILES[i] for i in range(34) for _ in range(st["seen"][i])]
    snap = _resp_snapshot("response_peng", st["hand"], st["tile"], st["melds"], river, st["wall"],
                          st["dealer"], 0)
    act = choose_action(snap) or {}
    prod = 0
    if act.get("action") in ("peng", "gang") and ["peng"] in st["opts"]:
        prod = st["opts"].index(["peng"])
    elif st["left"]:
        snap = _resp_snapshot("response_chi", st["hand"], st["tile"], st["melds"], river, st["wall"],
                              st["dealer"], 0)
        act = choose_action(snap) or {}
        if act.get("action") == "chi":
            prod = next((i for i, o in enumerate(st["opts"]) if o[0] == "chi"
                         and sorted(o[1]) == sorted(act.get("tiles") or [])), 0)
    return idx, [sum(v) / len(v) for v in vals], prod


def main_claim(args, states, cal):
    random.Random(20260926).shuffle(states)
    half = args.states // 2
    ours = [s for s in states if s["g"] == "ours"][:half]
    mast = [s for s in states if s["g"] == "master"][:args.states - len(ours)]
    states = ours + mast
    print("推演 %d 个吃碰决策点（我们 %d / 高手 %d），每个选项 %d 次……" % (
        len(states), len(ours), len(mast), args.rollouts))
    cell = defaultdict(Counter)
    with Pool(args.jobs, initializer=_init_eval, initargs=(cal,)) as pool:
        tasks = [(i, s, args.rollouts) for i, s in enumerate(states)]
        for done, (i, ev, prod) in enumerate(pool.imap_unordered(evaluate_claim, tasks), 1):
            st = states[i]
            best = max(range(len(ev)), key=lambda k: ev[k])
            kind = "碰" if ["peng"] in st["opts"] else "吃"
            key = (kind, min(st["hand"].count(JOKER), 2), min(len(st["melds"]), 2))
            c = cell[key]
            c["n"] += 1
            c["gain"] += max(ev[1:]) - ev[0]               # 最好的接法 相对 过
            c["acc_better"] += max(ev[1:]) > ev[0]
            c["prod_regret"] += ev[best] - ev[prod]
            c["prod_acc"] += prod > 0
            if prod > 0 and ev[0] > ev[prod]:
                c["wrong_acc"] += 1
                c["wrong_acc_loss"] += ev[0] - ev[prod]
            if prod == 0 and max(ev[1:]) > ev[0]:
                c["wrong_pass"] += 1
                c["wrong_pass_loss"] += max(ev[1:]) - ev[0]
            g = st["g"]
            c[g + "_n"] += 1
            c[g + "_regret"] += ev[best] - ev[st["actual"]]
            c[g + "_acc"] += st["actual"] > 0
            if done % 25 == 0 or done == len(states):
                print("  推演 %d / %d" % (done, len(states)), flush=True)

    print("\n=== 吃碰：接 vs 过 的期望分（每个决策点，分）===")
    print("%-14s %5s %9s %8s | %-24s | %-22s | %-22s" % (
        "窗口/财神/副露", "点数", "接比过多", "接更好占", "线上 遗憾 / 接受率", "高手 遗憾 / 接受率", "我们 遗憾 / 接受率"))
    tot = Counter()
    for key in sorted(cell):
        c = cell[key]
        tot.update(c)
        _claim_row("%s/%s白/%s露" % (key[0], "2+" if key[1] == 2 else key[1], "2+" if key[2] == 2 else key[2]), c)
    _claim_row("合计", tot)
    print("\n线上策略的错误拆分：")
    print("  该过却接：%d 次，平均每次亏 %.2f 分" % (tot["wrong_acc"], tot["wrong_acc_loss"] / max(tot["wrong_acc"], 1)))
    print("  该接却过：%d 次，平均每次亏 %.2f 分" % (tot["wrong_pass"], tot["wrong_pass_loss"] / max(tot["wrong_pass"], 1)))
    print("\n读法：「接比过多」= 最好的那种接法比「过」多的期望分（负 = 这类窗口整体不该接）。")
    print("遗憾 = 推演最优选项 − 实际选项 的期望分；线上遗憾明显大于高手的格子，就是吃碰规则该改的地方。")


def _claim_row(label, c):
    n = max(c["n"], 1)
    print("%-14s %5d %+9.2f %7.0f%% | %6.2f / %5.1f%%          | %6.2f / %5.1f%% n=%-4d | %6.2f / %5.1f%% n=%-4d" % (
        label, c["n"], c["gain"] / n, 100 * c["acc_better"] / n,
        c["prod_regret"] / n, 100 * c["prod_acc"] / n,
        c["master_regret"] / max(c["master_n"], 1), 100 * c["master_acc"] / max(c["master_n"], 1), c["master_n"],
        c["ours_regret"] / max(c["ours_n"], 1), 100 * c["ours_acc"] / max(c["ours_n"], 1), c["ours_n"]))


# ---------------------------------------------------------------- 2c. 逐房复盘：本房我们每一个可选决策

CAL_CACHE = "models/rollout_cal.json"


def _calibration(files, jobs):
    """危险率/支付标定：全量语料扫一次后缓存（逐房复盘不必每次重扫）。"""
    if os.path.exists(CAL_CACHE):
        with open(CAL_CACHE, encoding="utf-8") as source:
            return json.load(source)
    stats = Counter()
    with Pool(jobs) as pool:
        for _rows, st in pool.imap_unordered(collect_file, [(f, 0.0) for f in files], chunksize=8):
            stats.update(st)
    cal = {"hazard": {str(k): stats["win_%d" % k] / stats["alive_%d" % k]
                      for k in range(1, 26) if stats["alive_%d" % k]},
           "pay_n": stats["pay_sum_n"] / max(stats["pay_n_n"], 1),
           "pay_d": stats["pay_sum_d"] / max(stats["pay_n_d"], 1)}
    with open(CAL_CACHE, "w", encoding="utf-8") as sink:
        json.dump(cal, sink)
    return cal


def _option_values(args):
    idx, st, rollouts, tag = args
    unseen = []
    own = Counter(st["hand"])
    for i, t in enumerate(ALL_TILES):
        unseen += [t] * max(0, 4 - st["seen"][i] - own[t])
    vals = [[] for _ in st["opts"]]
    for r in range(rollouts):
        rng = random.Random("%s|%s|%d|%d" % (st["f"], tag, idx, r))
        pool = list(unseen)
        rng.shuffle(pool)
        haz = [rng.random() for _ in range(300)]
        for k, opt in enumerate(st["opts"]):
            if opt[0] == "discard":
                hand13 = list(st["hand"])
                hand13.remove(opt[1])
                seen = list(st["seen"])
                seen[TILE_INDEX[opt[1]]] += 1
                sim = _Sim(hand13, st["melds"], seen, st["wall"], st["dealer"], pool)
                vals[k].append(run_sim(sim, st["turn"], haz, pre_opp=3))
            elif opt[0] == "pass":
                sim = _Sim(st["hand"], st["melds"], st["seen"], st["wall"], st["dealer"], pool)
                vals[k].append(run_sim(sim, st["turn"], haz, pre_opp=st["pre_opp"]))
            else:
                sim = _Sim(st["hand"], st["melds"], st["seen"], st["wall"], st["dealer"], pool)
                sim.apply_claim(opt[0], st["tile"], opt[1] if len(opt) > 1 else None)
                vals[k].append(run_sim(sim, st["turn"], haz, pre_opp=3))
    return idx, vals


def _desc(st, k):
    opt = st["opts"][k]
    if opt[0] == "discard":
        return "打%s" % opt[1]
    if opt[0] == "chi":
        return "吃(%s)" % "".join(opt[1])
    return {"pass": "过", "peng": "碰"}[opt[0]]


def main_rooms(args):
    import glob
    files = discover_files()
    cal = _calibration(files, args.jobs)
    room_files = sorted({p for room in args.rooms for p in glob.glob("models/events/%s_*.json" % room)
                         + glob.glob("tools/models/events/%s_*.json" % room)})
    states = []
    for path in room_files:
        for st in collect_file((path, 1.0, True))[0]:
            st.update(kind="弃牌", opts=[["discard", t] for t in st["tier"]], actual=st["tier"].index(st["actual"]))
            states.append(st)
        for st in collect_claims_file((path, 1.0))[0]:
            if st["g"] == "ours":
                st["kind"] = "碰" if ["peng"] in st["opts"] else "吃"
                states.append(st)
    rounds = 0
    for path in room_files:
        with open(path, encoding="utf-8") as source:
            rounds += len(json.load(source).get("rounds") or [])
    print("房间 %s：%d 个对局文件、%d 局；我们可选择的决策点 %d 个（弃牌 %d / 吃碰 %d）" % (
        " ".join(args.rooms), len(room_files), rounds, len(states),
        sum(s["kind"] == "弃牌" for s in states), sum(s["kind"] != "弃牌" for s in states)))
    if not states:
        print("没有决策点（先用 pipeline.py collect 采集这些房）")
        return
    total_points = len(states)
    random.Random(20260926).shuffle(states)
    states = states[:args.max_points]
    scale = total_points / len(states)
    print("随机抽 %d 个决策点推演（结论按 %.1f 倍折算回全部决策点）" % (len(states), scale))

    # 第一轮：每个选项 R1 次推演，筛出「另一个选项明显更好」的点；结果存盘，中断后重跑直接复用
    cache = "data/analysis/room_audit_%s.json" % hashlib.md5(
        ("|".join(sorted(args.rooms)) + "|%d|%d" % (args.max_points, args.rollouts)).encode()).hexdigest()[:10]
    first = {}
    if os.path.exists(cache):
        with open(cache, encoding="utf-8") as source:
            first = {int(k): v for k, v in json.load(source).items()}
        print("复用第一轮缓存 %s（%d 个点）" % (cache, len(first)))
    todo = [(i, s, args.rollouts, "a") for i, s in enumerate(states) if i not in first]
    if todo:
        with Pool(args.jobs, initializer=_init_eval, initargs=(cal,)) as pool:
            for done, (i, vals) in enumerate(pool.imap_unordered(_option_values, todo), 1):
                first[i] = [sum(v) / len(v) for v in vals]
                if done % 50 == 0 or done == len(todo):
                    print("  第一轮 %d / %d" % (done, len(todo)), flush=True)
                    with open(cache, "w", encoding="utf-8") as sink:
                        json.dump(first, sink)
    gaps = []
    for i, means in first.items():
        best = max(range(len(means)), key=lambda k: means[k])
        gap = means[best] - means[states[i]["actual"]]
        if best != states[i]["actual"] and gap > 1.0:
            gaps.append((gap, i, best))
    gaps.sort(reverse=True)
    flagged = gaps[:args.confirm_top]
    print("第一轮可疑点 %d 个，复核差距最大的 %d 个（只比 实际 vs 推演最优，各 %d 次）……" % (
        len(gaps), len(flagged), args.confirm))

    confirmed = []
    tasks = []
    for _gap, i, best in flagged:
        st = dict(states[i])
        st["opts"] = [states[i]["opts"][states[i]["actual"]], states[i]["opts"][best]]
        tasks.append((i, st, args.confirm, "b"))
    with Pool(args.jobs, initializer=_init_eval, initargs=(cal,)) as pool:
        for done, (i, vals) in enumerate(pool.imap_unordered(_option_values, tasks), 1):
            diffs = [x - y for x, y in zip(vals[1], vals[0])]
            m = sum(diffs) / len(diffs)
            sd = (sum((d - m) ** 2 for d in diffs) / max(len(diffs) - 1, 1)) ** 0.5
            se = sd / len(diffs) ** 0.5
            if m > 0.5 and m > 2 * se:
                best = next(b for _g, j, b in flagged if j == i)
                confirmed.append((m, se, i, best))
            if done % 20 == 0 or done == len(tasks):
                print("  复核 %d / %d" % (done, len(tasks)), flush=True)

    confirmed.sort(reverse=True)
    loss = sum(m for m, _, _, _ in confirmed) * scale
    print("\n=== 复盘结论 ===")
    print("抽样中确认的决策失误 %d 个；折算到全部决策点合计少拿约 %.1f 分，摊到每局 %.2f 分（我们 %d 局）" % (
        len(confirmed), loss, loss / max(rounds, 1), rounds))
    print("（只复核了差距最大的 %d 个可疑点，这是下限估计）" % len(flagged))
    by = defaultdict(lambda: [0, 0.0])
    for m, _, i, _ in confirmed:
        st = states[i]
        key = "%s/%d白/%d露" % (states[i]["kind"], min(st["hand"].count(JOKER), 2), min(len(st["melds"]), 2))
        by[key][0] += 1
        by[key][1] += m * scale
    for key, (n, tot) in sorted(by.items(), key=lambda kv: -kv[1][1]):
        print("  %-14s 抽样中 %3d 次  折算合计 %.1f 分" % (key, n, tot))
    print("\n最大的 %d 个失误：" % min(args.top, len(confirmed)))
    for m, se, i, best in confirmed[:args.top]:
        st = states[i]
        melds = " ".join("[%s]" % "".join(g["tiles"]) for g in st["melds"])
        extra = ("  对手打出 %s" % st["tile"]) if st["kind"] != "弃牌" else ""
        print("  局%-3s %s%s 墙%-3d 手牌 %s %s%s" % (
            st.get("round_no", "?"), "庄 " if st["dealer"] else "", st["kind"], st["wall"],
            " ".join(sorted(st["hand"])), melds, extra))
        print("        我们 %s → 推演认为 %s 更好，每次多 %.1f 分（±%.1f）" % (
            _desc(st, st["actual"]), _desc(st, best), m, 2 * se))
    print("\n读法：只有在多个房里反复出现的同类失误才是真问题；单个失误可能是推演模型的偏差"
          "（别家不建模手牌、摸打随机）。")


# ---------------------------------------------------------------- 2d. 胡 vs 弃胡转爆头（S1 的格子该不该开）


def collect_hu_file(args):
    """摸牌后能胡但不是爆头、且存在一张弃牌能转成爆头听牌的局面（S1 的决策点）。"""
    path, sample = args
    from mj.rules import baotou, evaluate
    out, stats = [], Counter()
    try:
        with open(path, encoding="utf-8") as source:
            game = json.load(source)
    except (OSError, ValueError):
        return out, stats
    names = seat_names(game)
    if len(names) != 4:
        return out, stats
    info = {r.get("round_no"): r for r in game.get("rounds") or []}
    base = os.path.basename(path)
    for rnd in merge_rounds(game):
        meta = info.get(rnd["round_no"])
        if not meta or rnd.get("truncated") or not rnd.get("start_hands"):
            continue
        dealer = meta.get("dealer", rnd.get("dealer"))
        winner = None if meta.get("is_draw") else meta.get("winner")
        hands = [list(h) for h in rnd["start_hands"]]
        total_start = sum(len(h) for h in hands)
        drawn = 0
        melds = [[] for _ in range(4)]
        seen = [0] * 34
        draws = [0] * 4
        piaoed = [False] * 4
        pending = {}
        try:
            for ev in rnd["events"]:
                kind, seat, tile = ev["type"], ev.get("seat"), ev.get("tile")
                data = ev.get("data") or {}
                if kind == "tile_drawn":
                    hands[seat].append(tile)
                    drawn += 1
                    draws[seat] += 1
                    pending.pop(seat, None)
                    name = names[seat]
                    g = "ours" if name == OUR else ("master" if name in MASTERS else None)
                    hand = hands[seat]
                    m = len(melds[seat])
                    if (g and JOKER in hand and not piaoed[seat] and len(hand) + 3 * m == 14
                            and _keep("%s|%s" % (base, ev.get("seq")), sample)):
                        rest = list(hand)
                        rest.remove(tile)
                        res = evaluate(tuple(to_counts(rest)), TILE_INDEX[tile], meld_groups=m)
                        if res and not res["baotou"]:
                            outs = []
                            for t in sorted(set(hand)):
                                if t == JOKER:
                                    continue
                                left = list(hand)
                                left.remove(t)
                                if baotou(tuple(to_counts(left)), m):
                                    outs.append(t)
                            if outs:
                                pending[seat] = {
                                    "f": base, "p": name, "g": g, "hand": list(hand),
                                    "melds": [dict(x, tiles=list(x["tiles"])) for x in melds[seat]],
                                    "seen": list(seen), "wall": 136 - total_start - drawn,
                                    "dealer": int(seat == dealer), "turn": draws[seat], "drawn": tile,
                                    "hu_fan": res["fan"], "round_no": rnd["round_no"],
                                    "opts": [["hu"]] + [["discard", t] for t in outs]}
                elif kind == "tile_discarded":
                    st = pending.pop(seat, None)
                    if st is not None:
                        opt = ["discard", tile]
                        if opt in st["opts"]:
                            st["actual"] = st["opts"].index(opt)
                            out.append(st)
                        else:
                            stats["declined_to_non_baotou"] += 1
                    hands[seat].remove(tile)
                    seen[TILE_INDEX[tile]] += 1
                    if tile == JOKER:
                        piaoed[seat] = True
                elif kind in ("chi", "peng", "gang"):
                    pending.pop(seat, None)
                    if kind == "chi":
                        used = list(data.get("tiles") or [])
                        used.remove(tile)
                        for t in used:
                            hands[seat].remove(t)
                            seen[TILE_INDEX[t]] += 1
                        melds[seat].append({"kind": "chi", "tiles": list(data.get("tiles") or [])})
                    elif kind == "peng":
                        for _ in range(2):
                            hands[seat].remove(tile)
                        seen[TILE_INDEX[tile]] += 2
                        melds[seat].append({"kind": "peng", "tiles": [tile] * 3})
                    else:
                        gk = data.get("kind")
                        take = {"an": 4, "ming": 3, "bu": 1}[gk]
                        for _ in range(take):
                            hands[seat].remove(tile)
                        seen[TILE_INDEX[tile]] += take
                        if gk == "bu":
                            for x in melds[seat]:
                                if x["kind"] == "peng" and x["tiles"][0] == tile:
                                    x["kind"], x["tiles"] = "gang", [tile] * 4
                                    break
                        else:
                            melds[seat].append({"kind": "gang", "tiles": [tile] * 4})
        except (ValueError, KeyError, IndexError, TypeError):
            continue
        if winner is not None and winner in pending:        # 摸到后直接胡了
            st = pending[winner]
            st["actual"] = 0
            out.append(st)
        _round_stats(stats, draws, winner, dealer, meta.get("scores") or [0] * 4)
    return out, stats


def evaluate_hu(args):
    idx, st, rollouts = args
    from mj.bot import choose_action
    unseen = []
    own = Counter(st["hand"])
    for i, t in enumerate(ALL_TILES):
        unseen += [t] * max(0, 4 - st["seen"][i] - own[t])
    rate = 24 if st["dealer"] else 10
    vals = [[float(st["hu_fan"] * rate)] * rollouts] + [[] for _ in st["opts"][1:]]
    for r in range(rollouts):
        rng = random.Random("%s|h%d|%d" % (st["f"], idx, r))
        pool = list(unseen)
        rng.shuffle(pool)
        haz = [rng.random() for _ in range(300)]
        for k, opt in enumerate(st["opts"][1:], 1):
            hand13 = list(st["hand"])
            hand13.remove(opt[1])
            seen = list(st["seen"])
            seen[TILE_INDEX[opt[1]]] += 1
            sim = _Sim(hand13, st["melds"], seen, st["wall"], st["dealer"], pool)
            vals[k].append(run_sim(sim, st["turn"], haz, pre_opp=3))
    river = [ALL_TILES[i] for i in range(34) for _ in range(st["seen"][i])]
    snap = _snapshot(st["hand"], st["drawn"], st["melds"], None, st["wall"], st["dealer"], 0, river)
    act = choose_action(snap) or {}
    prod = 0
    if act.get("action") == "discard" and ["discard", act.get("tile")] in st["opts"]:
        prod = st["opts"].index(["discard", act.get("tile")])
    elif act.get("action") != "hu":
        prod = -1                                            # 线上弃成了非爆头的牌（不在比较范围）
    return idx, [sum(v) / len(v) for v in vals], prod


def main_hu(args, states, cal):
    random.Random(20260926).shuffle(states)
    half = args.states // 2
    ours = [s for s in states if s["g"] == "ours"][:half]
    mast = [s for s in states if s["g"] == "master"][:args.states - len(ours)]
    states = ours + mast
    print("推演 %d 个「能小胡、也能弃胡转爆头」的局面（我们 %d / 高手 %d），每个弃胡选项 %d 次……" % (
        len(states), len(ours), len(mast), args.rollouts))
    cell = defaultdict(Counter)
    with Pool(args.jobs, initializer=_init_eval, initargs=(cal,)) as pool:
        tasks = [(i, s, args.rollouts) for i, s in enumerate(states)]
        for done, (i, ev, prod) in enumerate(pool.imap_unordered(evaluate_hu, tasks), 1):
            st = states[i]
            key = (min(st["hand"].count(JOKER), 2), min(len(st["melds"]), 2))
            c = cell[key]
            best_decline = max(ev[1:])
            c["n"] += 1
            c["hu"] += ev[0]
            c["decline"] += best_decline
            c["decline_better"] += best_decline > ev[0]
            if prod >= 0:
                c["prod_n"] += 1
                c["prod_decline"] += prod > 0
                c["prod_regret"] += max(ev) - ev[prod]
            g = st["g"]
            c[g + "_n"] += 1
            c[g + "_decline"] += st["actual"] > 0
            c[g + "_regret"] += max(ev) - ev[st["actual"]]
            if done % 25 == 0 or done == len(states):
                print("  推演 %d / %d" % (done, len(states)), flush=True)

    print("\n=== 胡 vs 弃胡转爆头：期望分（每个决策点）===")
    print("%-10s %5s %8s %10s %9s | %-20s | %-22s | %-22s" % (
        "财神/副露", "点数", "当场胡", "最好的弃胡", "弃胡更好", "线上 遗憾 / 弃胡率", "高手 遗憾 / 弃胡率", "我们 遗憾 / 弃胡率"))
    for key in sorted(cell):
        c = cell[key]
        n = max(c["n"], 1)
        print("%-10s %5d %8.1f %10.1f %8.0f%% | %6.2f / %5.1f%%     | %6.2f / %5.1f%% n=%-4d | %6.2f / %5.1f%% n=%-4d" % (
            "%s白/%s露" % ("2+" if key[0] == 2 else key[0], "2+" if key[1] == 2 else key[1]),
            c["n"], c["hu"] / n, c["decline"] / n, 100 * c["decline_better"] / n,
            c["prod_regret"] / max(c["prod_n"], 1), 100 * c["prod_decline"] / max(c["prod_n"], 1),
            c["master_regret"] / max(c["master_n"], 1), 100 * c["master_decline"] / max(c["master_n"], 1),
            c["master_n"], c["ours_regret"] / max(c["ours_n"], 1), 100 * c["ours_decline"] / max(c["ours_n"], 1),
            c["ours_n"]))
    print("\n读法：「弃胡更好」高的格子应该打开 S1（弃胡），低的格子应该当场胡。")
    print("线上 = 现行 weights.json 下 S1 的做法（1白/1露 已关）；对照高手的弃胡率看他们是否在这一格等爆头。")


# ---------------------------------------------------------------- 3. 汇总

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--states", type=int, default=600)
    ap.add_argument("--rollouts", type=int, default=40)
    ap.add_argument("--sample", type=float, default=0.08, help="抽局面的比例（再截到 --states 个）")
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--mode", choices=["discard", "claim", "hu"], default="discard",
                    help="discard = 持财神弃牌；claim = 吃碰 接 vs 过；hu = 当场小胡 vs 弃胡转爆头")
    ap.add_argument("--rooms", nargs="*", help="逐房复盘：只推演这些房里我们的每一个可选决策")
    ap.add_argument("--confirm", type=int, default=100, help="逐房复盘：可疑点复核时每个选项的推演次数")
    ap.add_argument("--confirm-top", type=int, default=120, help="逐房复盘：只复核第一轮差距最大的前 N 个")
    ap.add_argument("--max-points", type=int, default=800, help="逐房复盘：最多随机抽多少个决策点")
    ap.add_argument("--top", type=int, default=12)
    args = ap.parse_args()
    if args.rooms:
        if args.rollouts == 40:
            args.rollouts = 16
        main_rooms(args)
        return

    files = discover_files()
    states, stats = [], Counter()
    collector = {"claim": collect_claims_file, "hu": collect_hu_file}.get(args.mode, collect_file)
    if args.mode == "hu" and args.sample == 0.08:
        args.sample = 1.0                          # 这类局面稀少，全取
    with Pool(args.jobs) as pool:
        for done, (rows, st) in enumerate(pool.imap_unordered(collector, [(f, args.sample) for f in files],
                                                              chunksize=8), 1):
            states += rows
            stats.update(st)
            if done % 500 == 0 or done == len(files):
                print("  抽局面 %d / %d 个文件，候选局面 %d" % (done, len(files), len(states)), flush=True)
    hazard = {str(k): stats["win_%d" % k] / stats["alive_%d" % k]
              for k in range(1, 26) if stats["alive_%d" % k]}
    cal = {"hazard": hazard,
           "pay_n": stats["pay_sum_n"] / max(stats["pay_n_n"], 1),
           "pay_d": stats["pay_sum_d"] / max(stats["pay_n_d"], 1)}
    print("标定：别家自摸时本座平均支付 闲 %.2f / 庄 %.2f；每次摸牌自摸危险率 第3手 %.3f 第8手 %.3f 第12手 %.3f"
          % (cal["pay_n"], cal["pay_d"], hazard.get("3", 0), hazard.get("8", 0), hazard.get("12", 0)))

    if args.mode == "claim":
        main_claim(args, states, cal)
        return
    if args.mode == "hu":
        main_hu(args, states, cal)
        return
    random.Random(20260925).shuffle(states)
    # 我们与高手各占一半（用于比较遗憾值），不够的用其他玩家补足
    half = args.states // 2
    ours = [s for s in states if s["g"] == "ours"][:half]
    mast = [s for s in states if s["g"] == "master"][:args.states - len(ours)]
    rest = [s for s in states if s["g"] == "other"][:args.states - len(ours) - len(mast)]
    states = ours + mast + rest
    print("推演 %d 个局面（我们 %d / 高手 %d / 其他 %d），每个候选 %d 次……" % (
        len(states), sum(s["g"] == "ours" for s in states), sum(s["g"] == "master" for s in states),
        sum(s["g"] == "other" for s in states), args.rollouts))

    weights = _fit.load_weights()
    cell = defaultdict(lambda: Counter())
    agree = Counter()
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as sink, \
            Pool(args.jobs, initializer=_init_eval, initargs=(cal,)) as pool:
        tasks = [(i, s, args.rollouts) for i, s in enumerate(states)]
        for done, (i, means, prod) in enumerate(pool.imap_unordered(evaluate_state, tasks), 1):
            st = states[i]
            best = max(st["tier"], key=lambda c: means[c])
            key = (min(st["hand"].count(JOKER), 2), min(len(st["melds"]), 2))
            c = cell[key]
            c["n"] += 1
            if prod in means:
                c["prod_regret"] += means[best] - means[prod]
                c["prod_best"] += prod == best
                c["prod_n"] += 1
            if st["g"] in ("master", "ours"):
                c[st["g"] + "_regret"] += means[best] - means[st["actual"]]
                c[st["g"] + "_n"] += 1
                agree[st["g"] + "_best"] += st["actual"] == best
                agree[st["g"] + "_n"] += 1
            if max(means.values()) - min(means.values()) < 1.0:
                agree["tie_skipped"] += 1           # 各候选推演结果几乎一样（打哪张最后都收敛到同一手），不作标签
                continue
            agree["labeled"] += 1
            visible = [min(4, st["seen"][j] + st["hand"].count(ALL_TILES[j])) for j in range(34)]
            X = [[features_joker(st["hand"], t, len(st["melds"]), visible, weights)[n] for n in J_FEATURES]
                 for t in st["tier"]]
            sink.write(json.dumps({
                "k": "dj", "g": "master", "p": "推演最优", "f": st["f"], "d": st["dealer"],
                "c": st["tier"], "X": X, "y": st["tier"].index(best),
                "cur": st["tier"].index(prod) if prod in st["tier"] else -1,
                "ev": [round(means[t], 2) for t in st["tier"]], "src": st["g"],
                "actual": st["tier"].index(st["actual"]),
            }, ensure_ascii=False) + "\n")
            if done % 20 == 0 or done == len(states):
                print("  推演 %d / %d" % (done, len(states)), flush=True)

    print("\n=== 与推演最优的期望分差（每个决策点，分）：越小越好 ===")
    print("%-10s %5s | %-22s | %-16s | %-16s" % ("财神/副露", "局面", "线上策略 遗憾 / 选中最优", "高手实际 遗憾", "我们实际 遗憾"))
    tot = Counter()
    for key in sorted(cell):
        c = cell[key]
        tot.update(c)
        label = "%s白/%s露" % ("2+" if key[0] == 2 else key[0], "2+" if key[1] == 2 else key[1])
        print("%-10s %5d | %6.2f / %5.1f%%        | %6.2f (n=%-4d) | %6.2f (n=%-4d)" % (
            label, c["n"], c["prod_regret"] / max(c["prod_n"], 1), 100 * c["prod_best"] / max(c["prod_n"], 1),
            c["master_regret"] / max(c["master_n"], 1), c["master_n"],
            c["ours_regret"] / max(c["ours_n"], 1), c["ours_n"]))
    print("%-10s %5d | %6.2f / %5.1f%%        | %6.2f (n=%-4d) | %6.2f (n=%-4d)" % (
        "合计", tot["n"], tot["prod_regret"] / max(tot["prod_n"], 1), 100 * tot["prod_best"] / max(tot["prod_n"], 1),
        tot["master_regret"] / max(tot["master_n"], 1), tot["master_n"],
        tot["ours_regret"] / max(tot["ours_n"], 1), tot["ours_n"]))
    print("\n写入标签 %d 个局面（另有 %d 个局面各候选推演结果几乎相同，不作标签）"
          % (agree["labeled"], agree["tie_skipped"]))
    for g, label in (("ours", "我们"), ("master", "高手")):
        if agree[g + "_n"]:
            print("%s实际选择 = 推演最优 的比例：%.1f%%（n=%d，含平局局面）"
                  % (label, 100 * agree[g + "_best"] / agree[g + "_n"], agree[g + "_n"]))
    print("\n读法：遗憾 = 推演最优那张的期望分 − 实际打的那张的期望分（同一局面、同一批随机牌序）。")
    print("线上遗憾 < 高手遗憾 → 现行策略在这类局面已经不比高手差；反之说明还有可挖的分。")
    print("下一步：python3 tools/discard_fit.py fit --inp %s --kinds dj --save models/rollout_fit_weights.json"
          % args.out)


if __name__ == "__main__":
    main()
