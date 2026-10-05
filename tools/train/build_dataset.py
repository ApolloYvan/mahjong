"""S1：从真实对局日志（回放事件）构建训练数据集——**所有玩家、所有决策**，标签是实际动作。

    python3 tools/train/build_dataset.py --smoke                      # 冒烟，<1 分钟
    caffeinate -i nice -n 15 python3 tools/train/build_dataset.py --jobs 6 --max-minutes 60

数据来源：``models/events/*.json`` 的整局事件流（``mining_common.discover_files``），用 ``RoundEngine`` 逐事件回放，
每个决策点用 ``mj.sim.snapshot.build_snapshot``（已用 99 万条真实决策核对过的服务端原始快照 schema）生成快照，
再用 ``mj.nn.encode`` 编码。决策点：
- 摸牌阶段（摸牌后 / 吃碰后）：打哪张 / 胡 / 杠；
- 响应窗口（碰窗口、吃窗口）：每个有资格的座位 过 / 碰 / 明杠 / 吃（低中高）。
服务端代打的弃牌（``mark_auto_discards``）默认不进数据集（不是玩家的决策）。

每条记录（jsonl）：``id / room / split / who(ours|master|other) / seat / round_no / phase / i,v（稀疏特征）/
y（动作下标）/ legal（合法动作下标，超集）/ ret{delta,win,dealer}（本局该座位得分增量等，价值网络用）``。
训练/验证**按房间切分**：房间名（game_id 的第二段）的哈希，``room%10 < 2`` 为验证集。``room`` 字段（0..999）也留给
S2 用"按房间哈希的一半选高手"。

缓存续跑：每个对局文件一个分片 ``tools/.cache/train/parts/<split>/<file>.jsonl``，已存在就跳过；
``--max-minutes`` 时间一到不再取新文件。明细在分片文件里，终端只打 <=40 行汇总。
"""
import argparse
import hashlib
import json
import os
import random
import sys
import time
from collections import Counter
from multiprocessing import Pool

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
os.environ.setdefault("MJ_WEIGHTS_NO_FILE", "1")

from baotou_funnel import MASTERS, OUR  # noqa: E402
from mc_offline import _apply_event  # noqa: E402
from mining_common import discover_files, mark_auto_discards, merge_rounds, seat_names  # noqa: E402
from mj.nn import encode as E  # noqa: E402
from mj.sim.engine import IllegalActionError, RoundEngine  # noqa: E402
from mj.sim.snapshot import build_snapshot  # noqa: E402

PARTS = os.path.join(ROOT, "tools", ".cache", "train", "parts")


def room_of(game_id):
    """game_id 形如 a_db9e594544b6_r1_b4_t0：第二段是房间。返回 (房间名, 0..999 哈希)。"""
    parts = os.path.basename(game_id).replace(".json", "").split("_")
    room = parts[1] if len(parts) > 1 else parts[0]
    return room, int(hashlib.md5(room.encode()).hexdigest()[:8], 16) % 1000


def split_of(room_hash):
    return "val" if room_hash % 10 < 2 else "train"


def _peek_draw_action(events, start, seat, auto):
    for j in range(start, min(len(events), start + 12)):
        ev = events[j]
        if ev.get("seat") != seat:
            if ev.get("type") == "round_ended":
                break
            continue
        t = ev.get("type")
        if t == "tile_discarded":
            return ({"action": "discard", "tile": ev.get("tile")}, j in auto)
        if t == "gang":
            return ({"action": "gang", "tile": ev.get("tile")}, False)
        if t == "round_ended":
            return ({"action": "hu", "tile": ""}, False)
    return None


def _peek_response_action(events, start, seat, window_tile):
    """该座位对这个窗口的响应；窗口之后先出现别人的动作/下一次摸打（没有它自己的记录）则返回 None（跳过，不猜）。"""
    for j in range(start, min(len(events), start + 14)):
        ev = events[j]
        t = ev.get("type")
        if ev.get("seat") == seat:
            if t == "pass" or (t == "timeout" and (ev.get("data") or {}).get("kind") == "response"):
                return {"action": "pass", "tile": ""}
            if t == "peng":
                return {"action": "peng", "tile": ev.get("tile")}
            if t == "gang":
                return {"action": "gang", "tile": ev.get("tile")}
            if t == "chi":
                used = list((ev.get("data") or {}).get("tiles") or [])
                if ev.get("tile") in used:
                    used.remove(ev.get("tile"))
                return {"action": "chi", "tile": ev.get("tile"), "tiles": used}
            if t in ("tile_discarded", "tile_drawn", "round_ended"):
                return None
        elif t in ("peng", "chi", "gang", "tile_drawn", "tile_discarded", "round_ended"):
            return None
    return None


