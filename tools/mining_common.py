# -*- coding: utf-8 -*-
"""策略挖掘管线的共用工具：事件重放、财神爆头距离、弃牌分类、服务端代打配对。

被 tools/decision_table.py、tools/opp_tenpai.py、tools/divergence_mine.py、
tools/rule_extract.py 共用。纯 stdlib，Python 3.9（不用 match / X|Y 注解）。

数据源与约定见 docs/STRATEGY_MINING.md 与 .claude/skills/mahjong-strategy/SKILL.md。
"""
import glob
import os
from functools import lru_cache

import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mj.tiles import ALL_TILES, JOKER, JOKER_IDX, NSUITS, TILE_INDEX, is_suited, rank  # noqa: E402

OUR_UID = "u_fd06550b5fb3"
OUR_NAME = "重生之我是雀神"


def seat_names(game):
    """四个座位的名字；我们的账号按 user_id 统一成 OUR_NAME。
    2026-09-28 平台上我们改名为「乾元用九」（user_id 不变），按名字认人的工具会把这些房漏掉。"""
    return [OUR_NAME if s.get("user_id") == OUR_UID else s.get("name") for s in game.get("seats") or []]


# ---------------------------------------------------------------------------
# 文件发现（按 basename 去重，两个目录有重叠）
# ---------------------------------------------------------------------------

def discover_files(patterns=("tools/models/events/*.json", "models/events/*.json"), limit=None):
    files, seen = [], set()
    for pattern in patterns:
        for path in sorted(glob.glob(pattern)):
            name = os.path.basename(path)
            if name in seen:
                continue
            seen.add(name)
            files.append(path)
    files.sort()
    if limit:
        files = files[:limit]
    return files


# ---------------------------------------------------------------------------
# blocks -> rounds 合并（与 tools/joker_playbook.py::merge_rounds 一致，
# 额外带上 truncated 标记与 dealer，供数据质量统计使用）
# ---------------------------------------------------------------------------

def merge_rounds(game):
    by = {}
    for block in game.get("blocks") or []:
        rno = block.get("round_no")
        item = by.setdefault(rno, dict(round_no=rno, start_hands=None, events=[],
                                       truncated=False, dealer=block.get("dealer")))
        hands = block.get("start_hands")
        if hands and all(hands):
            item["start_hands"] = hands
        if block.get("truncated"):
            item["truncated"] = True
        item["events"].extend(block.get("events") or [])
    out = []
    for rno in sorted(by, key=lambda x: (x is None, x)):
        by[rno]["events"].sort(key=lambda e: e.get("seq", 0))
        out.append(by[rno])
    return out


# ---------------------------------------------------------------------------
# 服务端代打配对：timeout(kind=discard, seat=s) 标记"这一手弃牌不是玩家决策"。
#
# 采用规则（20 例抽样 + 400 文件/3475 例自动化核实，见 tests/test_decision_table.py
# 与 docs/experiments/OFFLINE_REPORT.md 第一节）：
#   对每个 timeout(kind=discard, seat=s) 事件，从它往前在同一 block 的事件列表里
#   查找**最近的、尚未被配对过的、同座位的 tile_discarded 事件**，限制回溯窗口
#   （默认 6 个事件——3475 个真实样本里 97% 就是紧邻的前一条，其余是抓打圈连锁
#   反应导致中间插入了别家的 tile_drawn/tile_discarded，最远也只隔了 2 条）。
#   找到即配对（标记该 tile_discarded 的索引为 auto=1）；找不到就放弃这条 timeout
#   的配对（不影响其它事件的正常重放，只是这一手弃牌无法确认是否代打，保守按
#   "玩家决策"处理，并在统计里单独计数)。
# ---------------------------------------------------------------------------

def mark_auto_discards(events, window=6):
    """返回 set(index)：events 中被判定为服务端代打的 tile_discarded 下标。"""
    auto_idx = set()
    for i, ev in enumerate(events):
        if ev.get("type") != "timeout" or (ev.get("data") or {}).get("kind") != "discard":
            continue
        seat = ev.get("seat")
        found = None
        for j in range(i - 1, max(-1, i - 1 - window), -1):
            cand = events[j]
            if (cand.get("type") == "tile_discarded" and cand.get("seat") == seat
                    and j not in auto_idx):
                found = j
                break
        if found is not None:
            auto_idx.add(found)
    return auto_idx


