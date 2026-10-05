"""S1 验收：编码器在 N 个真实快照上与"生产快照"逐字段一致、可逆字段可还原、动作空间可往返。

    python3 tools/train/check_encoder.py --smoke                 # 冒烟，<1 分钟
    caffeinate -i nice -n 15 python3 tools/train/check_encoder.py --n 10000 --max-minutes 20

检查项（任何一项有违例都以非零退出码结束）：
1. 形状：稠密向量长度 == FEATURE_DIM，全部有限；稀疏（升序、无重复、非零）与稠密一致；
2. 独立参考实现（本文件里的 ``ref_encode``：直接按快照字段、用 list.count 之类另一种写法逐块算出来）与
   ``encode`` 逐元素完全相等 —— "逐字段一致"；
3. ``decode(encode(s))`` 的可逆字段（手牌/刚摸/窗口牌、副露、牌河计数、最近 6 张、墙、局号、庄、分数、阶段、链…）
   还原回快照里的值；
4. 动作：标签 -> 下标 -> 动作 dict -> 下标 往返不变；标签在合法动作集合内；
5. 真实线上日志（logs/*.jsonl 的 decision 记录，我们自己 bot 的真实决策）转成快照后能编码（字段缺口会列出来：
   当前落盘的 decision 记录没有 discards / window_tile，说明线上快照里这两个字段是否存在需要另外确认）。

快照来自回放整局事件流（``build_dataset.iter_decisions``，``build_snapshot`` 的 schema 已用 99 万条真实决策核对）。
"""
import argparse
import glob
import json
import math
import os
import random
import sys
import time
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "tools", "train"))
os.environ.setdefault("MJ_WEIGHTS_NO_FILE", "1")

from build_dataset import iter_decisions  # noqa: E402
from mining_common import discover_files  # noqa: E402
from mj.nn import encode as E  # noqa: E402
from mj.tiles import ALL_TILES, JOKER  # noqa: E402


def ref_encode(snap, ctx):
    """独立参考实现：不共用 encode.py 的任何辅助函数，只共用块布局（偏移）。"""
    v = [0.0] * E.FEATURE_DIM
    me = snap["seat"]

    def off(name):
        return E.LAYOUT[name][0]

    hand = snap["my_hand"]
    for k, t in enumerate(ALL_TILES):
        v[off("hand") + k] = hand.count(t) / 4.0
        if snap.get("drawn_tile") == t:
            v[off("drawn") + k] = 1.0
        if (snap.get("window_tile") or snap.get("last_discard") or snap.get("discarded_tile")) == t:
            v[off("window") + k] = 1.0
    for seat in range(4):
        r = (seat - me) % 4
        tiles = [t for m in snap["melds"][seat] for t in m["tiles"]]
        river = snap["discards"][seat]
        for k, t in enumerate(ALL_TILES):
            v[off("meld_tiles") + r * 34 + k] = tiles.count(t) / 4.0
            v[off("disc_count") + r * 34 + k] = river.count(t) / 4.0
        n_chi = sum(1 for m in snap["melds"][seat] if m["kind"] == "chi")
        n_peng = sum(1 for m in snap["melds"][seat] if m["kind"] == "peng")
        n_an = sum(1 for m in snap["melds"][seat] if m["kind"] == "gang_an")
        n_gang = sum(1 for m in snap["melds"][seat] if m["kind"].startswith("gang")) - n_an
        for k, n in enumerate((n_chi, n_peng, n_an, n_gang)):
            v[off("meld_kinds") + r * 4 + k] = n / 4.0
        for slot in range(E.RECENT):
            if slot < len(river):
                v[off("disc_recent") + (r * E.RECENT + slot) * 34 + ALL_TILES.index(river[len(river) - 1 - slot])] = 1.0
    for k, t in enumerate(ALL_TILES):
        total = hand.count(t)
        for seat in range(4):
            total += snap["discards"][seat].count(t) + sum(m["tiles"].count(t) for m in snap["melds"][seat])
        v[off("visible") + k] = total / 4.0
    v[off("wall")] = snap["wall_remaining"] / 136.0
    rn = max(1, min(snap["round_no"], E.MAX_ROUND))
    v[off("round_oh") + rn - 1] = 1.0
    v[off("round_frac")] = snap["round_no"] / 8.0
    v[off("dealer_rel") + (snap["dealer"] - me) % 4] = 1.0
    v[off("is_dealer")] = 1.0 if snap["dealer"] == me else 0.0
    v[off("turn_rel") + (snap["turn"] - me) % 4] = 1.0
    for seat in range(4):
        v[off("scores") + (seat - me) % 4] = snap["scores"][seat] / 100.0
    v[off("phase") + ("draw", "response_peng", "response_chi").index(snap["phase"])] = 1.0
    v[off("self_responding")] = 1.0 if me in snap["responding_seats"] else 0.0
    god = snap["god"]
    v[off("catch_play")] = 1.0 if god["catch_play"] else 0.0
    g = god["god_discarder_seat"]
    v[off("god_rel") + (4 if g is None or g < 0 else (g - me) % 4)] = 1.0
    v[off("chain")] = god["chain_count"] / 3.0
    v[off("chain_visible")] = 1.0 if god["chain_count"] else 0.0
    v[off("my_piao")] = ctx["piao_count"] / 4.0
    v[off("chain_has_gang")] = 1.0 if ctx["chain_has_gang"] else 0.0
    v[off("joker_cnt")] = hand.count(JOKER) / 4.0
    v[off("my_melds")] = len(snap["melds"][me]) / 4.0
    v[off("has_drawn")] = 1.0 if snap.get("drawn_tile") else 0.0
    return v