def iter_decisions(path, keep=1.0, seed=0, include_auto=False):
    """生成器：逐个决策点产出 dict(snapshot, ctx, seat, action, who, room, round_no, phase, ret, auto, id)。"""
    try:
        with open(path, encoding="utf-8") as f:
            game = json.load(f)
    except (OSError, ValueError):
        return
    names = seat_names(game)
    if len(names) != 4:
        return
    who = ["ours" if n == OUR else "master" if n in MASTERS else "other" for n in names]
    base = os.path.basename(path)
    room, room_hash = room_of(base)
    info = {r.get("round_no"): r for r in game.get("rounds") or []}
    rng = random.Random("%s:%s" % (seed, base))
    for rnd in merge_rounds(game):
        meta = info.get(rnd["round_no"])
        hands = rnd.get("start_hands")
        if not meta or not hands or len(hands) != 4 or rnd.get("truncated") or not all(hands):
            continue
        start_scores = [0, 0, 0, 0]
        for prior in sorted(k for k in info if isinstance(k, int) and k < rnd["round_no"]):
            d = info[prior].get("scores")
            if isinstance(d, list) and len(d) == 4:
                start_scores = [a + b for a, b in zip(start_scores, d)]
        deltas = meta.get("scores") if isinstance(meta.get("scores"), list) and len(meta.get("scores")) == 4 else None
        events = rnd["events"]
        winner = None
        for ev in events:
            if ev.get("type") == "round_ended" and not (ev.get("data") or {}).get("draw"):
                winner = ev.get("seat")
        dealer = meta.get("dealer", rnd.get("dealer"))
        engine = RoundEngine.from_known_hands(hands, dealer, rnd["round_no"], scores=start_scores)
        auto = mark_auto_discards(events)
        seen_windows = set()

        def emit(i, seat, snap, ctx, action, is_auto, phase):
            prob = keep(names[seat], phase, action) if callable(keep) else keep   # keep 可以是 (名字, 阶段, 动作)->概率
            if prob <= 0 or rng.random() >= prob:
                return None
            return {"id": "%s:%d:%d:%d" % (base, rnd["round_no"], i, seat), "who": who[seat], "room": room_hash,
                    "name": names[seat], "keep_prob": prob,
                    "room_name": room, "seat": seat, "round_no": rnd["round_no"], "phase": phase,
                    "snapshot": snap, "ctx": ctx, "action": action, "auto": is_auto,
                    "ret": {"delta": deltas[seat] if deltas else None, "win": winner == seat,
                            "dealer": dealer == seat}}

        for i, ev in enumerate(events):
            kind, seat = ev.get("type"), ev.get("seat")
            if kind == "round_ended":
                break
            try:
                _apply_event(engine, ev)
            except (IllegalActionError, ValueError, KeyError, IndexError):
                break
            if engine.finished:
                break
            if engine.phase == "draw" and (
                    (kind == "tile_drawn" and seat == engine.turn and engine.drawn_tile is not None)
                    or kind in ("chi", "peng")):
                s = engine.turn
                nxt = _peek_draw_action(events, i + 1, s, auto)
                if nxt is not None and (include_auto or not nxt[1]):
                    ctx = {"piao_count": engine.piao_count[s], "chain_has_gang": bool(engine.chain_has_gang)}
                    rec = emit(i, s, build_snapshot(engine, s), ctx, nxt[0], nxt[1], "draw")
                    if rec:
                        yield rec
            elif engine.phase == "response":
                sig = (engine.response_stage, engine.window_seat, engine.window_tile,
                       len(engine.discards[engine.window_seat]), engine.drawn_count)
                if sig in seen_windows:
                    continue
                seen_windows.add(sig)
                for s in list(engine.responding_seats):
                    act = _peek_response_action(events, i + 1, s, engine.window_tile)
                    if act is None:
                        continue
                    snap = build_snapshot(engine, s)
                    yield_rec = emit(i, s, snap, {}, act, False, snap["phase"])
                    if yield_rec:
                        yield yield_rec


def encode_record(rec):
    snap = rec["snapshot"]
    idx, val = E.encode_sparse(snap, rec["ctx"])
    y = E.action_to_index(snap, rec["action"])
    legal = E.legal_actions(snap)
    return {"id": rec["id"], "room": rec["room"], "split": split_of(rec["room"]), "who": rec["who"],
            "seat": rec["seat"], "round_no": rec["round_no"], "phase": rec["phase"],
            "i": idx, "v": [round(x, 6) for x in val], "y": y, "legal": legal, "ret": rec["ret"]}