# ---------------------------------------------------------------------------
# 弃牌分类：字 / 幺九 / 二八 / 中张 / 白
# ---------------------------------------------------------------------------

def act_cat(tile):
    if tile == JOKER:
        return "白"
    idx = TILE_INDEX.get(tile)
    if idx is None:
        return None
    if idx >= 27:
        return "字"
    r = idx % 9 + 1
    if r in (1, 9):
        return "幺九"
    if r in (2, 8):
        return "二八"
    return "中张"


# ---------------------------------------------------------------------------
# to_baotou：去掉 1 张财神（留作将）后，其余牌全部做成完整面子还差几步。
#
# 实现：与 mj/shanten.py::_search 同构的手写 DFS + memo，但没有"预留一对"的
# 加分项（爆头结构不需要真实的一对——单张财神本身就充当将），target 面子数
# 由 meld_groups 决定（need = 4 - meld_groups）。距离定义：
#   need_groups 里已完成 melds、离最快听牌进度 taatsu 个搭子时，
#   distance = 2*(need_groups - melds) - taatsu   (>=0)
# 没有财神时返回 None（爆头结构不成立）。
# ---------------------------------------------------------------------------

def _tb_remove(c, i, n=1):
    v = list(c)
    v[i] -= n
    return tuple(v)