def check_decode(snap, ctx, dense):
    """返回不一致的字段名列表。"""
    me = snap["seat"]
    d = E.decode(dense)
    bad = []
    ti = {t: i for i, t in enumerate(ALL_TILES)}
    hand_c = [snap["my_hand"].count(t) for t in ALL_TILES]
    if d["hand"] != hand_c:
        bad.append("hand")
    if d["drawn"] != (ti[snap["drawn_tile"]] if snap.get("drawn_tile") else None):
        bad.append("drawn")
    wt = snap.get("window_tile") or snap.get("last_discard") or snap.get("discarded_tile")
    if d["window"] != (ti[wt] if wt else None):
        bad.append("window")
    for seat in range(4):
        r = (seat - me) % 4
        mt = [0] * 34
        for m in snap["melds"][seat]:
            for t in m["tiles"]:
                mt[ti[t]] += 1
        if d["meld_tiles"][r] != mt:
            bad.append("meld_tiles")
        river = snap["discards"][seat]
        if d["disc_count"][r] != [river.count(t) for t in ALL_TILES]:
            bad.append("disc_count")
        want = [ti[t] for t in reversed(river[-E.RECENT:])]
        want += [None] * (E.RECENT - len(want))
        if d["disc_recent"][r] != want:
            bad.append("disc_recent")
    if d["wall"] != snap["wall_remaining"]:
        bad.append("wall")
    if d["round_no"] != max(1, min(snap["round_no"], E.MAX_ROUND)):
        bad.append("round_no")
    if d["dealer_rel"] != (snap["dealer"] - me) % 4:
        bad.append("dealer")
    if d["turn_rel"] != (snap["turn"] - me) % 4:
        bad.append("turn")
    if d["scores"] != [snap["scores"][(me + r) % 4] for r in range(4)]:
        bad.append("scores")
    if d["phase"] != snap["phase"]:
        bad.append("phase")
    if d["catch_play"] != bool(snap["god"]["catch_play"]):
        bad.append("catch_play")
    if d["chain"] != snap["god"]["chain_count"]:
        bad.append("chain")
    if d["my_piao"] != ctx["piao_count"] or d["chain_has_gang"] != bool(ctx["chain_has_gang"]):
        bad.append("ctx")
    return bad