def process_file(task):
    path, keep, seed, include_auto = task
    base = os.path.basename(path)
    room, room_hash = room_of(base)
    split = split_of(room_hash)
    out_dir = os.path.join(PARTS, split)
    out_path = os.path.join(out_dir, base + ".jsonl")
    if os.path.exists(out_path):
        return base, split, None
    stats = Counter()
    os.makedirs(out_dir, exist_ok=True)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for rec in iter_decisions(path, keep, seed, include_auto):
            try:
                row = encode_record(rec)
            except (KeyError, ValueError, TypeError):
                stats["encode_error"] += 1
                continue
            if row["y"] is None:
                stats["label_unmappable"] += 1
                continue
            stats["n"] += 1
            stats["phase_%s" % row["phase"]] += 1
            stats["who_%s" % row["who"]] += 1
            stats["y_%d" % row["y"]] += 1
            if row["y"] not in row["legal"]:
                stats["label_not_legal"] += 1
                stats["notlegal_y_%d" % row["y"]] += 1
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    os.replace(tmp, out_path)
    return base, split, dict(stats)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=int(os.environ.get("MJ_JOBS", "6")))
    ap.add_argument("--keep", type=float, default=0.25, help="每个决策点保留的概率（全量约 数百万条，默认抽 25%%）")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--limit-files", type=int, default=None)
    ap.add_argument("--include-auto", action="store_true", help="也保留服务端代打的弃牌")
    ap.add_argument("--max-minutes", type=float, default=None)
    ap.add_argument("--smoke", action="store_true", help="冒烟：20 个文件、全部决策点，分片写到 smoke 目录")
    args = ap.parse_args()
    global PARTS
    if args.smoke:
        args.limit_files, args.keep = 20, 1.0
        PARTS = os.path.join(ROOT, "tools", ".cache", "train", "parts_smoke")
    files = discover_files(limit=args.limit_files)
    started = time.time()
    deadline = started + args.max_minutes * 60 if args.max_minutes else None
    total, done, skipped = Counter(), 0, 0
    per_split = Counter()
    tasks = [(p, args.keep, args.seed, args.include_auto) for p in files]
    if args.smoke or args.jobs <= 1:
        results = (process_file(t) for t in tasks)
        pool = None
    else:
        pool = Pool(args.jobs)
        results = pool.imap_unordered(process_file, tasks, chunksize=2)
    stopped = False
    try:
        for base, split, st in results:
            if st is None:
                skipped += 1
            else:
                done += 1
                total.update(st)
                per_split[split] += st.get("n", 0)
            if deadline and time.time() > deadline:
                stopped = True
                break
    finally:
        if pool:
            pool.terminate()
            pool.join()
    print("=== build_dataset（%s，%.0fs；分片目录 %s） ===" % ("冒烟" if args.smoke else "全量", time.time() - started, PARTS))
    print("文件 %d 个：本次新处理 %d，已有分片跳过 %d%s" % (len(files), done, skipped,
                                                 "；到 --max-minutes 上限提前停止，再跑同一命令续跑" if stopped else ""))
    print("新增记录 %d（训练 %d / 验证 %d）；按谁：%s" % (
        total["n"], per_split["train"], per_split["val"],
        {k[4:]: v for k, v in total.items() if k.startswith("who_")}))
    print("按阶段：%s" % {k[6:]: v for k, v in total.items() if k.startswith("phase_")})
    ys = Counter({int(k[2:]): v for k, v in total.items() if k.startswith("y_")})
    names = {E.A_HU: "胡", E.A_GANG_SELF: "杠", E.A_PASS: "过", E.A_PENG: "碰", E.A_GANG_MING: "明杠",
             E.A_CHI_LOW: "吃低", E.A_CHI_MID: "吃中", E.A_CHI_HIGH: "吃高"}
    print("非出牌动作：%s；出牌 %d" % ({names[k]: v for k, v in sorted(ys.items()) if k in names},
                                    sum(v for k, v in ys.items() if k < 34)))
    print("标签无法映射 %d，编码出错 %d，标签不在合法集合内 %d（%s）" % (
        total["label_unmappable"], total["encode_error"], total["label_not_legal"],
        {k[10:]: v for k, v in total.items() if k.startswith("notlegal_y_")} or "无"))


if __name__ == "__main__":
    main()