@lru_cache(maxsize=1 << 20)
def _tb_search(c, wild, i, melds, taatsu, need):
    while i < NSUITS and c[i] == 0:
        i += 1
    if i >= NSUITS:
        # 所有真实牌的分支都已穷举完毕，剩余的纯财神可以自由组成"纯财神面子/
        # 搭子"（不依赖任何真实牌锚点，例如 3 张财神单独成刻）——DFS 的主体
        # 只在真实牌下标触发分支，永远不会单独消耗财神，必须在这里补上。
        # 面子比搭子效率更高（每张财神抵 2/3 距离 vs 1/2 距离），贪心先填面子。
        melds = min(melds, need)
        remaining = need - melds
        add_melds = min(wild // 3, remaining)
        melds += add_melds
        wild_left = wild - add_melds * 3
        remaining = need - melds
        taatsu = min(taatsu + wild_left // 2, remaining)
        return 2 * (need - melds) - taatsu
    n = c[i]
    best = _tb_search(_tb_remove(c, i, n), wild, i, melds, taatsu, need)
    if n >= 3:
        best = min(best, _tb_search(_tb_remove(c, i, 3), wild, i, melds + 1, taatsu, need))
    if n >= 2:
        best = min(best, _tb_search(_tb_remove(c, i, 2), wild, i, melds, taatsu + 1, need))
        if wild >= 1:
            best = min(best, _tb_search(_tb_remove(c, i, 2), wild - 1, i, melds + 1, taatsu, need))
    elif n == 1 and wild >= 1:
        best = min(best, _tb_search(_tb_remove(c, i, 1), wild - 1, i, melds, taatsu + 1, need))
    if wild >= 2:
        best = min(best, _tb_search(_tb_remove(c, i, 1), wild - 2, i, melds + 1, taatsu, need))
    if is_suited(i):
        r = rank(i)
        if r <= 7:
            need_w = (c[i + 1] == 0) + (c[i + 2] == 0)
            if need_w <= wild:
                v = list(c)
                v[i] -= 1
                if c[i + 1]:
                    v[i + 1] -= 1
                if c[i + 2]:
                    v[i + 2] -= 1
                best = min(best, _tb_search(tuple(v), wild - need_w, i, melds + 1, taatsu, need))
        if r <= 8:
            need_w = 0 if c[i + 1] else 1
            if need_w <= wild:
                v = list(c)
                v[i] -= 1
                if c[i + 1]:
                    v[i + 1] -= 1
                best = min(best, _tb_search(tuple(v), wild - need_w, i, melds, taatsu + 1, need))
        if r <= 7:
            need_w = 0 if c[i + 2] else 1
            if need_w <= wild:
                v = list(c)
                v[i] -= 1
                if c[i + 2]:
                    v[i + 2] -= 1
                best = min(best, _tb_search(tuple(v), wild - need_w, i, melds, taatsu + 1, need))
    return best


def to_baotou(counts13, meld_groups=0):
    jokers = counts13[JOKER_IDX]
    if not jokers:
        return None
    real = list(counts13)
    real[JOKER_IDX] = 0
    wild = jokers - 1
    need = 4 - meld_groups
    if need <= 0:
        return 0
    return _tb_search(tuple(real), wild, 0, 0, 0, need)


# ---------------------------------------------------------------------------
# wall_left: 136 - 起手发牌总数 - 已摸张数（含补杠摸牌）
# ---------------------------------------------------------------------------

TOTAL_TILES = 136


# ---------------------------------------------------------------------------
# 统计工具（手写，纯 stdlib）：BH 校正、两比例 z 检验、房间聚类自助法、
# 同人内部 Δ（min(nA,nB) 加权）。被 tools/hypotheses.py 使用。
# ---------------------------------------------------------------------------

import math
import random


def norm_cdf(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def two_proportion_z_test(x1, n1, x2, n2):
    """两比例 z 检验，返回 (z, p_two_sided)。任一组样本为 0 时返回 (None, None)。"""
    if n1 <= 0 or n2 <= 0:
        return None, None
    p1, p2 = x1 / n1, x2 / n2
    p = (x1 + x2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1.0 / n1 + 1.0 / n2)) if 0 < p < 1 else 0.0
    if se == 0:
        return 0.0, 1.0
    z = (p1 - p2) / se
    p_val = 2 * (1 - norm_cdf(abs(z)))
    return z, p_val


def bh_reject(pvalues, alpha=0.05):
    """Benjamini-Hochberg：返回与 pvalues 等长的布尔列表（True=拒绝原假设/显著）。
    None 视为不参与校正（对应位置恒 False）。"""
    indexed = [(i, p) for i, p in enumerate(pvalues) if p is not None]
    m = len(indexed)
    reject = [False] * len(pvalues)
    if m == 0:
        return reject
    indexed.sort(key=lambda t: t[1])
    max_rank = -1
    for rank, (_i, p) in enumerate(indexed, start=1):
        if p <= (rank / m) * alpha:
            max_rank = rank
    if max_rank < 0:
        return reject
    for rank, (i, _p) in enumerate(indexed, start=1):
        if rank <= max_rank:
            reject[i] = True
    return reject


def cluster_bootstrap_ci(rows, room_of, stat_fn, n_boot=1000, seed=20260924, alpha=0.05):
    """按房间聚类的百分位自助法 CI。``stat_fn(rows)`` 在一份（可能重复）房间的
    行集合上计算统计量，返回 None 表示这次重采样统计量不可算（跳过）。
    重采样房间（有放回），不重采样决策点/局——避免伪重复把 CI 算窄。"""
    by_room = {}
    for r in rows:
        by_room.setdefault(room_of(r), []).append(r)
    rooms = list(by_room.keys())
    if not rooms:
        return None, None, None
    rng = random.Random(seed)
    point = stat_fn(rows)
    stats = []
    for _ in range(n_boot):
        sample_rooms = [rng.choice(rooms) for _ in rooms]
        sample_rows = []
        for rm in sample_rooms:
            sample_rows.extend(by_room[rm])
        s = stat_fn(sample_rows)
        if s is not None:
            stats.append(s)
    if not stats:
        return point, None, None
    stats.sort()
    lo_idx = int((alpha / 2) * len(stats))
    hi_idx = min(len(stats) - 1, int((1 - alpha / 2) * len(stats)))
    return point, stats[lo_idx], stats[hi_idx]


def within_person_delta(rows, uid_of, group_of, value_of, group_a, group_b, min_n=1):
    """同一 uid 在同一情境桶里有时做 A、有时做 B，只比这些样本。
    汇总用 min(nA, nB) 加权（Mantel-Haenszel 式），返回
    (delta, per_uid_list[(uid, delta_uid, na, nb)])。"""
    by_uid = {}
    for r in rows:
        g = group_of(r)
        if g not in (group_a, group_b):
            continue
        uid = uid_of(r)
        by_uid.setdefault(uid, {"a": [], "b": []})
        by_uid[uid]["a" if g == group_a else "b"].append(value_of(r))
    total_w = 0.0
    weighted = 0.0
    details = []
    for uid, d in by_uid.items():
        na, nb = len(d["a"]), len(d["b"])
        if na < min_n or nb < min_n:
            continue
        ma = sum(d["a"]) / na
        mb = sum(d["b"]) / nb
        w = min(na, nb)
        weighted += w * (ma - mb)
        total_w += w
        details.append((uid, ma - mb, na, nb))
    delta = weighted / total_w if total_w else None
    return delta, details


def mean(values):
    values = list(values)
    return sum(values) / len(values) if values else None