def log_decision_to_snapshot(p):
    """logs/*.jsonl 里 kind=decision 的 payload -> 快照（字段名 hand->my_hand；没有的字段补空，并记下缺了什么）。"""
    snap = {"seat": p.get("seat"), "phase": p.get("phase"), "dealer": p.get("dealer"), "round_no": p.get("round_no"),
            "wall_remaining": p.get("wall_remaining"), "scores": p.get("scores") or [0, 0, 0, 0],
            "my_hand": p.get("hand") or [], "drawn_tile": p.get("drawn_tile") or None, "turn": p.get("turn"),
            "melds": p.get("melds") or [[], [], [], []], "responding_seats": p.get("responding_seats") or [],
            "god": p.get("god") or {}, "discards": p.get("discards") or [[], [], [], []],
            "window_tile": p.get("window_tile")}
    missing = [k for k in ("discards", "window_tile") if k not in p]
    return snap, missing


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=10000)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--max-minutes", type=float, default=None)
    ap.add_argument("--smoke", action="store_true")
    args = ap.parse_args()
    if args.smoke:
        args.n = 300
    files = discover_files()
    random.Random(args.seed).shuffle(files)
    keep = 0.05 if not args.smoke else 0.3
    started = time.time()
    deadline = started + args.max_minutes * 60 if args.max_minutes else None
    viol = Counter()
    n = 0
    examples = []
    labels = Counter()
    for path in files:
        if n >= args.n or (deadline and time.time() > deadline):
            break
        for rec in iter_decisions(path, keep=keep, seed=args.seed):
            snap, ctx = rec["snapshot"], rec["ctx"]
            ctx = {"piao_count": ctx.get("piao_count", 0), "chain_has_gang": ctx.get("chain_has_gang", False)}
            n += 1
            dense = E.encode(snap, ctx)
            idx, val = E.encode_sparse(snap, ctx)
            if len(dense) != E.FEATURE_DIM:
                viol["dim"] += 1
            if not all(math.isfinite(x) for x in dense):
                viol["nonfinite"] += 1
            if idx != sorted(set(idx)) or any(x == 0 for x in val) or [i for i, x in enumerate(dense) if x] != idx:
                viol["sparse_dense_mismatch"] += 1
            ref = ref_encode(snap, ctx)
            if ref != dense:
                viol["ref_mismatch"] += 1
                if len(examples) < 3:
                    diff = [i for i in range(E.FEATURE_DIM) if ref[i] != dense[i]][:5]
                    examples.append((rec["id"], diff))
            for f in check_decode(snap, ctx, dense):
                viol["decode_" + f] += 1
            y = E.action_to_index(snap, rec["action"])
            labels[y if y is None or y >= 34 else "discard"] += 1
            if y is None:
                viol["label_unmappable"] += 1
            else:
                back = E.index_to_action(snap, y)
                if back is None or E.action_to_index(snap, back) != y:
                    viol["action_roundtrip"] += 1
                if y not in E.legal_actions(snap):
                    viol["label_not_legal"] += 1
            if n >= args.n:
                break
    print("=== check_encoder（%s，%d 个真实快照，%.0fs） ===" % ("冒烟" if args.smoke else "全量", n, time.time() - started))
    print("FEATURE_DIM=%d；动作空间 %d；标签分布 %s" % (E.FEATURE_DIM, E.N_ACTIONS, dict(labels)))
    print("违例（0 为通过）：%s" % (dict(viol) or "无"))
    if examples:
        print("参考实现不一致示例（决策id, 前几个不同的特征下标）：%s" % examples)
    # 5. 真实线上日志
    ok = bad = 0
    miss = Counter()
    for path in sorted(glob.glob(os.path.join(ROOT, "logs", "*.jsonl")))[-3:]:
        try:
            f = open(path, encoding="utf-8")
        except OSError:
            continue
        with f:
            for line in f:
                if '"kind": "decision"' not in line[:80] and '"kind":"decision"' not in line[:80]:
                    continue
                try:
                    p = json.loads(line)["payload"]
                    snap, missing = log_decision_to_snapshot(p)
                    if snap["phase"] not in ("draw", "response_peng", "response_chi"):
                        continue
                    E.encode(snap, {})
                    ok += 1
                    miss.update(missing)
                except (KeyError, ValueError, TypeError):
                    bad += 1
                if ok + bad >= (200 if args.smoke else 5000):
                    break
    print("线上日志 decision 记录（最近 3 个文件）：编码成功 %d，失败 %d；落盘记录里缺的字段：%s" % (
        ok, bad, dict(miss) or "无"))
    # 6. 线上原始快照（logs 里 result 事件的 state.snapshot）：discards / last_discard 非空，last_discard 能编码进窗口牌特征
    raw = Counter()
    raw_keys = set()
    for path in sorted(glob.glob(os.path.join(ROOT, "logs", "*.jsonl"))):
        with open(path, encoding="utf-8") as f:
            for line in f:
                if "last_discard" not in line or '"kind": "result"' not in line[:80]:
                    continue
                try:
                    sn = json.loads(line)["payload"]["state"]["snapshot"]
                except (KeyError, ValueError, TypeError):
                    continue
                raw["n"] += 1
                raw_keys.update(sn)
                if sn.get("phase") not in ("draw", "response_peng", "response_chi", "finished") or not sn.get("my_hand"):
                    continue
                sn = dict(sn, phase="draw" if sn.get("phase") == "finished" else sn["phase"])
                sn.setdefault("turn", sn.get("seat"))
                sn.setdefault("responding_seats", [])
                try:
                    dense = E.encode(sn, {})
                except (KeyError, ValueError, TypeError):
                    raw["encode_fail"] += 1
                    continue
                raw["encoded"] += 1
                if any(sn.get("discards") or []):
                    raw["discards_nonempty"] += 1
                ld = sn.get("last_discard")
                if ld:
                    raw["last_discard_nonempty"] += 1
                    if E.decode(dense)["window"] == ALL_TILES.index(ld):
                        raw["last_discard_in_window_feature"] += 1
                hc = sn.get("hand_counts")
                if hc and hc[sn["seat"]] == len(sn["my_hand"]):
                    raw["hand_counts_match_own"] += 1
                if raw["n"] >= (300 if args.smoke else 100000):
                    break
        if args.smoke and raw["n"] >= 300:
            break
    print("线上原始快照（result 事件 state.snapshot）：%s；键：%s" % (dict(raw), sorted(raw_keys)))
    if raw["encoded"] and (raw["last_discard_nonempty"] != raw["last_discard_in_window_feature"]
                           or raw["discards_nonempty"] == 0 or raw["last_discard_nonempty"] == 0):
        print("违例：线上快照的 discards/last_discard 为空，或 last_discard 没进窗口牌特征")
        sys.exit(1)
    sys.exit(1 if viol or bad else 0)


if __name__ == "__main__":
    main()
