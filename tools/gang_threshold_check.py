"""G1 返修第 1 条：GANG_FORBIDDEN_REMAINING 口径核实。

已知：``tools/sim_snapshot_check.py`` 的 3c 全量核对（37 万条决策）证实
``引擎 wall_remaining = 服务端 wall_remaining + 1``，是全局固定偏移。服务端
真实规则是"wall_remaining<=20 禁杠"（``mj/responses.py``/``mj/state.py``
的既有实现和注释，走服务端原始口径），换算成引擎口径应该是"<=21 禁杠"——
但 ``mj/sim/engine.py`` 当前的 ``GANG_FORBIDDEN_REMAINING`` 还是 20（照抄
服务端数字，没做 +1 换算），这条工具就是要拿真实语料把这个换算钉死：
统计全量语料里所有真实发生过的 gang 事件，发生那一刻的引擎 wall_remaining
（用 mj.sim.engine 强制回放算出来，不是猜的）分布，尤其是最小值——如果
换算无误，理论上应该全部 >=22（服务端不允许 wall_remaining<=20 时杠，
换算成引擎口径就是不允许 <=21，真实发生的杠都应该在 >=22 的时候）。

    python3 tools/gang_threshold_check.py --limit 30    # 冒烟
    python3 tools/gang_threshold_check.py --jobs 8       # 全量，交给用户执行
"""
import argparse
import json
import os
import sys
from collections import Counter
from multiprocessing import Pool

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from mining_common import discover_files, merge_rounds  # noqa: E402
from mj.sim.engine import IllegalActionError, RoundEngine  # noqa: E402


def _scan_round(rnd, meta):
    """返回 (gang_walls, mismatches)：gang_walls 是每次真实 gang 事件发生时
    引擎算出的 wall_remaining 列表；mismatches 是引擎判定该次杠不合法的
    案例（不该出现——真实日志里这个杠确实发生了）。"""
    gang_walls = []
    mismatches = []
    hands = rnd.get("start_hands")
    if not hands or len(hands) != 4 or not all(hands):
        return gang_walls, mismatches
    dealer = meta.get("dealer", rnd.get("dealer"))
    if dealer is None:
        return gang_walls, mismatches
    engine = RoundEngine.from_known_hands(hands, dealer, rnd["round_no"], scores=[0, 0, 0, 0])
    events = rnd["events"]
    if len(events) == 1 and events[0].get("type") == "round_ended":
        engine.drawn_tile = engine.hands[dealer][-1]
    for i, ev in enumerate(events):
        kind = ev.get("type")
        seat = ev.get("seat")
        tile = ev.get("tile")
        data = ev.get("data") or {}
        try:
            if kind == "tile_drawn":
                engine.step_draw(forced_tile=tile)
            elif kind == "tile_discarded":
                engine.apply_discard(seat, tile)
            elif kind == "chi":
                used = list(data.get("tiles") or [])
                if tile in used:
                    used.remove(tile)
                engine.apply_claim(seat, "chi", used, auto_draw=False)
            elif kind == "peng":
                engine.apply_claim(seat, "peng", None, auto_draw=False)
            elif kind == "gang":
                sub = data.get("kind")
                wall_before = engine.wall_remaining()
                gang_walls.append(wall_before)
                if sub == "ming":
                    engine.apply_claim(seat, "gang", None, auto_draw=False)
                else:
                    engine.apply_gang(seat, tile, sub, auto_draw=False)
            elif kind == "pass":
                engine.apply_pass(seat)
            elif kind == "timeout":
                if data.get("kind") == "response":
                    engine.apply_pass(seat)
                continue
            elif kind == "round_ended":
                if not data.get("draw"):
                    engine.apply_hu(meta.get("winner"))
                continue
            else:
                continue
        except IllegalActionError as exc:
            mismatches.append((i, kind, seat, tile, str(exc)))
        except (ValueError, KeyError, IndexError) as exc:
            mismatches.append((i, kind, seat, tile, "内部异常 %r" % exc))
    return gang_walls, mismatches


def scan(path):
    walls = []
    mismatches = []
    try:
        with open(path, encoding="utf-8") as source:
            game = json.load(source)
    except (OSError, ValueError):
        return path, walls, mismatches
    info = {r.get("round_no"): r for r in game.get("rounds") or []}
    for rnd in merge_rounds(game):
        meta = info.get(rnd["round_no"])
        if not meta or rnd.get("truncated"):
            continue
        gw, mm = _scan_round(rnd, meta)
        walls.extend(gw)
        for m in mm:
            mismatches.append((path, rnd["round_no"]) + m)
    return path, walls, mismatches


def _scan_path(path):
    return scan(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    files = discover_files(limit=args.limit)

    all_walls = []
    all_mismatches = []
    with Pool(args.jobs) as pool:
        for done, (path, walls, mismatches) in enumerate(pool.imap_unordered(_scan_path, files, chunksize=4), 1):
            all_walls.extend(walls)
            all_mismatches.extend(mismatches)
            if done % 500 == 0 or done == len(files):
                print("  已处理 %d / %d 个文件，累计杠事件 %d，累计非法 %d" % (
                    done, len(files), len(all_walls), len(all_mismatches)), flush=True)

    print("\n=== GANG_FORBIDDEN_REMAINING 口径核实 ===")
    print("文件数 %d，真实 gang 事件总数 %d" % (len(files), len(all_walls)))
    if all_walls:
        print("引擎 wall_remaining 最小值 %d，最大值 %d" % (min(all_walls), max(all_walls)))
        dist = Counter(all_walls)
        low = sorted(w for w in dist if w <= 25)
        print("低位分布（<=25）：%s" % {w: dist[w] for w in low})
    print("回放非法动作数（不该有，出现说明当前阈值/规则挡住了真实发生过的杠）：%d" % len(all_mismatches))
    if all_mismatches:
        print("前 20 条非法明细：")
        for row in all_mismatches[:20]:
            print(" ", row)


if __name__ == "__main__":
    main()
